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
    """Слова с подменёнными глифами — со ВСЕХ страниц документа.

    Раньше брались только страницы, целиком похожие на кракозябры. Но на
    чертеже штамп и основной текст набраны разными шрифтами: доля порчи мала,
    страница проходит как исправная, и её глифы («ǨКСПЛИКАЦИǪ ǒДАНИǔ» —
    заглавные Э, Я, З, Й) в подстановку не попадали вовсе. Кандидатов лишними
    словами не испортить: подбор всё равно требует единственного совпадения по
    словарю и двух независимых свидетелей.
    """
    out: Counter = Counter()
    for i in range(doc.page_count):
        t = doc[i].get_text("text")
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
    return extend_by_shifts(mapping, detect_shifts(words, vocab))


# Кириллица, в которую имеет смысл раскодировать: буквы + Ё/ё.
_CYR_RANGE = set(range(0x0410, 0x0450)) | {0x0401, 0x0451}

# Меньше стольких слов-свидетелей — совпадение сдвига может быть случайным.
MIN_VOTES_FOR_SHIFT = 3

# Насколько за пределы кодов своих свидетелей распространяется сдвиг. Шрифт
# занимает непрерывный блок, но свидетели покрывают не весь алфавит: запас
# нужен, чтобы достать редкие буквы (Ъ, Ё, заглавные).
SHIFT_SPAN_MARGIN = 96


def detect_shifts(
    words: Counter, vocab: Counter | set
) -> list[tuple[int, int, int]]:
    """Ищет сдвиги кодовой таблицы: [(сдвиг, минимальный код, максимальный код)].

    Порча ToUnicode в CAD-PDF обычно смещает весь кириллический блок шрифта на
    постоянную величину. Шрифтов на листе бывает несколько, и сдвиги у них
    разные: на ИОС2 основной текст смещён на 581, а заголовки вроде
    «Ʉɚɧɚɥɢɡɚɰɢɨɧɧɵɟ ɨɱɢɫɬɧɵɟ ɫɨɨɪɭɠɟɧɢɹ» — на 470.

    Ищем так же, как словарный подбор: битое слово сопоставляем словам той же
    длины из словаря документа. Но здесь не требуем единственного кандидата —
    достаточно, чтобы все подменённые позиции дали ОДНУ дельту. Правильный
    сдвиг наберёт голоса многих независимых слов, случайный — один-два.
    """
    by_len: dict[int, list[str]] = defaultdict(list)
    for word in vocab:
        by_len[len(word)].append(word)

    votes: Counter = Counter()
    span: dict[int, list[int]] = {}
    for gw in words:
        for cand in by_len.get(len(gw), ()):
            deltas = set()
            codes: list[int] = []
            fits = True
            for g, t in zip(gw, cand):
                if is_broken_char(g):
                    deltas.add(ord(t) - ord(g))
                    codes.append(ord(g))
                elif g != t:
                    fits = False
                    break
            if not fits or len(deltas) != 1:
                continue
            shift = deltas.pop()
            votes[shift] += 1
            lo, hi = span.get(shift, (min(codes), max(codes)))
            span[shift] = (min(lo, *codes), max(hi, *codes))

    return [
        (shift, span[shift][0], span[shift][1])
        for shift, n in votes.most_common()
        if n >= MIN_VOTES_FOR_SHIFT
    ]


def extend_by_shifts(
    mapping: dict[str, str], shifts: list[tuple[int, int, int]]
) -> dict[str, str]:
    """Достраивает подстановку найденными сдвигами.

    Словарный подбор открывает только буквы, для которых в документе нашлось
    слово-ключ. Редкие заглавные так и остаются нерасшифрованными:
    «ǨКСПЛИКАЦИǪ ǒДАНИǔ» вместо «ЭКСПЛИКАЦИЯ ЗДАНИЙ» — а это заголовок таблицы,
    ради которой лист и читают. Сдвиг закрывает алфавит целиком.

    Каждый сдвиг действует только рядом с кодами своих свидетелей: шрифты
    занимают разные блоки, и пускать сдвиг одного шрифта на глифы другого
    нельзя. Уже подобранные словарём пары не переписываются — они надёжнее.
    """
    if not shifts:
        return mapping
    extended = dict(mapping)
    for shift, lo, hi in shifts:
        for code in range(lo - SHIFT_SPAN_MARGIN, hi + SHIFT_SPAN_MARGIN + 1):
            ch = chr(code)
            if ch in extended or not is_broken_char(ch):
                continue
            target = code + shift
            if target in _CYR_RANGE:
                extended[ch] = chr(target)
    return extended

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
    # Вторая, независимая мера: доля битых слов, в которых после починки не
    # осталось подменённых глифов. Словарная доля (`pct`) занижена там, где
    # подстановка достроена сдвигом: она открывает редкие заглавные термины
    # («ХОЛДИНГ», «АЛЬЯНС», «ЭКСПЛИКАЦИЯ»), которых в словаре документа нет по
    # природе — они встречаются только в шапках и штампах.
    total_words = sum(words.values())
    decoded_clean = sum(
        freq
        for gw, freq in words.items()
        if not any(is_broken_char(c) for c in decode(gw, mapping))
    )
    return {
        "checked": tot,
        "in_vocab": hit,
        "pct": round(100.0 * hit / tot, 1) if tot else None,
        "decoded": decoded_clean,
        "words": total_words,
        "pct_clean": (
            round(100.0 * decoded_clean / total_words, 1) if total_words else None
        ),
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

# ── Готовое применение: текст листа с уже починенным слоем ──────────────────
#
# Подстановка выводится по всему документу и нужна на каждом листе, а конвейер
# в сервисе считает по листу за вызов и открывает PDF заново. Без кэша таблица
# пересобиралась бы тысячу раз на документ.

_MAPS: dict[str, dict[str, str]] = {}

# Ниже этой доли восстановленных символов подстановке не доверяем: лучше
# оставить кракозябры и отдать лист модели, чем подсунуть выдуманный текст.
MIN_COVERAGE_PCT = 60.0


def map_for_doc(doc: fitz.Document, *, quiet: bool = False) -> dict[str, str]:
    """Подстановка для документа, с кэшем на процесс. {} — чинить нечем."""
    key = getattr(doc, "name", "") or f"id{id(doc)}"
    cached = _MAPS.get(key)
    if cached is not None:
        return cached

    mapping: dict[str, str] = {}
    try:
        mapping = build_mapping(doc)
        if mapping:
            pct = coverage(doc, mapping).get("pct", 0.0)
            if pct < MIN_COVERAGE_PCT:
                if not quiet:
                    print(
                        f"    deglyph: подстановка ненадёжна ({pct:.0f}% символов) — "
                        "текстовый слой оставлен как есть",
                        flush=True,
                    )
                mapping = {}
            elif not quiet:
                print(
                    f"    deglyph: чиню текстовый слой, {len(mapping)} глифов, "
                    f"{pct:.0f}% символов",
                    flush=True,
                )
    except Exception as e:  # порча бывает не только этой природы
        if not quiet:
            print(f"    deglyph skip: {e}", flush=True)
        mapping = {}

    _MAPS[key] = mapping
    return mapping


def page_text_fixed(page, *, quiet: bool = False) -> str:
    """Текстовый слой листа с починкой сломанного ToUnicode.

    Исправный слой возвращается как есть; неисправный — раскодированным, если
    подстановка нашлась и оказалась надёжной. Иначе возвращается исходный
    текст, и решение о нём принимает вызывающий код.
    """
    # sort=True — порядок чтения по координатам, а не по внутреннему потоку
    # файла. Без него страница пояснительной записки начинается со штампа
    # («Изм. Кол.уч, №док., Митрофанов…»), а текст документа идёт после него:
    # для инженера это нечитаемо, а раньше было незаметно, потому что слой
    # никто не показывал целиком.
    raw = page.get_text("text", sort=True) or ""
    if not raw.strip():
        return raw
    broken = sum(1 for ch in raw if is_broken_char(ch))
    if not broken:
        return raw
    # Чиним по наличию битых символов, а не по вердикту «вся страница битая».
    # На чертеже штамп и основной текст часто набраны разными шрифтами: доля
    # порчи мала, страница проходит как исправная, а из слоя вываливается
    # «ǨКСПЛИКАЦИǪ ǒДАНИǔ» — то есть ровно то слово, ради которого лист и читают.
    mapping = map_for_doc(page.parent, quiet=quiet)
    if not mapping:
        return raw
    fixed = decode(raw, mapping)
    left = sum(1 for ch in fixed if is_broken_char(ch))
    return fixed if left < broken else raw
