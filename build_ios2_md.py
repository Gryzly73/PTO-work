#!/usr/bin/env python3
"""Собирает читаемый итоговый .md для произвольного PDF из результатов VLM-прогона
(hf_api_bench.py) + текстового слоя PDF (только если он реально читаем).

Важно: у CAD-PDF часто битый ToUnicode у шрифтов — тогда «Текст листа» выглядит
как кракозябры и модели бесполезен. Такие страницы помечаем и опираемся на VLM.

Каждая страница итога:
  ## Страница N
  ### Описание листа (VLM)              — Pass-A (главное для понимания чертежа)
  ### Извлечение по фрагментам (VLM)    — Pass-B (для чертежей/схем по умолчанию)
  ### Текст листа (из PDF)              — только если слой читаемый

Использование:
  python build_ios2_md.py --run hf_runs/<stamp>_qwen3vl-32b_twopass --pdf "<file>.pdf" -o out_final.md
"""
from __future__ import annotations

import argparse
import re
from collections import Counter
from pathlib import Path

import fitz  # PyMuPDF

ROOT = Path(__file__).resolve().parent

# Символы, типичные для «битого» ToUnicode CAD-шрифтов (не кириллица/латиница/цифры)
_GARBLED_RE = re.compile(
    r"[\u0180-\u024F\u1E00-\u1EFF\u2C60-\u2C7F\uA720-\uA7FF"
    r"\u0100-\u017F\u00C0-\u00FF\u0370-\u03FF]"
)
_CYR_RE = re.compile(r"[А-Яа-яЁё]")


def split_run_pages(md: str) -> dict[int, str]:
    """Режет out.md прогона на страницы по «## Страница N»."""
    parts = re.split(r"(?m)^##\s+Страница\s+(\d+)\s*$", md)
    out: dict[int, str] = {}
    for i in range(1, len(parts), 2):
        out[int(parts[i])] = parts[i + 1] if i + 1 < len(parts) else ""
    return out


def extract_pass(page_body: str) -> tuple[str, str, str]:
    """Возвращает (pass_0, pass_a, pass_b) из тела страницы прогона."""
    pass_0, pass_a, pass_b = "", "", ""
    z = re.search(
        r"###\s*PASS-0[^\n]*\n(.*?)(?=\n###\s*PASS-A|\n###\s*PASS-B|\Z)",
        page_body,
        re.S,
    )
    if z:
        pass_0 = z.group(1).strip()
    a = re.search(
        r"###\s*PASS-A[^\n]*\n(.*?)(?=\n###\s*PASS-B|\Z)", page_body, re.S
    )
    if a:
        pass_a = a.group(1).strip()
    b = re.search(r"###\s*PASS-B[^\n]*\n(.*)\Z", page_body, re.S)
    if b:
        pass_b = b.group(1).strip()
    return pass_0, pass_a, pass_b


def normalize_pdf_text(raw: str) -> str:
    """Чистит текстовый слой PDF: пустые строки + спам коротких CAD-меток.

    На планах сетей одна метка (W1, V1, К1, В1…) стоит на каждом сегменте —
    get_text() выгружает её сотни раз подряд. Для Q&A это шум: схлопываем.
    """
    lines = [ln.rstrip() for ln in raw.splitlines()]
    # 1) подряд идущие одинаковые короткие метки → оставить 2 + счётчик
    collapsed: list[str] = []
    i = 0
    while i < len(lines):
        ln = lines[i]
        key = ln.strip()
        if not key:
            # пустые — не больше одной подряд
            if not collapsed or collapsed[-1] != "":
                collapsed.append("")
            i += 1
            continue
        j = i + 1
        while j < len(lines) and lines[j].strip() == key:
            j += 1
        run = j - i
        # короткая метка/код (≤20 символов, без пробелов или одно «слово»)
        is_label = len(key) <= 20 and (
            " " not in key or re.fullmatch(r"[A-Za-zА-Яа-яЁё0-9./∅φ×\-]+", key)
        )
        if is_label and run >= 4:
            collapsed.append(key)
            collapsed.append(key)
            collapsed.append(f"(×{run} на плане — схлопнуто)")
        elif run >= 8 and len(key) <= 40:
            # длиннее, но всё равно явный копипаст
            collapsed.append(key)
            collapsed.append(f"(×{run} — схлопнуто)")
        else:
            collapsed.extend(lines[i:j])
        i = j

    # 2) сводка по коротким меткам, если их всё ещё много разбросано
    label_re = re.compile(
        r"^(?:[A-Za-zА-Яа-яЁё]\d{0,3}(?:\.\d+)?|ДРП|ПГ\d+|УП\d+(?:\.\d+)?)$"
    )
    counts = Counter(
        ln.strip() for ln in collapsed if label_re.match(ln.strip() or "")
    )
    heavy = {k: c for k, c in counts.items() if c >= 15}
    if heavy:
        # оставляем каждое тяжёлое слово максимум 2 раза в теле
        seen: dict[str, int] = {}
        filtered: list[str] = []
        for ln in collapsed:
            k = ln.strip()
            if k in heavy:
                seen[k] = seen.get(k, 0) + 1
                if seen[k] <= 2:
                    filtered.append(ln)
                continue
            filtered.append(ln)
        summary = ", ".join(
            f"{k} (×{c})" for k, c in sorted(heavy.items(), key=lambda x: -x[1])
        )
        filtered.append("")
        filtered.append(f"Метки сетей на плане (сводка): {summary}.")
        collapsed = filtered

    text = "\n".join(collapsed).strip()
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text


def is_garbled_pdf_text(text: str) -> bool:
    """True, если текстовый слой PDF повреждён (битый ToUnicode) и нечитаем.

    Эвристика: много «чужих» букв (латиница расширенная / PUA-подобные из CAD)
    при малой доле нормальной кириллицы.
    """
    if not text or len(text.strip()) < 20:
        return False
    cyr = len(_CYR_RE.findall(text))
    garbled = len(_GARBLED_RE.findall(text))
    letters = cyr + garbled
    if letters < 30:
        return False
    # если «битых» больше ~25% от букв — слой бесполезен для Q&A
    return (garbled / letters) >= 0.25


def is_drawing_page(page: fitz.Page) -> bool:
    """Чертёж/схема: крупнее обычного A4 (текст/содержание).

    A4 ≈ 595×842; A3 ≈ 842×1191; генпланы ≫ 2000.
    """
    w, h = page.rect.width, page.rect.height
    longest = max(w, h)
    shortest = min(w, h)
    # A3 и больше, либо очень широкий лист
    return longest >= 1100 or (longest >= 900 and shortest >= 700)


def build(
    run_dir: Path,
    pdf_path: Path,
    *,
    include_tiles: bool | None = None,
) -> str:
    out_md = run_dir / "out.md"
    if not out_md.exists():
        raise FileNotFoundError(f"нет {out_md} — прогон ещё не завершён?")
    run_pages = split_run_pages(out_md.read_text(encoding="utf-8"))

    doc = fitz.open(str(pdf_path))
    total = doc.page_count

    blocks: list[str] = []
    header = (
        f"# {pdf_path.stem}\n\n"
        f"Итоговый Markdown: подробное описание листа (VLM) + текстовый слой PDF "
        f"(только если читаемый). На чертежах/схемах добавлены фрагменты VLM. "
        f"Графические символы — словами, без эмодзи. Всего страниц: {total}.\n"
    )
    blocks.append(header)

    # ── шифр документа из листов с исправным слоем ────────────────────────
    # В комплекте рабочей документации шифр в штампе один и тот же на всех
    # листах. Значит его можно прочитать там, где текстовый слой цел, и
    # подсказать на листах, где слой повреждён и VLM шифр не разобрала.
    # Механизм универсален: ничего не выдумывается, источник подписан, а если
    # шифров в документе несколько (сшивка разделов) — подсказка не даётся.
    # ── починка сломанного ToUnicode ──────────────────────────────────────
    # Порча оказалась посимвольной подстановкой: отображение подбирается по
    # словарю читаемых листов (плюс текст VLM как подсказка) и делает слой
    # повреждённых страниц точным. Включаем только при высоком качестве
    # подбора — иначе продолжаем опираться на VLM, как раньше.
    glyph_map: dict[str, str] = {}
    glyph_stats: dict = {}
    try:
        from deglyph import build_mapping, coverage, verify

        _vlm_text = "\n".join(run_pages.values())
        _m = build_mapping(doc, _vlm_text)
        if _m:
            _cov = coverage(doc, _m)
            _ver = verify(doc, _m, _vlm_text)
            glyph_stats = {"cov": _cov, "ver": _ver}
            # Гейт по прямому результату: доля битых слов, в которых после
            # починки не осталось подменённых глифов. Словарная доля
            # (`ver["pct"]`) для этого не годится — подстановка достраивается
            # сдвигом кодовой таблицы и открывает редкие заглавные термины
            # («ХОЛДИНГ», «ЭКСПЛИКАЦИЯ»), которых в словаре документа нет по
            # природе: чем лучше работает починка, тем ниже была бы оценка.
            _clean = _ver.get("pct_clean", _ver.get("pct")) or 0
            if (_cov["pct"] or 0) >= 90 and _clean >= 90:
                glyph_map = _m
                print(
                    f"текстовый слой починен: {_cov['glyphs']} глифов, "
                    f"{_cov['pct']}% символов, слов раскодировано {_clean}% "
                    f"(из них в словаре {_ver['pct']}%)"
                )
            else:
                print(
                    f"починка слоя отклонена гейтом: символов {_cov['pct']}%, "
                    f"слов раскодировано {_clean}% (нужно 90% и 90%)"
                )
    except Exception as e:
        print(f"починка слоя пропущена: {e}")

    # отпечатки рамки со штампом — считаются один раз на документ
    try:
        from pdf_tables import frame_signatures

        frames = frame_signatures(doc)
        if frames:
            print(f"рамка листа распознана и исключена из таблиц ({len(frames)} шт.)")
    except Exception as e:
        frames = set()
        print(f"распознавание рамки пропущено: {e}")

    doc_codes: dict[str, int] = {}
    code_re = re.compile(r"\b\d{2,3}-[А-ЯЁA-Z]{2,5}-[\w./-]{3,}", re.UNICODE)
    for _p in range(total):
        _t = doc[_p].get_text("text")
        if is_garbled_pdf_text(_t):
            continue
        for _m in code_re.finditer(_t):
            _c = _m.group().rstrip(".,;")
            doc_codes[_c] = doc_codes.get(_c, 0) + 1
    # берём шифр, встречающийся на большинстве читаемых листов
    main_code = None
    if doc_codes:
        _best, _cnt = max(doc_codes.items(), key=lambda kv: kv[1])
        if _cnt >= 3 and _cnt >= 0.5 * sum(doc_codes.values()):
            main_code = _best
    if main_code:
        print(f"шифр документа из штампов: {main_code} ({doc_codes[main_code]} листов)")

    # Импорт локальный: deglyph сам импортирует этот модуль (is_garbled_pdf_text),
    # на уровне модуля вышел бы цикл.
    try:
        from deglyph import is_broken_any, page_raw_text
    except Exception:  # pragma: no cover — без deglyph работаем как раньше
        from deglyph import is_broken_char as is_broken_any  # type: ignore

        def page_raw_text(page):
            return page.get_text("text")

    skipped_garbled = 0
    for n in range(1, total + 1):
        body = run_pages.get(n, "")
        pass_0, pass_a, pass_b = extract_pass(body)
        page = doc[n - 1]
        raw_text = page_raw_text(page)
        garbled = is_garbled_pdf_text(raw_text)
        repaired = False
        if glyph_map:
            fixed = "".join(glyph_map.get(c, c) for c in raw_text)
            # Чиним по наличию подменённых символов, а не по вердикту «весь
            # лист кракозябры». На плане штамп и подписи набраны разными
            # шрифтами: доля порчи мала, лист проходит как читаемый — и
            # отметки «112.25» так и остаются управляющими кодами, которые
            # normalize_pdf_text молча выбросит.
            before = sum(1 for c in raw_text if is_broken_any(c))
            after = sum(1 for c in fixed if is_broken_any(c))
            better = after < before and (not garbled or not is_garbled_pdf_text(fixed))
            if better:
                raw_text, repaired = fixed, True
                garbled = is_garbled_pdf_text(raw_text)
        pdf_text = normalize_pdf_text(raw_text)
        drawing = is_drawing_page(page)
        use_tiles = (
            include_tiles if include_tiles is not None else drawing
        )

        chunk = [f"## Страница {n}\n"]
        if pass_0:
            chunk.append("### Паспорт листа\n")
            chunk.append(pass_0 + "\n")
        if pass_a:
            chunk.append("### Описание листа (VLM)\n")
            chunk.append(pass_a + "\n")
        if use_tiles and pass_b:
            # для планов/схем секция = пространственные фрагменты, не «сырой OCR»
            tile_title = (
                "### Пространственные фрагменты (VLM)\n"
                if "kind: `plan`" in pass_0 or "kind: `scheme`" in pass_0
                else "### Извлечение по фрагментам (VLM)\n"
            )
            chunk.append(tile_title)
            chunk.append(pass_b + "\n")

        # Таблицы из текстового слоя: точная сетка + точные значения без VLM.
        # Ставим ПЕРЕД сырым текстом — читателю нужна структура, а не поток слов.
        if not garbled:
            try:
                from pdf_tables import page_tables_md

                tables_md = page_tables_md(
                    page, glyph_map if repaired else None, frames
                )
            except Exception:
                tables_md = []
            if tables_md:
                chunk.append("### Таблицы листа (из PDF)\n")
                for ti, tmd in enumerate(tables_md, start=1):
                    if len(tables_md) > 1:
                        chunk.append(f"**Таблица {ti}**\n")
                    chunk.append(tmd + "\n")

        if garbled:
            skipped_garbled += 1
            chunk.append("### Текст листа (из PDF)\n")
            chunk.append(
                "*(текстовый слой PDF повреждён — битая кодировка шрифта CAD. "
                "Опирайся на описание VLM выше.)*\n"
            )
            # шифр не выдумываем: помечаем, что он взят из штампов других листов
            if main_code and main_code.lower() not in "\n".join(chunk).lower():
                chunk.append(
                    f"\n*Шифр документа: `{main_code}` — прочитан из штампов "
                    f"других листов этого комплекта, на самом листе слой "
                    f"повреждён.*\n"
                )
        elif pdf_text:
            chunk.append("### Текст листа (из PDF)\n")
            if repaired:
                chunk.append(
                    "*(шрифт листа отдавал неверные коды символов; кодировка "
                    "восстановлена подстановкой, выведенной по читаемым листам "
                    "этого же документа)*\n"
                )
            chunk.append(pdf_text + "\n")
        else:
            chunk.append("### Текст листа (из PDF)\n")
            chunk.append("*(на странице нет текстового слоя)*\n")

        blocks.append("\n".join(chunk))

    doc.close()
    print(f"garbled pdf-text pages skipped: {skipped_garbled}/{total}", flush=True)
    return "\n".join(blocks).rstrip() + "\n"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", type=Path, required=True, help="папка прогона hf_runs/<...>")
    ap.add_argument("--pdf", type=Path, required=True)
    ap.add_argument("-o", "--out", type=Path, default=None)
    ap.add_argument(
        "--include-tiles",
        action="store_true",
        default=None,
        help="Всегда включать Pass-B. По умолчанию — только на чертежах/схемах.",
    )
    ap.add_argument(
        "--no-tiles",
        action="store_true",
        help="Никогда не включать Pass-B.",
    )
    args = ap.parse_args()

    include: bool | None
    if args.no_tiles:
        include = False
    elif args.include_tiles:
        include = True
    else:
        include = None  # auto: только чертежи

    md = build(args.run, args.pdf, include_tiles=include)
    out_path = args.out or (args.run / "final_readable.md")
    out_path.write_text(md, encoding="utf-8")
    print(f"wrote {out_path} ({len(md)} chars, {len(md.splitlines())} lines)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
