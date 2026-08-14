#!/usr/bin/env python3
"""PDF → текст/Markdown через локальный Ollama VLM (qwen3-vl / gemma4).

RTX 4060 8GB — важный вывод по qwen3-vl:8b:
  на ЦЕЛОМ сложном чертеже модель залипает в thinking (content='') и страница
  «не идёт» 10+ минут. На МЕЛКИХ кропах (~512–640px) content появляется за ~10с.

Поэтому default-пайплайн:
  1) ужать страницу (page-max ~1280)
  2) порезать на ≤4 тайла ~640px
  3) по каждому тайлу 1 короткий вызов (num_predict~768)
  4) склеить тексты БЕЗ второго LLM-merge
  5) stream + early-stop на repetition

Пример:
  python pdf_to_md_ollama.py drawing.pdf --pages 1,3,5 -o out.md
"""

from __future__ import annotations

import argparse
import base64
import io
import json
import re
import sys
import time
from pathlib import Path

import fitz
import requests
from PIL import Image

DEFAULT_MODEL = "gemma4:e4b"
OLLAMA_URL = "http://127.0.0.1:11434"

# ── качество/скорость: gemma + мелкий тайлинг, выше DPI-бюджет ────────────────
DPI_DRAWING = 150
DPI_TEXT = 120
PAGE_MAX_PIXEL = 1760          # осторожно под 8GB (HQ было 1920)
TILE_MAX_PIXEL = 650
TILE_OVERLAP_PCT = 0.08
MAX_TILES = 6                  # меньше вызовов = меньше нагрев/VRAM thrash
REQUEST_MAX_PIXEL = 650
JPEG_QUALITY = 82
MAX_RETRIES = 1
RETRY_BASE_SEC = 1.0
SEED = 42
TEMPERATURE = 0.0
NUM_PREDICT = 800
NUM_CTX = 4096
CALL_TIMEOUT = 60
THINK_ONLY_ABORT_SEC = 28

RASTER_COVER_MIN = 0.9
TEXT_RICH_THRESHOLD = 800
LARGE_FORMAT_PT = 600
MIN_TEXT_LAYER_CHARS = 400

SYSTEM_PROMPT = (
    "Ты OCR-экстрактор русских строительных чертежей. "
    "Выводи ТОЛЬКО реально видимый текст, построчно. "
    "Без рассуждений, без Markdown, без перевода, без выдумок. "
    "Кириллицу не латинизировать (В1≠B1, ХСА≠XCA). "
    "Если участок пустой/нечитаемый — ПРОПУСТИ его, не пиши «(не читается)» много раз."
)

PROMPT_TILE = (
    "Перечисли ВЕСЬ читаемый текст на ЭТОМ фрагменте. "
    "Одна строка = один факт/ячейка. Таблицы: ячейки через «;». "
    "Пустые зоны пропускай. Только данные."
)

PROMPT_PAGE = (
    "Перечисли ВЕСЬ читаемый текст на странице. "
    "Одна строка = один факт/ячейка. Таблицы: ячейки через «;». "
    "Пустые зоны пропускай. Только данные."
)

PROMPT_WITH_OCR_SUFFIX = (
    "\n\nНиже — сырой OCR того же фрагмента (может содержать ошибки). "
    "Используй его как подсказку: исправь очевидные опечатки по картинке, "
    "добавь то, что OCR пропустил, не выдумывай отсутствующее.\n\n"
    "=== OCR ===\n{ocr}\n=== /OCR ==="
)

DEFAULT_SYNTH_MODEL = "qwen3.5:4b"
SYNTH_NUM_PREDICT = 2000
SYNTH_NUM_CTX = 8192

SYNTH_SYSTEM = (
    "Ты редактор извлечений с русских строительных чертежей. "
    "Склеиваешь черновик VLM и сырой OCR в один чистый текстовый документ. "
    "Правила: (1) НЕ выдумывай факты, которых нет в источниках; "
    "(2) кириллицу не латинизируй; (3) при конфликте по кодам/цифрам предпочитай OCR; "
    "(4) убери повторы и мусор вроде «(не читается)»; "
    "(5) сохрани ВСЕ реальные коды, DN, шифры, ФИО, названия из обоих источников; "
    "(6) вывод — простой Markdown/текст, без рассуждений."
)

SYNTH_USER = (
    "Объедини черновик и OCR в один итог по странице.\n\n"
    "=== ЧЕРНОВИК VLM ===\n{draft}\n=== /ЧЕРНОВИК ===\n\n"
    "=== OCR ===\n{ocr}\n=== /OCR ===\n\n"
    "Итог:"
)

SYNTH_STRICT_SYSTEM = (
    "Ты консервативный редактор извлечений с русских строительных чертежей. "
    "Задача — объединить два источника БЕЗ сжатия и БЕЗ выдумок.\n"
    "Правила:\n"
    "1) Каждая строка/факт из черновика или OCR должен попасть в итог (можно убрать только явный мусор).\n"
    "2) ЗАПРЕЩЕНО дописывать последовательности чисел, скважины, марки, которых нет в источниках.\n"
    "3) ЗАПРЕЩЕНО обобщать таблицы в одну строку — сохраняй ячейки/строки.\n"
    "4) Кириллицу не латинизировать. Коды/DN/шифры — дословно.\n"
    "5) При конфликте цифр/кодов — предпочитай OCR.\n"
    "6) Без рассуждений, только итоговый текст/Markdown."
)

SYNTH_STRICT_USER = (
    "Склей черновик VLM и OCR. НЕ сокращай и НЕ дополняй.\n\n"
    "=== ЧЕРНОВИК VLM ===\n{draft}\n=== /ЧЕРНОВИК ===\n\n"
    "=== OCR ===\n{ocr}\n=== /OCR ===\n\n"
    "Итог (все факты из обоих блоков):"
)


def parse_page_range(page_range_str: str | None, total_pages: int) -> list[int]:
    if not page_range_str:
        return list(range(total_pages))
    pages: list[int] = []
    for part in page_range_str.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            pages.extend(range(max(1, int(a)) - 1, min(total_pages, int(b))))
        else:
            p = int(part)
            if 1 <= p <= total_pages:
                pages.append(p - 1)
    return sorted(set(pages))


def to_jpeg_b64(im: Image.Image, max_px: int = REQUEST_MAX_PIXEL) -> str:
    if im.mode != "RGB":
        im = im.convert("RGB")
    if max(im.size) > max_px:
        scale = max_px / float(max(im.size))
        im = im.resize(
            (max(1, int(im.width * scale)), max(1, int(im.height * scale))),
            Image.LANCZOS,
        )
    buf = io.BytesIO()
    im.save(buf, format="JPEG", quality=JPEG_QUALITY, optimize=True)
    return base64.b64encode(buf.getvalue()).decode("ascii")


def raster_native_dpi(page: fitz.Page, dpi: int) -> int:
    try:
        infos = page.get_image_info()
    except Exception:
        return dpi
    page_area = page.rect.width * page.rect.height
    if not infos or page_area <= 0:
        return dpi

    def _area(im: dict) -> float:
        x0, y0, x1, y1 = im["bbox"]
        return abs(x1 - x0) * abs(y1 - y0)

    best = max(infos, key=_area)
    if _area(best) / page_area < RASTER_COVER_MIN:
        return dpi
    img_w, img_h = best.get("width", 0), best.get("height", 0)
    if img_w <= 0 or img_h <= 0:
        return dpi
    native = min(img_w * 72.0 / page.rect.width, img_h * 72.0 / page.rect.height)
    return max(24, min(dpi, int(native)))


def dpi_for_page_budget(page: fitz.Page, dpi: int, page_max_px: int) -> int:
    """DPI, при котором длинная сторона ≈ page_max_px (не ниже 36)."""
    longest_pt = max(page.rect.width, page.rect.height)
    if longest_pt <= 0:
        return max(36, dpi)
    target = int(page_max_px / (longest_pt / 72.0))
    return max(36, target)


def render_page_image(page: fitz.Page, dpi: int, page_max: int) -> Image.Image:
    pix = page.get_pixmap(dpi=dpi)
    im = Image.open(io.BytesIO(pix.tobytes("png"))).convert("RGB")
    if max(im.size) > page_max:
        scale = page_max / float(max(im.size))
        im = im.resize(
            (max(1, int(im.width * scale)), max(1, int(im.height * scale))),
            Image.LANCZOS,
        )
    return im


def compute_tile_grid(w: int, h: int, tile_max: int) -> tuple[int, int]:
    cols = max(1, -(-w // tile_max))
    rows = max(1, -(-h // tile_max))
    # уложиться в MAX_TILES: укрупняем клетку
    while rows * cols > MAX_TILES:
        if cols >= rows and cols > 1:
            cols -= 1
        elif rows > 1:
            rows -= 1
        else:
            break
    return rows, cols


def strip_think_tags(text: str) -> str:
    text = re.sub(r"<think>[\s\S]*?</think>", "", text, flags=re.I)
    text = re.sub(r"<think>[\s\S]*$", "", text, flags=re.I)
    return text.strip()


def mine_quoted_and_cyrillic(text: str) -> str:
    """Достать лейблы из thinking. Отбрасываем CoT-воды."""
    cot_words = {
        "хорошо", "нужно", "сначала", "посмотрю", "изображение", "вижу",
        "несколько", "фрагмент", "пользователь", "просить", "выводи",
        "рассужден", "перевода", "латинизировать", "нечитаемое", "помечать",
        "проанализир", "перечислить", "весь", "видимый", "текст", "только",
        "построчно", "этот", "чертеж", "секций", "слева", "справа", "центре",
        "написано", "got", "looking", "first", "then", "next", "lets", "let",
    }
    found: list[str] = []
    # кавычки — самый чистый источник
    for m in re.finditer(r'[«"]([^«»"\n]{2,100})[»"]', text):
        s = m.group(1).strip()
        if len(s) >= 2:
            found.append(s)
    # токены с цифрами / DN / шифры / ИГЭ
    for m in re.finditer(
        r"(?:DN\s?\d+|\d{2}-[А-ЯA-Z]{2,5}-\d/\d{2}-[А-ЯA-Z0-9]+|ИГЭ-?\d+|"
        r"\d{2,4}/[А-ЯA-Z]|[А-Яа-яЁё]{3,}(?:\s+[А-Яа-яЁё0-9/\-]{2,}){0,6})",
        text,
    ):
        s = m.group(0).strip()
        low = s.lower()
        if any(w in low for w in cot_words):
            continue
        if len(s) < 3:
            continue
        found.append(s)

    seen: set[str] = set()
    out: list[str] = []
    for s in found:
        key = s.upper()
        if key in seen:
            continue
        seen.add(key)
        out.append(s)
    return "\n".join(out[:120])


def looks_like_repetition(text: str) -> bool:
    if len(text) < 80:
        return False
    if re.search(r"(.{8,50}?)(?:\1){8,}", text):
        return True
    # типичный loop gemma/qwen — режем рано
    if text.count("(не читается)") >= 4:
        return True
    if text.lower().count("не читается") >= 5:
        return True
    if text.count("Уровень отметки") >= 4:
        return True
    if text.count("DN250") >= 6:
        return True
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if len(lines) >= 12:
        from collections import Counter
        top, cnt = Counter(lines).most_common(1)[0]
        if cnt >= 6 and len(top) < 80:
            return True
    return False


def trim_repetition(text: str) -> str:
    if not looks_like_repetition(text):
        return text
    text = re.sub(r"(.{8,50}?)(?:\1){6,}", r"\1", text)
    lines = text.splitlines()
    seen: dict[str, int] = {}
    cut = len(lines)
    for i, ln in enumerate(lines):
        k = ln.strip()
        if not k:
            continue
        seen[k] = seen.get(k, 0) + 1
        if seen[k] >= 6:
            cut = i
            break
    return "\n".join(lines[:cut]).rstrip()


class OllamaClient:
    def __init__(
        self,
        model: str,
        base_url: str = OLLAMA_URL,
        timeout: int = CALL_TIMEOUT,
        seed: int = SEED,
        num_predict: int = NUM_PREDICT,
        num_ctx: int = NUM_CTX,
    ) -> None:
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.seed = seed
        self.num_predict = num_predict
        self.num_ctx = num_ctx
        self.session = requests.Session()

    def ping(self) -> None:
        r = self.session.get(f"{self.base_url}/api/tags", timeout=10)
        r.raise_for_status()
        names = {m.get("name") for m in r.json().get("models", [])}
        if self.model not in names and self.model.split(":")[0] not in {
            n.split(":")[0] for n in names
        }:
            raise RuntimeError(f"Модель '{self.model}' не найдена: {sorted(names)}")

    def unload(self) -> None:
        """Освободить VRAM (keep_alive=0), чтобы загрузить другую модель."""
        try:
            self.session.post(
                f"{self.base_url}/api/generate",
                json={"model": self.model, "keep_alive": 0, "prompt": ""},
                timeout=30,
            )
        except Exception as e:
            print(f"    [unload {self.model}] {e}", flush=True)

    def chat(
        self,
        prompt: str,
        image_b64: str | None = None,
        *,
        system: str | None = None,
        keep_alive: str | int = "30m",
    ) -> str:
        messages = [
            {"role": "system", "content": system or SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ]
        if image_b64:
            messages[1]["images"] = [image_b64]

        payload = {
            "model": self.model,
            "messages": messages,
            "stream": True,
            "think": False,
            "keep_alive": keep_alive,
            "options": {
                "temperature": TEMPERATURE,
                "seed": self.seed,
                "num_predict": self.num_predict,
                "num_ctx": self.num_ctx,
                "repeat_penalty": 1.35,
            },
        }

        last_err: Exception | None = None
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                return self._stream(payload)
            except Exception as e:
                last_err = e
                if "400" in str(e) and "think" in payload:
                    payload.pop("think", None)
                    continue
                print(f"    [retry {attempt}] {e}", flush=True)
                time.sleep(RETRY_BASE_SEC)
        raise RuntimeError(str(last_err))

    def _stream(self, payload: dict) -> str:
        t0 = time.time()
        content = ""
        thinking = ""
        with self.session.post(
            f"{self.base_url}/api/chat",
            json=payload,
            timeout=self.timeout,
            stream=True,
        ) as r:
            if r.status_code >= 400:
                raise RuntimeError(f"HTTP {r.status_code}: {r.text[:200]}")
            for raw in r.iter_lines(decode_unicode=True):
                if not raw:
                    continue
                try:
                    chunk = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                msg = chunk.get("message") or {}
                if msg.get("content"):
                    content += msg["content"]
                if msg.get("thinking"):
                    thinking += msg["thinking"]
                elapsed = time.time() - t0
                if content and looks_like_repetition(content):
                    break
                if (not content) and looks_like_repetition(thinking):
                    break
                if (not content) and elapsed >= THINK_ONLY_ABORT_SEC and len(thinking) > 300:
                    break
                if chunk.get("done"):
                    break
                if elapsed > self.timeout:
                    break

        content = strip_think_tags(content)
        if content.strip():
            out = trim_repetition(content.strip())
        else:
            mined = mine_quoted_and_cyrillic(thinking)
            out = trim_repetition(mined)
            if out:
                print("      (fallback: mined thinking)", flush=True)

        dt = time.time() - t0
        print(
            f"      {dt:.1f}s c={len(content)} th={len(thinking)} out={len(out)}",
            flush=True,
        )
        if not out.strip():
            raise RuntimeError(f"empty out (c={len(content)} th={len(thinking)})")
        return out


def synthesize_page(
    synth_client: OllamaClient,
    draft: str,
    ocr_text: str,
    *,
    strict: bool = False,
) -> str:
    """Текстовый fuse черновика VLM + OCR. При сбое вернуть draft."""
    draft_trim = draft.strip()
    ocr_trim = (ocr_text or "").strip()
    if not draft_trim:
        return ocr_trim
    if not ocr_trim:
        return draft_trim
    draft_use = draft_trim[:9000]
    ocr_use = ocr_trim[:6000]
    if strict:
        prompt = SYNTH_STRICT_USER.format(draft=draft_use, ocr=ocr_use)
        system = SYNTH_STRICT_SYSTEM
    else:
        prompt = SYNTH_USER.format(draft=draft_use, ocr=ocr_use)
        system = SYNTH_SYSTEM
    try:
        out = synth_client.chat(prompt, system=system)
        out = strip_think_tags(out).strip()
        if len(out) < max(80, len(draft_trim) // 8):
            print("    [synth] too short — keep draft", flush=True)
            return draft_trim
        return out
    except Exception as e:
        print(f"    [synth FAIL] {e} — keep draft", flush=True)
        return draft_trim


def page_text_layer_enough(text: str) -> bool:
    if len(text) < MIN_TEXT_LAYER_CHARS:
        return False
    lines = [ln for ln in text.splitlines() if ln.strip()]
    return len(lines) >= 15 and (sum(len(x) for x in lines) / len(lines) >= 20)


def process_page(
    client: OllamaClient | None,
    doc: fitz.Document,
    page_index: int,
    *,
    page_max: int,
    tile_max: int,
    dpi: int,
    force_vlm: bool,
    single_shot: bool,
    ocr_mode: str = "none",
    ocr_stamp: bool = False,
    synth_client: OllamaClient | None = None,
) -> str:
    page = doc[page_index]
    page_num = page_index + 1
    text = page.get_text("text").strip()
    use_ocr = ocr_mode in ("only", "with")

    is_large = max(page.rect.width, page.rect.height) > LARGE_FORMAT_PT
    is_text_rich = (len(text) > TEXT_RICH_THRESHOLD) and not is_large
    if (not force_vlm) and (not use_ocr) and (not ocr_stamp) and is_text_rich and page_text_layer_enough(text):
        print(f"  p{page_num}: Layer0 text skip VLM", flush=True)
        return text

    # целевой DPI = ровно page_max по длинной стороне
    use_dpi = dpi_for_page_budget(page, 400, page_max)
    im = render_page_image(page, use_dpi, page_max)
    w, h = im.size
    print(f"  p{page_num}: {w}x{h} @{use_dpi}dpi ocr={ocr_mode} stamp={ocr_stamp}", flush=True)

    content = ""
    if single_shot or max(w, h) <= tile_max:
        print(f"  p{page_num}: single-shot", flush=True)
        ocr_text = ""
        if use_ocr:
            from local_ocr import ocr_image

            t_ocr = time.time()
            ocr_text = ocr_image(im)
            print(f"    ocr: {len(ocr_text)} chars {time.time()-t_ocr:.1f}s", flush=True)
            if ocr_mode == "only":
                content = ocr_text
        if not content:
            assert client is not None
            prompt = PROMPT_PAGE
            if ocr_mode == "with" and ocr_text:
                prompt = PROMPT_PAGE + PROMPT_WITH_OCR_SUFFIX.format(ocr=ocr_text[:6000])
            content = client.chat(prompt, to_jpeg_b64(im, REQUEST_MAX_PIXEL))
    else:
        rows, cols = compute_tile_grid(w, h, tile_max)
        tw, th = w / cols, h / rows
        ow, oh = tw * TILE_OVERLAP_PCT, th * TILE_OVERLAP_PCT
        print(f"  p{page_num}: tiles {rows}x{cols} (max {MAX_TILES})", flush=True)

        if use_ocr:
            from local_ocr import ocr_image

        parts: list[str] = []
        n = 0
        for r in range(rows):
            for c in range(cols):
                n += 1
                t0 = time.time()
                x0 = max(0, int(c * tw - ow))
                y0 = max(0, int(r * th - oh))
                x1 = min(w, int((c + 1) * tw + ow))
                y1 = min(h, int((r + 1) * th + oh))
                crop = im.crop((x0, y0, x1, y1))
                ocr_text = ""
                if use_ocr:
                    try:
                        ocr_text = ocr_image(crop)
                    except Exception as e:
                        ocr_text = ""
                        print(f"    tile {n} OCR FAIL {e}", flush=True)
                    if ocr_mode == "only":
                        chunk = ocr_text or "(пусто)"
                        parts.append(f"--- r{r+1}c{c+1} ---\n{chunk}")
                        print(
                            f"    tile {n}/{rows*cols}: ocr {len(chunk)} chars "
                            f"{time.time()-t0:.1f}s",
                            flush=True,
                        )
                        continue
                assert client is not None
                prompt = PROMPT_TILE
                if ocr_mode == "with" and ocr_text:
                    prompt = PROMPT_TILE + PROMPT_WITH_OCR_SUFFIX.format(ocr=ocr_text[:4000])
                try:
                    chunk = client.chat(prompt, to_jpeg_b64(crop, REQUEST_MAX_PIXEL))
                except Exception as e:
                    chunk = ocr_text if ocr_text else f"(ошибка тайла r{r+1}c{c+1}: {e})"
                    print(f"    tile {n} FAIL {e}", flush=True)
                parts.append(f"--- r{r+1}c{c+1} ---\n{chunk}")
                print(f"    tile {n}/{rows*cols}: {len(chunk)} chars {time.time()-t0:.1f}s", flush=True)

        content = "\n\n".join(parts)

    stamp_raw = ""
    stamp_codes: list[str] = []
    if ocr_stamp or synth_client is not None:
        from local_ocr import ocr_stamp as run_stamp_ocr

        stamp_max = max(page_max, 2800)
        stamp_dpi = dpi_for_page_budget(page, 400, stamp_max)
        stamp_im = render_page_image(page, stamp_dpi, stamp_max)
        t_st = time.time()
        stamp_raw, stamp_codes = run_stamp_ocr(stamp_im)
        print(
            f"    stamp-ocr: codes={len(stamp_codes)} raw={len(stamp_raw)} chars "
            f"{time.time()-t_st:.1f}s",
            flush=True,
        )

    if synth_client is not None and ocr_mode != "only":
        t_sy = time.time()
        # VLM unload перед text-model уже сделан снаружи, если нужно
        content = synthesize_page(synth_client, content, stamp_raw)
        print(f"    synth: {len(content)} chars {time.time()-t_sy:.1f}s", flush=True)

    if ocr_stamp and stamp_raw:
        from local_ocr import append_stamp_missing

        content = append_stamp_missing(content, stamp_raw, stamp_codes)

    return content


def resume_dir_for(out_path: Path) -> Path:
    return out_path.with_suffix(out_path.suffix + ".pages")


def load_done(rdir: Path) -> dict[int, str]:
    done: dict[int, str] = {}
    if not rdir.exists():
        return done
    for p in rdir.glob("page_*.md"):
        m = re.match(r"page_(\d+)\.md$", p.name)
        if m:
            done[int(m.group(1))] = p.read_text(encoding="utf-8")
    return done


def save_page(rdir: Path, num: int, content: str) -> None:
    rdir.mkdir(parents=True, exist_ok=True)
    (rdir / f"page_{num:04d}.md").write_text(content, encoding="utf-8")


def assemble(pages: dict[int, str]) -> str:
    return "\n\n".join(
        f"## Страница {n}\n\n{pages[n].rstrip()}\n" for n in sorted(pages)
    ) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description="PDF → MD via Ollama (fast tiled)")
    ap.add_argument("pdf", type=Path)
    ap.add_argument("-o", "--output", type=Path, default=None)
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--base-url", default=OLLAMA_URL)
    ap.add_argument("--pages", default=None)
    ap.add_argument("--dpi", type=int, default=DPI_DRAWING)
    ap.add_argument("--page-max", type=int, default=PAGE_MAX_PIXEL)
    ap.add_argument("--tile-max", type=int, default=TILE_MAX_PIXEL)
    ap.add_argument(
        "--single-shot",
        action="store_true",
        help="Одна картинка на страницу (для gemma; для qwen на чертежах хуже)",
    )
    ap.add_argument("--force-vlm", action="store_true")
    ap.add_argument("--no-resume", action="store_true")
    ap.add_argument("--num-predict", type=int, default=NUM_PREDICT)
    ap.add_argument("--num-ctx", type=int, default=NUM_CTX)
    ap.add_argument("--timeout", type=int, default=CALL_TIMEOUT)
    ap.add_argument("--seed", type=int, default=SEED)
    ap.add_argument(
        "--ocr-only",
        action="store_true",
        help="Только локальный Tesseract (без VLM). Бесплатно, CPU.",
    )
    ap.add_argument(
        "--with-ocr",
        action="store_true",
        help="OCR-подсказки в промпт VLM (картинка + сырой OCR тайла).",
    )
    ap.add_argument(
        "--ocr-stamp",
        action="store_true",
        help="После страницы: Tesseract только углы штампа, дописать missing коды.",
    )
    ap.add_argument(
        "--synthesize",
        nargs="?",
        const=DEFAULT_SYNTH_MODEL,
        default=None,
        metavar="MODEL",
        help=f"Text-fuse черновика + штамп OCR через text-модель (default: {DEFAULT_SYNTH_MODEL}).",
    )
    args = ap.parse_args()

    if args.ocr_only and args.with_ocr:
        print("Укажи либо --ocr-only, либо --with-ocr, не оба.", file=sys.stderr)
        return 1
    ocr_mode = "only" if args.ocr_only else ("with" if args.with_ocr else "none")

    if not args.pdf.exists():
        print(f"PDF не найден: {args.pdf}", file=sys.stderr)
        return 1

    out_path = args.output or args.pdf.with_suffix(".ollama.md")
    rdir = resume_dir_for(out_path)
    client: OllamaClient | None = None
    if ocr_mode != "only":
        client = OllamaClient(
            args.model,
            args.base_url,
            timeout=args.timeout,
            seed=args.seed,
            num_predict=args.num_predict,
            num_ctx=args.num_ctx,
        )

    synth_client: OllamaClient | None = None
    if args.synthesize:
        synth_client = OllamaClient(
            args.synthesize,
            args.base_url,
            timeout=max(args.timeout, 120),
            seed=args.seed,
            num_predict=SYNTH_NUM_PREDICT,
            num_ctx=SYNTH_NUM_CTX,
        )

    if ocr_mode != "none" or args.ocr_stamp or args.synthesize:
        from local_ocr import ocr_healthcheck

        print(f"OCR -> {ocr_healthcheck()}", flush=True)
    if client is not None:
        print(f"Ollama -> {args.model}", flush=True)
    if synth_client is not None:
        print(f"Synth -> {args.synthesize}", flush=True)
    print(
        f"mode={'SINGLE' if args.single_shot else f'TILE<={MAX_TILES}'} "
        f"page-max={args.page_max} tile-max={args.tile_max} "
        f"predict={args.num_predict} ctx={args.num_ctx} ocr={ocr_mode} "
        f"stamp={args.ocr_stamp} synth={bool(args.synthesize)}",
        flush=True,
    )
    if client is not None:
        client.ping()
    if synth_client is not None:
        synth_client.ping()

    doc = fitz.open(args.pdf)
    indices = parse_page_range(args.pages, doc.page_count)
    print(f"PDF {args.pdf.name}: {len(indices)}/{doc.page_count} -> {out_path}", flush=True)

    done = {} if args.no_resume else load_done(rdir)
    pages_out = dict(done)
    t_doc = time.time()

    # если и VLM и synth — после всех VLM-страниц unload перед synth проще per-page:
    # unload VLM один раз перед первым synth call
    vlm_unloaded = False

    for idx in indices:
        num = idx + 1
        if num in pages_out and not args.no_resume:
            print(f"  p{num}: resume skip", flush=True)
            continue
        t0 = time.time()
        try:
            if synth_client is not None and client is not None and not vlm_unloaded:
                # synth нужен stamp; VLM держим до конца draft, unload перед synth внутри страницы:
                # делаем draft отдельно: сначала без synth, потом unload+synth
                pass
            content = process_page(
                client,
                doc,
                idx,
                page_max=args.page_max,
                tile_max=args.tile_max,
                dpi=args.dpi,
                force_vlm=args.force_vlm,
                single_shot=args.single_shot,
                ocr_mode=ocr_mode,
                ocr_stamp=args.ocr_stamp or bool(args.synthesize),
                synth_client=None,  # synth after unload
            )
            if synth_client is not None and ocr_mode != "only":
                if client is not None and not vlm_unloaded:
                    print(f"  unload VLM {args.model} for synth...", flush=True)
                    client.unload()
                    vlm_unloaded = True
                    time.sleep(1.0)
                # stamp уже вшит если ocr_stamp; для synth нужен raw — перечитаем угол
                from local_ocr import ocr_stamp as run_stamp_ocr

                page = doc[idx]
                stamp_im = render_page_image(
                    page,
                    dpi_for_page_budget(page, 400, 2800),
                    2800,
                )
                stamp_raw, _codes = run_stamp_ocr(stamp_im)
                # убрать уже appended штамп-блок из draft если есть
                draft_clean = re.split(r"\n### Штамп \(OCR\)\n", content, maxsplit=1)[0]
                t_sy = time.time()
                content = synthesize_page(synth_client, draft_clean, stamp_raw)
                if args.ocr_stamp:
                    from local_ocr import append_stamp_missing

                    content = append_stamp_missing(content, stamp_raw, _codes)
                print(f"    synth: {len(content)} chars {time.time()-t_sy:.1f}s", flush=True)
        except Exception as e:
            content = f"[Error p{num}: {e}]"
            print(f"  p{num}: FAILED {e}", flush=True)
        save_page(rdir, num, content)
        pages_out[num] = content
        print(f"  p{num}: DONE {len(content)} chars in {time.time()-t0:.1f}s", flush=True)

    doc.close()
    wanted = {i + 1 for i in indices}
    final = assemble({p: pages_out[p] for p in sorted(wanted) if p in pages_out})
    out_path.write_text(final, encoding="utf-8")
    meta = {
        "model": args.model if client is not None else "tesseract-ocr-only",
        "synth_model": args.synthesize,
        "ocr_mode": ocr_mode,
        "ocr_stamp": bool(args.ocr_stamp or args.synthesize),
        "pages": sorted(wanted),
        "elapsed_sec": round(time.time() - t_doc, 1),
        "page_max": args.page_max,
        "tile_max": args.tile_max,
        "num_predict": args.num_predict,
        "output": str(out_path),
    }
    out_path.with_suffix(out_path.suffix + ".meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"\nDone: {out_path} ({meta['elapsed_sec']}s)", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
