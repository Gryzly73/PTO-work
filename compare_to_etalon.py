#!/usr/bin/env python3
"""Грубая сверка вывода Ollama с эталонными .ref.md (без score_real_testset.py).

Метрики:
  - token_recall: доля эталонных токенов, найденных в гипотезе
  - key_phrase_hit: доля ключевых фраз/кодов из эталона
  - char_ratio: len(hyp)/len(ref)

Пример:
  python compare_to_etalon.py out_3pages.md --pages 1,3,5
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DEFAULT_MAP = {
    1: "ИДЕАЛ_19_PB_p82.ref.md",
    2: "ИДЕАЛ_21_ODI_p14.ref.md",
    3: "ИДЕАЛ_08_KR1_p53.ref.md",
    4: "ИДЕАЛ_10_KR3_p59.ref.md",
    5: "ИДЕАЛ_12_KR5_p14.ref.md",
    6: "ИДЕАЛ_11_KR4_p59.ref.md",
}

TOKEN_RE = re.compile(
    r"[0-9]+(?:[.,][0-9]+)?|[A-Za-zА-Яа-яЁё]{2,}(?:[-/][A-Za-zА-Яа-яЁё0-9]+)*",
    re.UNICODE,
)

KEY_PATTERNS = [
    re.compile(r"\d{2}-[А-ЯA-Z]{2,5}-\d/\d{2}-[А-ЯA-Z0-9]+"),  # шифр
    re.compile(r"ИГЭ-?\d+"),
    re.compile(r"DN\s?\d{2,4}", re.I),
    re.compile(r"СКВ\.?\s?\d+", re.I),
    re.compile(r"\d{2,4}/[А-ЯA-Z]"),
    re.compile(r"КУРСКРЕГИОНПРОЕКТ|КУРСКРЕГИОНПРОЕКТ", re.I),
    re.compile(r"Жуковский", re.I),
    re.compile(r"Насосная", re.I),
    re.compile(r"Сальников", re.I),
    # коды экспликации: 1а, 1б, 1ш1, 1т1-1т3
    re.compile(r"\b\d{1,2}[а-яА-Яa-zA-Z]\d{0,2}(?:\s*[-–]\s*\d{1,2}[а-яА-Яa-zA-Z]?\d{0,2})?\b"),
]

# Стоп-слова для soft-match ключевых фраз (не требуем их в hyp)
STOP_KEY_TOKENS = frozenset(
    {
        "РАСПОЛОЖЕНА",
        "РАСПОЛОЖЕН",
        "РАСПОЛОЖЕНЫ",
        "РАСПОЛОЖЕНО",
        "НАХОДИТСЯ",
        "НАХОДЯТСЯ",
        "ДЛЯ",
        "ПОД",
        "НАД",
        "ИЗ",
        "ПРИ",
        "ИЛИ",
        "ТАКЖЕ",
        "РЯДОМ",
        "ЧАСТИ",
        "УГЛУ",
        "ВЪЕЗДА",
        "ЮГО",
        "ЗАПАДНОЙ",
        "СЕВЕРО",
        "ВОСТОЧНОМ",
    }
)


def find_etalon_dir() -> Path:
    for p in ROOT.iterdir():
        if p.is_dir() and p.name.startswith("ЭТАЛОН"):
            return p
    raise FileNotFoundError("Папка ЭТАЛОН не найдена")


def normalize(text: str) -> str:
    text = re.sub(r"<think>[\s\S]*?</think>", " ", text, flags=re.I)
    text = re.sub(r"<think>[\s\S]*$", " ", text, flags=re.I)
    text = text.replace("ё", "е").replace("Ё", "Е")
    text = re.sub(r"\s+", " ", text).strip().upper()
    return text


def canon_code(s: str) -> str:
    """1Б / 1б / 1б1 → единый вид для матча."""
    s = s.replace("ё", "е").replace("Ё", "Е")
    s = re.sub(r"\s+", "", s)
    return s.upper()


def tokens(text: str) -> set[str]:
    return {t.upper() for t in TOKEN_RE.findall(text) if len(t) >= 2}


def extract_keys(text: str) -> set[str]:
    found: set[str] = set()
    for pat in KEY_PATTERNS:
        for m in pat.findall(text):
            raw = m if isinstance(m, str) else str(m)
            found.add(canon_code(raw) if re.match(r"^\d", raw) else raw.upper())
    # markdown-ячейки с кодом объекта: | 1б | Мойка ... |
    for m in re.finditer(
        r"\|\s*(\d{1,2}[а-яА-Яa-zA-Z]\d{0,2})\s*\|\s*([^|]+)\|",
        text,
    ):
        code = canon_code(m.group(1))
        name = m.group(2).strip()
        found.add(code)
        if len(name) >= 4:
            found.add(f"{code}: {name}".upper()[:80])
    # bullet-описания эталона: **1Б:** МОЙКА...
    for m in re.finditer(
        r"\*\*\s*(\d{1,2}[а-яА-Яa-zA-Z]\d{0,2})\s*:\*\*\s*([^*\n]+)",
        text,
        flags=re.I,
    ):
        code = canon_code(m.group(1))
        name = m.group(2).strip()
        found.add(code)
        if len(name) >= 4:
            found.add(f"{code}: {name}".upper()[:80])
    # короткие значимые строки заголовков
    for line in text.splitlines():
        s = line.strip()
        if 12 <= len(s) <= 80 and not s.startswith("|") and not s.startswith("#"):
            if re.search(r"[А-Яа-я]{4,}", s) and not s.startswith("*"):
                found.add(s.upper()[:80])
    return found


def key_matched(key: str, hyp_n: str, hyp_tok: set[str]) -> bool:
    """Гибкий матч: точная подстрока ИЛИ код+существенные токены названия."""
    k = key.upper().replace("Ё", "Е")
    if k in hyp_n:
        return True
    # "1Б: МОЙКА ДЛЯ АВТОМОБИЛЕЙ..." → код + ключевые слова
    if ":" in k:
        code, rest = k.split(":", 1)
        code = canon_code(code.strip())
        if code and code not in hyp_n and code not in hyp_tok:
            # код может быть как 1Б или 1б в таблице без двоеточия
            if code not in hyp_n:
                return False
        name_toks = {
            t
            for t in tokens(rest)
            if len(t) >= 4 and t not in STOP_KEY_TOKENS
        }
        if not name_toks:
            return code in hyp_n
        # достаточно ≥50% существенных токенов названия
        hit = sum(1 for t in name_toks if t in hyp_tok or t in hyp_n)
        return hit >= max(1, (len(name_toks) + 1) // 2)
    # короткий код объекта
    if re.match(r"^\d{1,2}[А-ЯA-Z]\d{0,2}$", k):
        return k in hyp_n
    # заголовок: ≥70% токенов
    kt = {t for t in tokens(k) if len(t) >= 3 and t not in STOP_KEY_TOKENS}
    if not kt:
        return k in hyp_n
    hit = sum(1 for t in kt if t in hyp_tok)
    return hit >= max(1, int(len(kt) * 0.7 + 0.999))


def split_pages(md: str) -> dict[int, str]:
    parts = re.split(r"(?m)^## Страница\s+(\d+)\s*$", md)
    # parts: [preamble, num, body, num, body, ...]
    out: dict[int, str] = {}
    if len(parts) < 3:
        out[1] = md
        return out
    for i in range(1, len(parts), 2):
        num = int(parts[i])
        body = parts[i + 1] if i + 1 < len(parts) else ""
        out[num] = body
    return out


def score(ref: str, hyp: str) -> dict:
    ref_n, hyp_n = normalize(ref), normalize(hyp)
    ref_tok, hyp_tok = tokens(ref_n), tokens(hyp_n)
    if not ref_tok:
        recall = 0.0
    else:
        recall = len(ref_tok & hyp_tok) / len(ref_tok)

    ref_keys = extract_keys(ref)
    hits = {k for k in ref_keys if key_matched(k, hyp_n, hyp_tok)}
    key_hit = (len(hits) / len(ref_keys)) if ref_keys else 0.0

    return {
        "token_recall": round(recall * 100, 1),
        "key_phrase_hit": round(key_hit * 100, 1),
        "ref_tokens": len(ref_tok),
        "hyp_tokens": len(hyp_tok),
        "overlap_tokens": len(ref_tok & hyp_tok),
        "ref_keys": len(ref_keys),
        "hit_keys": len(hits),
        "char_ratio": round(len(hyp) / max(1, len(ref)), 2),
        "sample_misses": sorted(list(ref_keys - hits))[:12],
        "sample_hits": sorted(list(hits))[:12],
    }


def safe_print(line: str) -> None:
    """Печать, которая не роняет замер об кодировку консоли.

    В примерах попаданий и промахов встречаются греческие буквы и типографика
    из чертежей, а консоль Windows по умолчанию cp1251. Прежний вариант
    перекодировал через utf-8 и всё равно падал на самом print: замер из трёх
    листов обрывался на последней строке, уже посчитав всё, что нужно.
    """
    encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
    print(line.encode(encoding, "replace").decode(encoding, "replace"))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("hypothesis", type=Path)
    ap.add_argument("--pages", default="1,3,5")
    ap.add_argument("--map", type=Path, default=ROOT / "page_map.json")
    args = ap.parse_args()

    page_map = DEFAULT_MAP
    if args.map.exists():
        page_map = {int(k): v for k, v in json.loads(args.map.read_text(encoding="utf-8")).items()}

    etalon_dir = find_etalon_dir()
    hyp_pages = split_pages(args.hypothesis.read_text(encoding="utf-8"))
    want = [int(x) for x in args.pages.split(",") if x.strip()]

    print(f"Hypothesis: {args.hypothesis}")
    print(f"Etalon dir: {etalon_dir.name}")
    print("-" * 72)

    rows = []
    for p in want:
        ref_name = page_map.get(p)
        if not ref_name:
            print(f"p{p}: нет маппинга")
            continue
        ref_path = etalon_dir / ref_name
        if not ref_path.exists():
            print(f"p{p}: эталон не найден {ref_name}")
            continue
        hyp = hyp_pages.get(p, "")
        if not hyp.strip():
            print(f"p{p}: пустая гипотеза")
            continue
        ref = ref_path.read_text(encoding="utf-8")
        s = score(ref, hyp)
        rows.append((p, ref_name, s))
        print(f"p{p} <-> {ref_name}")
        print(
            f"  token_recall={s['token_recall']}%  "
            f"key_hit={s['key_phrase_hit']}%  "
            f"chars hyp/ref={s['char_ratio']}  "
            f"tok {s['overlap_tokens']}/{s['ref_tokens']}"
        )
        if s["sample_hits"]:
            safe_print(f"  hits: {', '.join(s['sample_hits'][:6])}")
        if s["sample_misses"]:
            safe_print(f"  misses: {', '.join(s['sample_misses'][:6])}")
        print()

    if rows:
        avg_r = sum(r[2]["token_recall"] for r in rows) / len(rows)
        avg_k = sum(r[2]["key_phrase_hit"] for r in rows) / len(rows)
        print("=" * 72)
        print(f"AVG token_recall={avg_r:.1f}%  AVG key_hit={avg_k:.1f}%  (n={len(rows)})")
        print("NB: грубая эвристика, не score_real_testset.py. Смотри hits/misses глазами.")

    out_json = args.hypothesis.with_suffix(".compare.json")
    out_json.write_text(
        json.dumps(
            [{"page": p, "ref": name, **s} for p, name, s in rows],
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"Saved: {out_json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
