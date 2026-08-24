"""Скоринг markdown-выгрузки против ТЕКСТОВОГО СЛОЯ самого PDF.

Зачем: у большинства реальных PDF (не сканов) есть текстовый слой — точный
список того, что на листе действительно написано. Это бесплатный,
детерминированный и неограниченный по числу прогонов эталон: не нужен ни
Gemini, ни ручная разметка, ни подглядывание в конкретные страницы.

Метрики (на страницу):
  token_recall  — доля слов текстового слоя, найденных в выводе  → полнота
  num_recall    — доля ЧИСЕЛ текстового слоя, найденных в выводе → цифры
  num_precision — доля чисел вывода, которые есть в слое         → галлюцинации
  code_recall   — доля «кодов» (шифры, DN/Ду, марки) в выводе    → ключевые метки

Страницы с битым ToUnicode (кракозябры) пропускаются: там слой сам мусор.

Использование:
  python score_vs_pdftext.py <out.md> --pdf <file.pdf> [--pages 8-33,44-49]
  python score_vs_pdftext.py <out.md> --pdf <file.pdf> --json report.json
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

import fitz

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from build_ios2_md import is_garbled_pdf_text  # noqa: E402

# ── нормализация ──────────────────────────────────────────────────────────
_NUM_RE = re.compile(r"\d+(?:[.,]\d+)?")
# код = буквенно-цифровая метка с дефисом/слэшем/цифрой: 28-ХСА-1/25-ИОС2.ПЗ,
# DN100, Ду50, В1, К1, Т3, ГОСТ 12.1.004
_CODE_RE = re.compile(
    r"(?:[A-Za-zА-Яа-яЁё]{1,6}[-–/]?\d{1,4}(?:[./-]\d{1,4})*)"
    r"|(?:\d{2,3}-[A-ZА-Я]{2,4}-[\w./-]+)",
    re.UNICODE,
)


# У CAD-PDF с битым ToUnicode ломаются БУКВЫ (глифы уезжают в чужие блоки
# Unicode), а цифры и часть слов остаются верными. Значит такой слой всё ещё
# годится как ЧАСТИЧНЫЙ эталон: берём только те токены, где нет ни одного
# символа вне обычных диапазонов. Правило универсальное — не зависит от того,
# какой именно шрифт сломался.
def _is_clean_token(tok: str) -> bool:
    for ch in tok:
        o = ord(ch)
        if ch.isdigit() or ch in ".,-/№%()°":
            continue
        if 0x0041 <= o <= 0x007A:  # латиница
            continue
        if 0x0410 <= o <= 0x044F or o in (0x0401, 0x0451):  # кириллица + Ёё
            continue
        return False
    return True


def norm_word(w: str) -> str:
    return w.lower().replace("ё", "е").strip("«»\"'()[]{}.,;:!?—–-")


def norm_num(s: str) -> str:
    """0,001 / 0.001 → 0.001; 1 500 склеивается вызывающей стороной."""
    s = s.replace(",", ".")
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s.lstrip("0") or "0"


def _glue_spaced_numbers(text: str) -> str:
    """«4 169» / «1 500,5» в текстовом слое CAD-PDF — одно число."""
    prev = None
    while prev != text:
        prev = text
        text = re.sub(r"(?<=\d)[   ](?=\d{3}\b)", "", text)
    return text


def tokens_of(text: str, *, clean_only: bool = False) -> tuple[set[str], set[str], set[str]]:
    """→ (слова, числа, коды). Слова — от 3 букв (короткие шумят).

    clean_only: отбросить токены с «поломанными» символами — режим частичного
    эталона для страниц с битым ToUnicode.
    """
    text = _glue_spaced_numbers(text)
    words = {
        w
        for w in (norm_word(x) for x in re.findall(r"[A-Za-zА-Яа-яЁё]{3,}", text))
        if len(w) >= 3 and (not clean_only or _is_clean_token(w))
    }
    # Однозначные числа («раздел 5», «в 3 блоках», индексы формул) слишком шумны:
    # они встречаются в любой прозе и дают ложные срабатывания в обе стороны.
    nums = {
        n
        for n in (norm_num(m.group()) for m in _NUM_RE.finditer(text))
        if len(n.lstrip("-").replace(".", "")) >= 2
    }
    codes = {
        norm_word(m.group())
        for m in _CODE_RE.finditer(text)
        if any(c.isdigit() for c in m.group())
        and any(c.isalpha() for c in m.group())
        and (not clean_only or _is_clean_token(m.group()))
    }
    return words, nums, {c for c in codes if c}


# Блоки, которые САМИ собраны из текстового слоя PDF: включать их в замер —
# значит мерить слой против слоя (всегда 100%). Для честной оценки модели
# такие секции вырезаются (--vlm-only).
_PDF_SOURCED_HEADINGS = (
    "Текст листа (из PDF)",
    "Таблицы листа (из PDF)",
    "Текст вне таблицы (из PDF)",
)


def strip_pdf_sourced(page_md: str) -> str:
    """Убирает секции, взятые из текстового слоя, оставляя только вывод VLM."""
    out: list[str] = []
    skip = False
    for ln in page_md.splitlines():
        m = re.match(r"^(#{2,4})\s+(.*)$", ln)
        if m:
            skip = any(h in m.group(2) for h in _PDF_SOURCED_HEADINGS)
        if not skip:
            out.append(ln)
    return "\n".join(out)


def split_pages_md(text: str) -> dict[int, str]:
    parts = re.split(r"(?m)^##\s+Страница\s+(\d+)\s*$", text)
    out: dict[int, str] = {}
    i = 1
    while i + 1 < len(parts) + 1 and i + 1 <= len(parts):
        try:
            out[int(parts[i])] = parts[i + 1]
        except (ValueError, IndexError):
            pass
        i += 2
    return out


def parse_pages(spec: str, total: int) -> list[int]:
    out: list[int] = []
    for chunk in spec.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if "-" in chunk:
            a, b = chunk.split("-", 1)
            out.extend(range(int(a), min(int(b), total) + 1))
        else:
            out.append(int(chunk))
    return [p for p in dict.fromkeys(out) if 1 <= p <= total]


def score_page(ref_text: str, hyp_text: str, *, partial: bool = False) -> dict:
    rw, rn, rc = tokens_of(ref_text, clean_only=partial)
    hw, hn, hc = tokens_of(hyp_text)
    if partial:
        # При битом ToUnicode «уцелевшие» слова — это ОБРЫВКИ («одст»,
        # «складско»): часть символов слова пережила порчу, часть нет. Модель,
        # написавшая слово правильно, такой обрывок не содержит — сравнивать
        # по словам и кодам нельзя. Цифры порчу переживают, их и меряем.
        rw, rc = set(), set()

    def pct(a: int, b: int) -> float | None:
        """None (=«н/д») когда знаменатель пуст — иначе пустая метрика
        занижает среднее (у таблиц кодов может не быть вовсе)."""
        return round(100.0 * a / b, 1) if b else None

    # числа вывода, которых нет в слое — кандидаты в галлюцинации
    hallucinated = sorted(hn - rn)[:15]
    return {
        "token_recall": pct(len(rw & hw), len(rw)),
        "num_recall": pct(len(rn & hn), len(rn)),
        "num_precision": pct(len(rn & hn), len(hn)),
        "code_recall": pct(len(rc & hc), len(rc)),
        "ref_words": len(rw),
        "ref_nums": len(rn),
        "ref_codes": len(rc),
        "hyp_words": len(hw),
        "hyp_nums": len(hn),
        "missed_words": sorted(rw - hw)[:15],
        "missed_nums": sorted(rn - hn)[:15],
        "missed_codes": sorted(rc - hc)[:10],
        "hallucinated_nums": hallucinated,
        "partial": partial,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("md", help="markdown-выгрузка (## Страница N)")
    ap.add_argument("--pdf", required=True)
    ap.add_argument("--pages", default=None, help="1-33,44-49 (иначе все из MD)")
    ap.add_argument("--json", default=None, help="куда сохранить JSON-отчёт")
    ap.add_argument("--quiet", action="store_true", help="только итоговая строка")
    ap.add_argument(
        "--vlm-only",
        action="store_true",
        help="вырезать секции, собранные из текстового слоя (честный замер модели)",
    )
    ap.add_argument("--label", default=None, help="метка прогона для JSON-отчёта")
    ap.add_argument(
        "--no-repair",
        action="store_true",
        help="не чинить сломанный ToUnicode перед сравнением",
    )
    ap.add_argument(
        "--partial",
        action="store_true",
        help="включить страницы с битым ToUnicode: эталон строится только из "
        "уцелевших токенов (цифры переживают порчу шрифта)",
    )
    args = ap.parse_args()

    md_text = Path(args.md).read_text(encoding="utf-8")
    pages_md = split_pages_md(md_text)
    doc = fitz.open(args.pdf)

    # Сломанный ToUnicode чинится подстановкой, выведенной по читаемым листам
    # (см. deglyph). Если починка проходит гейт качества, повреждённые страницы
    # получают ПОЛНОЦЕННЫЙ эталон — слова и коды, а не только цифры.
    glyph_map: dict[str, str] = {}
    if not args.no_repair:
        try:
            from deglyph import build_mapping, coverage, verify

            # ВАЖНО: отображение строится ТОЛЬКО по самому PDF. Подмешивать сюда
            # проверяемую выгрузку нельзя — эталон стал бы зависеть от гипотезы,
            # и модель с более гладким текстом получала бы более полный эталон.
            _m = build_mapping(doc)
            if _m:
                _c, _v = coverage(doc, _m), verify(doc, _m)
                # см. build_ios2_md: словарная доля занижена там, где
                # подстановка достроена сдвигом, поэтому гейт — по доле
                # полностью раскодированных слов.
                _clean = _v.get("pct_clean", _v.get("pct")) or 0
                if (_c["pct"] or 0) >= 90 and _clean >= 90:
                    glyph_map = _m
                    if not args.quiet:
                        print(
                            f"слой починен: {_c['glyphs']} глифов, "
                            f"{_c['pct']}% символов → битые страницы "
                            f"мерятся полноценно"
                        )
        except Exception as e:
            if not args.quiet:
                print(f"починка слоя недоступна: {e}")
    want = (
        parse_pages(args.pages, doc.page_count)
        if args.pages
        else sorted(pages_md.keys())
    )

    rows: list[dict] = []
    skipped: list[int] = []
    for p in want:
        if p not in pages_md:
            continue
        ref = doc[p - 1].get_text()
        if len(ref.strip()) < 40:
            skipped.append(p)
            continue
        if glyph_map and is_garbled_pdf_text(ref):
            fixed = "".join(glyph_map.get(c, c) for c in ref)
            if not is_garbled_pdf_text(fixed):
                ref = fixed
        partial = is_garbled_pdf_text(ref)
        if partial and not args.partial:
            skipped.append(p)
            continue
        hyp = pages_md[p]
        if args.vlm_only:
            hyp = strip_pdf_sourced(hyp)
        r = score_page(ref, hyp, partial=partial)
        # частичный эталон слишком беден → не засоряем отчёт (у битых страниц
        # эталон состоит только из чисел, поэтому порог ниже)
        if partial and r["ref_nums"] < 5:
            skipped.append(p)
            continue
        r["page"] = p
        rows.append(r)
    doc.close()

    if not args.quiet:
        print(f"MD:  {args.md}")
        print(f"PDF: {args.pdf}")
        print("-" * 78)
        print(
            f"{'стр':>4} {'слова':>7} {'числа':>7} {'точн.ч':>7} "
            f"{'коды':>6}   потеряно (примеры)"
        )
        def fmt(v: float | None, w: int = 6) -> str:
            return f"{v:>{w}.1f}%" if v is not None else f"{'н/д':>{w+1}}"

        for r in rows:
            miss = ", ".join((r["missed_codes"] or r["missed_words"])[:4])
            mark = "~" if r.get("partial") else " "
            print(
                f"{r['page']:>3}{mark} {fmt(r['token_recall'])} {fmt(r['num_recall'])} "
                f"{fmt(r['num_precision'])} {fmt(r['code_recall'], 5)}   {miss[:44]}"
            )
        if any(r.get("partial") for r in rows):
            print("\n~ — частичный эталон (битый ToUnicode: только уцелевшие токены)")
        if skipped:
            print(f"\nпропущены (битый/пустой текстовый слой): {skipped}")

    avg: dict[str, float | None] = {}
    if rows:
        for k in ("token_recall", "num_recall", "num_precision", "code_recall"):
            vals = [r[k] for r in rows if r[k] is not None]
            avg[k] = round(sum(vals) / len(vals), 1) if vals else None
        print("=" * 78)
        print(
            f"AVG слова={avg['token_recall']}%  числа={avg['num_recall']}%  "
            f"точность чисел={avg['num_precision']}%  коды={avg['code_recall']}%  "
            f"(n={len(rows)}{', vlm-only' if args.vlm_only else ''})"
        )
    else:
        print("нет страниц с пригодным текстовым слоем")

    if args.json:
        Path(args.json).write_text(
            json.dumps(
                {
                    "md": str(args.md),
                    "pdf": str(args.pdf),
                    "label": args.label or Path(args.md).parent.name,
                    "vlm_only": args.vlm_only,
                    "avg": avg,
                    "pages": rows,
                    "skipped": skipped,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        print(f"Saved: {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
