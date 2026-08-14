"""Локальный OCR (Tesseract rus+eng) для строительных чертежей."""

from __future__ import annotations

import os
from pathlib import Path

from PIL import Image

DEFAULT_TESSERACT = Path(r"C:\Program Files\Tesseract-OCR\tesseract.exe")
USER_TESSDATA = Path(os.environ.get("LOCALAPPDATA", "")) / "tessdata"
PROG_TESSDATA = Path(r"C:\Program Files\Tesseract-OCR\tessdata")


def resolve_tesseract_cmd() -> str:
    env = os.environ.get("TESSERACT_CMD")
    if env and Path(env).exists():
        return env
    if DEFAULT_TESSERACT.exists():
        return str(DEFAULT_TESSERACT)
    raise FileNotFoundError(
        "Tesseract не найден. Установите UB-Mannheim Tesseract-OCR "
        "или задайте TESSERACT_CMD."
    )


def resolve_tessdata_dir() -> Path:
    env = os.environ.get("TESSDATA_DIR")
    if env and Path(env).exists():
        return Path(env)
    # user copy must have rus (+ preferably eng)
    if (USER_TESSDATA / "rus.traineddata").exists():
        return USER_TESSDATA
    if (PROG_TESSDATA / "rus.traineddata").exists():
        return PROG_TESSDATA
    raise FileNotFoundError(
        f"Нет rus.traineddata. Ожидался файл в {USER_TESSDATA} или {PROG_TESSDATA}."
    )


def ocr_image(
    im: Image.Image,
    *,
    lang: str = "rus+eng",
    psm: int = 6,
) -> str:
    """OCR одного Pillow Image → текст (без пустых хвостов)."""
    import pytesseract

    pytesseract.pytesseract.tesseract_cmd = resolve_tesseract_cmd()
    tessdata = resolve_tessdata_dir()
    if im.mode not in ("RGB", "L"):
        im = im.convert("RGB")
    # без кавычек: на Windows pytesseract+tesseract их ломают
    config = f"--tessdata-dir {tessdata} --psm {psm}"
    raw = pytesseract.image_to_string(im, lang=lang, config=config)
    lines = [ln.rstrip() for ln in raw.splitlines()]
    # убрать полностью пустые / шум из 1-2 символов подряд в конце
    cleaned: list[str] = []
    for ln in lines:
        s = ln.strip()
        if not s:
            if cleaned and cleaned[-1] != "":
                cleaned.append("")
            continue
        cleaned.append(s)
    while cleaned and cleaned[-1] == "":
        cleaned.pop()
    return "\n".join(cleaned).strip()


def ocr_healthcheck() -> str:
    cmd = resolve_tesseract_cmd()
    td = resolve_tessdata_dir()
    return f"tesseract={cmd} tessdata={td} rus={(td / 'rus.traineddata').exists()}"


# Плотные коды из штампа / маркировки (то, что VLM чаще всего роняет)
CODE_PATTERNS = [
    r"\d{2}-[А-ЯA-Z]{2,5}-\d/\d{2}-[А-ЯA-Z0-9]+",  # 28-ХСА-1/25-ПБ
    r"DN\s?\d{2,4}",
    r"ИГЭ-?\d+",
    r"СКВ\.?\s?\d+",
    r"ООО\s*[\"«]?[А-ЯA-ZЁёа-я]{4,}[\"»]?",
    r"КУРСКРЕГИОНПРОЕКТ|КУРСКРЕГ[ИІ]ОНПРОЕКТ",
    r"Жуковский|ЖУКОВСКИЙ",
    r"Сальников|САЛЬНИКОВ|Романов|РОМАНОВ",
    r"Формат\s*А\d",
    r"Стадия\s*[А-ЯA-ZПРпр]",
]


def crop_stamp_regions(im: Image.Image) -> dict[str, Image.Image]:
    """Углы штампа: правый низ (основная надпись) + левый низ (инв.)."""
    w, h = im.size
    return {
        "br": im.crop((int(w * 0.55), int(h * 0.68), w, h)),
        "bl": im.crop((0, int(h * 0.55), int(w * 0.22), h)),
    }


def extract_stamp_codes(text: str) -> list[str]:
    import re

    found: list[str] = []
    seen: set[str] = set()
    for pat in CODE_PATTERNS:
        for m in re.finditer(pat, text, flags=re.IGNORECASE | re.UNICODE):
            s = re.sub(r"\s+", " ", m.group(0)).strip()
            key = s.upper()
            if key in seen or len(s) < 3:
                continue
            seen.add(key)
            found.append(s)
    return found


def ocr_stamp(im: Image.Image) -> tuple[str, list[str]]:
    """OCR углов штампа. Возвращает (сырой текст, извлечённые коды)."""
    parts: list[str] = []
    for name, crop in crop_stamp_regions(im).items():
        # для плотных таблиц штампа PSM 4/6 норм
        txt = ocr_image(crop, psm=6)
        if txt:
            parts.append(f"[{name}]\n{txt}")
    raw = "\n\n".join(parts).strip()
    codes = extract_stamp_codes(raw)
    # также целые строки >12 символов с кириллицей — полезны как факты
    import re

    for ln in raw.splitlines():
        s = ln.strip()
        if s.startswith("["):
            continue
        if 12 <= len(s) <= 120 and re.search(r"[А-Яа-яЁё]{4,}", s):
            if s.upper() not in {c.upper() for c in codes}:
                codes.append(s)
    return raw, codes


def append_stamp_missing(vlm_text: str, stamp_raw: str, codes: list[str]) -> str:
    """Дописать в вывод только то, чего ещё нет в тексте VLM."""
    upper = vlm_text.upper()
    missing = [c for c in codes if c.upper() not in upper]
    if not missing and not stamp_raw:
        return vlm_text
    block_lines = ["", "### Штамп (OCR)", ""]
    if missing:
        block_lines.append("Коды/факты:")
        block_lines.extend(f"- {m}" for m in missing[:40])
        block_lines.append("")
    # короткий сырой хвост (обрезка шума)
    raw_short = "\n".join(
        ln for ln in stamp_raw.splitlines() if ln.strip() and not ln.strip().startswith("[")
    )[:1200]
    if raw_short.strip():
        block_lines.append("Сырой OCR угла:")
        block_lines.append(raw_short.strip())
    return vlm_text.rstrip() + "\n" + "\n".join(block_lines) + "\n"
