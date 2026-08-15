"""Восстановление текстового слоя PDF со сломанным ToUnicode.

Проблема (обычная для CAD-PDF): встроенный шрифт отдаёт глифы, которым в
таблице ToUnicode сопоставлены неверные коды. Слой выглядит кракозябрами
(«ǜодерǱаǸие» вместо «Содержание»), хотя структура текста цела.

Наблюдение, на котором всё держится: порча — это ПОСИМВОЛЬНАЯ подстановка
один в один. Длина слов сохраняется, часть букв приходит верно, остальные
заменены чужими глифами. Проверено на документе ИОС2: ноль конфликтов.

Отсюда решение: подобрать отображение «чужой глиф → настоящая буква», решая
задачу как кроссворд. Ключ берём из самого документа — со страниц, где слой
исправен: там та же терминология и тот же язык. Ни одного правила, привязанного
к конкретному файлу; на любом другом PDF механизм работает так же, а если
подобрать отображение не удалось, функция честно возвращает пустой словарь и
конвейер продолжает опираться на VLM.

Использование:
    from deglyph import build_mapping, decode
    mapping = build_mapping(doc)
    fixed = decode(page.get_text(), mapping)
"""

from __future__ import annotations

import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

import fitz

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from build_ios2_md import is_garbled_pdf_text  # noqa: E402

WORD_RE = re.compile(r"[^\W\d_]{3,}", re.UNICODE)
MIN_VOCAB = 40  # меньше — словарь слишком беден, подбор ненадёжен


def is_broken_char(ch: str) -> bool:
    """Символ не из обычных диапазонов → это подменённый глиф."""
    o = ord(ch)
    if ch.isspace() or ch.isdigit() or not ch.isalpha():
        return False
    if 0x0041 <= o <= 0x007A:  # латиница
        return False
    if 0x0410 <= o <= 0x044F or o in (0x0401, 0x0451):  # кириллица
        return False
    return True


def _vocab_from_clean_pages(doc: fitz.Document, extra_text: str = "") -> Counter:
    """Слова со страниц, где слой исправен (+ необязательный текст VLM)."""
    vocab: Counter = Counter()
    for i in range(doc.page_count):
        t = doc[i].get_text("text")
        if is_garbled_pdf_text(t):
            continue
        for w in WORD_RE.findall(t):
            if not any(is_broken_char(c) for c in w):
                vocab[w] += 1
    for w in WORD_RE.findall(extra_text):
        if not any(is_broken_char(c) for c in w):
            vocab[w] += 1
    return vocab


def _garbled_words(doc: fitz.Document) -> Counter:
    out: Counter = Counter()
    for i in range(doc.page_count):
        t = doc[i].get_text("text")
        if not is_garbled_pdf_text(t):
            continue
        for w in WORD_RE.findall(t):
            if any(is_broken_char(c) for c in w):
                out[w] += 1
    return out


def build_mapping(doc: fitz.Document, extra_text: str = "") -> dict[str, str]:
    """Подбирает «чужой глиф → буква», решая кроссворд по словарю документа."""
    vocab = _vocab_from_clean_pages(doc, extra_text)
    if len(vocab) < MIN_VOCAB:
        return {}
    words = _garbled_words(doc)
    if not words:
        return {}

    by_len: dict[int, list[str]] = defaultdict(list)
    for w in vocab:
        by_len[len(w)].append(w)

    mapping: dict[str, str] = {}
    used: set[str] = set()  # подстановка взаимно однозначна

    for _ in range(12):  # итерации: новые пары открывают новые слова
        votes: dict[str, Counter] = defaultdict(Counter)
        support: dict[str, dict[str, set]] = defaultdict(lambda: defaultdict(set))
        for gw in words:
            cand = [
                v
                for v in by_len.get(len(gw), ())
                if all(
                    (mapping.get(g, g) == t) if not is_broken_char(g) or g in mapping
                    else True
                    for g, t in zip(gw, v)
                )
            ]
            if len(cand) != 1:
                continue  # неоднозначно — ждём следующей итерации
            for g, t in zip(gw, cand[0]):
                if is_broken_char(g) and g not in mapping:
                    votes[g][t] += 1
                    support[g][t].add(gw)
        new = 0
        for g, cnt in votes.items():
            (t, n), = cnt.most_common(1)
            if t in used:
                continue
            others = sum(cnt.values()) - n
            # Одного совпадения мало: короткое слово легко совпадает случайно
            # («?ата» → и «Дата», и «лата»). Требуем либо два независимых
            # слова-свидетеля, либо одно длинное — там случайность исключена.
            witnesses = support[g][t]
            strong = len(witnesses) >= 2 or max(map(len, witnesses)) >= 6
            if not strong or n < 2 * others:
                continue
            mapping[g] = t
            used.add(t)
            new += 1
        if not new:
            break
    return mapping


def verify(doc: fitz.Document, mapping: dict[str, str], extra_text: str = "") -> dict:
    """Доля раскодированных слов, которые нашлись в словаре документа.

    Это честный признак того, что отображение верное: при ошибочной паре
    слова получаются несуществующими и доля падает."""
    vocab = set(_vocab_from_clean_pages(doc, extra_text))
    words = _garbled_words(doc)
    hit = miss = 0
    bad_examples: list[str] = []
    for gw, freq in words.items():
        dec = decode(gw, mapping)
        if any(is_broken_char(c) for c in dec):
            continue  # слово раскодировано не полностью — не в счёт
        if dec in vocab or dec.lower() in {v.lower() for v in vocab}:
            hit += freq
        else:
            miss += freq
            if len(bad_examples) < 12:
                bad_examples.append(f"{gw}→{dec}")
    tot = hit + miss
    return {
        "checked": tot,
        "in_vocab": hit,
        "pct": round(100.0 * hit / tot, 1) if tot else None,
        "examples_out_of_vocab": bad_examples,
    }


def decode(text: str, mapping: dict[str, str]) -> str:
    return "".join(mapping.get(c, c) for c in text) if mapping else text


def coverage(doc: fitz.Document, mapping: dict[str, str]) -> dict:
    """Насколько полно отображение чинит документ (для отчётов и гейтов)."""
    total = fixed = 0
    for i in range(doc.page_count):
        t = doc[i].get_text("text")
        if not is_garbled_pdf_text(t):
            continue
        for c in t:
            if is_broken_char(c):
                total += 1
                if c in mapping:
                    fixed += 1
    return {
        "broken_chars": total,
        "recovered": fixed,
        "pct": round(100.0 * fixed / total, 1) if total else None,
        "glyphs": len(mapping),
    }


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("pdf")
    ap.add_argument("--show", default="", help="показать страницы: 3,4,6")
    args = ap.parse_args()
    d = fitz.open(args.pdf)
    m = build_mapping(d)
    cov = coverage(d, m)
    print(f"подобрано глифов: {cov['glyphs']}")
    print(f"повреждённых символов: {cov['broken_chars']}, "
          f"восстановлено: {cov['recovered']} ({cov['pct']}%)")
    if m:
        print("отображение: " + ", ".join(f"{g}→{t}" for g, t in sorted(m.items())))
        v = verify(d, m)
        print(f"проверка: слов раскодировано {v['checked']}, "
              f"нашлось в словаре {v['in_vocab']} ({v['pct']}%)")
        if v["examples_out_of_vocab"]:
            print("  не из словаря: " + ", ".join(v["examples_out_of_vocab"]))
    for spec in filter(None, args.show.split(",")):
        p = int(spec)
        raw = d[p - 1].get_text()
        print(f"\n─── стр. {p} ───")
        print("было:  ", " ".join(raw.split())[:200])
        print("стало: ", " ".join(decode(raw, m).split())[:200])
    d.close()
