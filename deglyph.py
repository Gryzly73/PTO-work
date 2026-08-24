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


def is_broken_glyph(ch: str) -> bool:
    r"""Небуквенный подменённый глиф: управляющий код или приватная область.

    Буквы ловит `is_broken_char()`, но порча ими не ограничена: у того же
    шрифта смещены и цифры с пунктуацией, а они приходят кодами вроде `\x14`
    или `\uf02d`. Для человека это невидимый мусор, для конвейера — потерянная
    отметка «112.25» и шифр «28 ХСА 1 25 ИОС2» вместо «28-ХСА-1/25-ИОС2».
    """
    if ch in (chr(9), chr(10), chr(13)):
        return False
    o = ord(ch)
    return o < 0x20 or 0xF000 <= o <= 0xF0FF


def is_broken_any(ch: str) -> bool:
    """Любой подменённый символ — буква или цифра с пунктуацией."""
    return is_broken_char(ch) or is_broken_glyph(ch)


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
    """Подбирает «чужой глиф → символ», решая кроссворд по словарю документа.

    Две ступени: буквы — кроссвордом по словам, цифры и пунктуация — сдвигом
    кодовой таблицы шрифта (`detect_glyph_shifts`). Вторая работает и там, где
    первой делать нечего: буквы листа целы, а отметки и шифр — нет.
    """
    vocab = _vocab_from_clean_pages(doc, extra_text)
    if len(vocab) < MIN_VOCAB:
        return {}
    words = _garbled_words(doc)
    if not words:
        return detect_glyph_shifts(doc, {})

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
    mapping = extend_by_shifts(mapping, detect_shifts(words, vocab))
    # Цифры и пунктуация — отдельной ступенью и поверх готовых букв: сдвиг
    # проверяется совпадением с исправно набранными токенами документа, а их
    # надо сперва починить, иначе сверять не с чем.
    mapping.update(detect_glyph_shifts(doc, mapping))
    return mapping


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

# ── Цифры и пунктуация: сдвиг кодовой таблицы по шрифтам ────────────────────
#
# Буквы чинит кроссворд по словарю: у слова есть форма, по которой его узнают.
# У «2.2» и «112.25» формы нет — в таблице экспликации они стоят поодиночке,
# слова-ключа для них в документе не существует, и кроссворд их не берёт.
#
# Опора у них другая — шрифт. Подменённые коды одного шрифта лежат непрерывным
# блоком и смещены относительно настоящих на одну величину: на ИОС2
# Arial-ItalicMT смещён на +29 (`\x14` → «1», `\x03` → пробел), ISOCPEUR — на
# +31 (`\x0e` → «-», `\x10` → «/»). Величину перебираем и проверяем
# результатом: верный сдвиг превращает битые токены в числа и в токены,
# которые в этом же документе уже набраны исправно. Неверный не даёт ни того,
# ни другого — разрыв в счёте получается кратный, а не на проценты.

_ASCII_LO, _ASCII_HI = 0x20, 0x7E

# Токен целиком из цифр с разделителями: отметка, площадь, номер позиции.
_NUMERIC_TOKEN_RE = re.compile(r"^[0-9]+(?:[.,][0-9]+)*$")

# Вес свидетельств. Совпадение с исправно набранным токеном того же документа
# сильнее, чем «получилось похоже на число»: чисел много и случайный сдвиг
# наберёт их тоже, а попасть в готовый токен ему нечем.
_W_VOCAB, _W_NUMERIC = 3, 1

# Меньше этого веса — свидетельств мало, шрифт не трогаем.
MIN_SHIFT_SCORE = 12

# Во столько раз лучший сдвиг должен обойти второй. Без запаса берём соседний
# сдвиг, который тоже даёт «числа», но не те.
SHIFT_MARGIN = 2.0


def _font_tokens(
    doc: fitz.Document, letter_map: dict[str, str]
) -> tuple[dict[str, Counter], Counter]:
    """Битые токены по шрифтам и словарь исправно набранных токенов.

    Разбор по шрифтам обязателен: на одном листе их несколько, сдвиги у них
    разные, и общая таблица без такого разделения смешала бы «-» одного
    шрифта с «/» другого.
    """
    garbled: dict[str, Counter] = defaultdict(Counter)
    clean: Counter = Counter()
    for i in range(doc.page_count):
        for block in doc[i].get_text("dict").get("blocks", ()):
            for line in block.get("lines", ()):
                for span in line.get("spans", ()):
                    # Буквы к этому моменту уже починены — сравнивать со
                    # словарём нужно то, что реально попадёт в вывод.
                    text = decode(span.get("text", ""), letter_map)
                    for tok in text.split():
                        if any(is_broken_glyph(c) for c in tok):
                            garbled[span.get("font", "?")][tok] += 1
                        elif len(tok) >= 2 and not any(is_broken_char(c) for c in tok):
                            clean[tok] += 1
    return garbled, clean


def _apply_shift(token: str, shift: int) -> str:
    return "".join(
        chr(ord(c) + shift) if is_broken_glyph(c) else c for c in token
    )


def _token_weight(decoded: str, clean: Counter) -> tuple[int, int]:
    """Вес свидетельства: (словарное, числовое).

    Разделены нарочно. «Похоже на число» даёт и соседний сдвиг — цифры лежат
    подряд, и промах на единицу превращает одни цифры в другие. А попасть в
    токен, который в этом же документе уже набран исправно, соседний сдвиг не
    может: там участвует пунктуация, и она смещается вместе с цифрами.
    """
    parts = decoded.split()
    if len(parts) > 1:
        # Подменённый пробел: токен распадается на слова, и каждое из них в
        # документе встречается набранным правильно.
        return (_W_VOCAB, 0) if all(p in clean for p in parts) else (0, 0)
    if decoded in clean:
        return (_W_VOCAB, 0)
    if _NUMERIC_TOKEN_RE.match(decoded):
        return (0, _W_NUMERIC)
    return (0, 0)


def detect_glyph_shifts(
    doc: fitz.Document, letter_map: dict[str, str]
) -> dict[str, str]:
    r"""Подстановка для цифр и пунктуации: {подменённый глиф: символ}.

    Считается по шрифтам, но применяется одной таблицей — `decode()` работает
    посимвольно и о шрифте не знает. Когда два шрифта claim'ят один код с
    разными символами, побеждает тот вариант, за который больше подтверждённых
    словарём вхождений: на ИОС2 так решается спор за `\x10` между «/» шифра и
    «-» слова «хозяйственно-бытового».
    """
    garbled, clean = _font_tokens(doc, letter_map)
    if not clean or not garbled:
        return {}

    # глиф → символ → накопленный вес свидетельств
    claims: dict[str, Counter] = defaultdict(Counter)
    for tokens in garbled.values():
        codes = sorted({ord(c) for tok in tokens for c in tok if is_broken_glyph(c)})
        if not codes:
            continue
        # Сдвиг обязан уложить ВЕСЬ блок шрифта в печатаемый ASCII: это и есть
        # проверка на то, что блок действительно непрерывный и один.
        lo, hi = codes[0], codes[-1]
        if hi - lo > _ASCII_HI - _ASCII_LO:
            continue
        scored = []
        for s in range(_ASCII_LO - lo, _ASCII_HI - hi + 1):
            vocab_hits = numeric_hits = 0
            for tok, freq in tokens.items():
                v, n = _token_weight(_apply_shift(tok, s), clean)
                vocab_hits += freq * v
                numeric_hits += freq * n
            scored.append((vocab_hits, numeric_hits, s))
        if not scored:
            continue
        scored.sort(reverse=True)
        best_vocab, _, shift = scored[0]
        runner_up = scored[1][0] if len(scored) > 1 else 0
        # Гейт — по словарной части: она одна отличает верный сдвиг от соседа.
        if best_vocab < MIN_SHIFT_SCORE or best_vocab < SHIFT_MARGIN * max(runner_up, 1):
            continue
        for tok, freq in tokens.items():
            v, n = _token_weight(_apply_shift(tok, shift), clean)
            weight = freq * max(v + n, 1)
            for ch in tok:
                if is_broken_glyph(ch):
                    claims[ch][chr(ord(ch) + shift)] += weight

    return {glyph: cnt.most_common(1)[0][0] for glyph, cnt in claims.items()}


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
    """Насколько полно отображение чинит документ (для отчётов и гейтов).

    `pct` считается только по буквам и только по страницам, признанным
    кракозябрами, — на нём стоят гейты, и трогать его шкалу нельзя. Цифры с
    пунктуацией идут отдельными ключами (`glyph_*`): они встречаются и на
    листах, которые в остальном читаются, поэтому в ту же долю не сводятся.
    """
    total = fixed = 0
    glyph_total = glyph_fixed = 0
    for i in range(doc.page_count):
        t = doc[i].get_text("text")
        for c in t:
            if is_broken_glyph(c):
                glyph_total += 1
                if c in mapping:
                    glyph_fixed += 1
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
        "glyph_chars": glyph_total,
        "glyph_recovered": glyph_fixed,
        "glyph_pct": (
            round(100.0 * glyph_fixed / glyph_total, 1) if glyph_total else None
        ),
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
            cov = coverage(doc, mapping)
            # pct=None — букв чинить не пришлось (подстановка только по цифрам
            # и пунктуации). Отклонять такую нечего: у неё свой гейт по весу
            # свидетельств внутри detect_glyph_shifts().
            pct = cov["pct"] if cov["pct"] is not None else 100.0
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


def page_raw_text(page) -> str:
    """Текст листа в порядке чтения, без потери подменённых глифов.

    Порядок чтения по координатам, а не по внутреннему потоку файла, нужен
    обязательно: без него страница пояснительной записки начинается со штампа
    («Изм. Кол.уч, №док., Митрофанов…»), а текст документа идёт после него.

    Но `get_text("text", sort=True)` заодно выбрасывает управляющие коды — а
    это и есть подменённые цифры с пунктуацией, ради которых слой чинится: на
    титуле из «28-ХСА-1/25-ИОС2» так пропадали все три разделителя, и починке
    было уже нечего исправлять. Блоки с той же сортировкой их сохраняют, а
    вдобавок не склеивают соседние ячейки таблицы в «131313».
    """
    try:
        blocks = page.get_text("blocks", sort=True)
    except Exception:
        blocks = None
    if not blocks:
        return page.get_text("text", sort=True) or ""
    # b[6] — тип блока: 0 текст, 1 картинка. У картинки в b[4] лежат байты.
    return "".join(b[4] for b in blocks if b[6] == 0 and isinstance(b[4], str))


def page_text_fixed(page, *, quiet: bool = False) -> str:
    """Текстовый слой листа с починкой сломанного ToUnicode.

    Исправный слой возвращается как есть; неисправный — раскодированным, если
    подстановка нашлась и оказалась надёжной. Иначе возвращается исходный
    текст, и решение о нём принимает вызывающий код.
    """
    raw = page_raw_text(page)
    if not raw.strip():
        return raw
    broken = sum(1 for ch in raw if is_broken_any(ch))
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
    left = sum(1 for ch in fixed if is_broken_any(ch))
    return fixed if left < broken else raw
