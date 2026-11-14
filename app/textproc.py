# -*- coding: utf-8 -*-
"""Очистка сообщений чата, поиск эмоций и разбиение текста на языковые куски."""
import re

import langs

URL_RE = re.compile(r"(https?://\S+|www\.\S+|\b[\w-]+\.(?:com|ru|tv|net|org|gg|io|me|su|рф)(?:/\S*)?)", re.I)
# Разрешены буквы всех распространённых алфавитов: латиница (с диакритикой),
# кириллица, греческий, иврит, арабский, деванагари, тайский, иероглифы,
# кана, хангыль.
ALLOWED_RE = re.compile(
    r"[^0-9A-Za-z\u00C0-\u024F\u0370-\u03FF\u0400-\u04FF\u0530-\u058F\u0590-\u05FF"
    r"\u0600-\u06FF\u0750-\u077F\u0900-\u097F\u0E00-\u0E7F\u1100-\u11FF\u3040-\u30FF"
    r"\u3130-\u318F\u3400-\u4DBF\u4E00-\u9FFF\uAC00-\uD7AF\uF900-\uFAFF"
    r"\s.,!?;:'\"()%+\-\u2013\u2014/&№$€₽]")
LETTER_RE = re.compile(
    r"[A-Za-z\u00C0-\u024F\u0370-\u03FF\u0400-\u04FF\u0530-\u058F\u0590-\u05FF"
    r"\u0600-\u06FF\u0750-\u077F\u0900-\u097F\u0E00-\u0E7F\u3040-\u30FF"
    r"\u3400-\u4DBF\u4E00-\u9FFF\uAC00-\uD7AF]")
REPEAT_CHAR_RE = re.compile(r"(.)\1{3,}")
REPEAT_PUNCT_RE = re.compile(r"([.,!?;:\-])\1+")
SPACE_RE = re.compile(r"\s+")
CYR_RE = re.compile(r"[А-Яа-яЁё]")
LAT_RE = re.compile(r"[A-Za-z]")

# Максимальная длина одного куска, который отдаётся модели (у XTTS есть лимит на язык)
CHUNK_LIMIT = {"ru": 160, "en": 220}


def strip_emotes(text, emote_ranges):
    """Убирает смайлы Twitch по позициям из IRC-тегов."""
    if not emote_ranges:
        return text
    chars = list(text)
    for start, end in sorted(emote_ranges, reverse=True):
        if 0 <= start <= end < len(chars):
            chars[start:end + 1] = [" "]
    return "".join(chars)


def _clean_plain(text):
    text = text.replace("@", " ").replace("_", " ")
    text = ALLOWED_RE.sub(" ", text)
    text = REPEAT_CHAR_RE.sub(lambda m: m.group(1) * 3, text)
    text = REPEAT_PUNCT_RE.sub(r"\1", text)
    return SPACE_RE.sub(" ", text).strip()


def _cut(text, max_len):
    if len(text) <= max_len:
        return text
    cut = text[:max_len]
    space = cut.rfind(" ")
    if space > max_len * 0.6:
        cut = cut[:space]
    return cut.strip()


def _speakable(text):
    """Есть ли что произнести: буква любого алфавита или цифра."""
    return bool(LETTER_RE.search(text)) or bool(re.search(r"\d", text))


def clean_text(text, emote_ranges=None, max_len=200, emotes=False):
    """Чистит сообщение. При emotes=True ремарки вроде (смех) или *вздыхает*
    заменяются метками {laugh}, {sigh} - их потом озвучивает emotes.py."""
    text = strip_emotes(text, emote_ranges)
    text = URL_RE.sub(" ", text)
    if not emotes:
        text = _cut(_clean_plain(text), max_len)
        return text if _speakable(text) else ""
    from emotes import mark, split_emotes
    out, left, spoken = [], max_len, False
    for kind, val in split_emotes(text):
        if kind == "emote":
            out.append(mark(val))
            spoken = True
            continue
        if left <= 0:
            continue
        piece = _cut(_clean_plain(val), left)
        if piece:
            left -= len(piece) + 1
            out.append(piece)
            spoken = spoken or _speakable(piece)
    return " ".join(out) if spoken else ""


def _word_lang(word, dominant="en"):
    """Язык слова: None, если в слове нет букв."""
    if not LETTER_RE.search(word):
        return None
    return langs.word_code(word, dominant)


def _letters(s):
    return len(LETTER_RE.findall(s))


def _split_long(text, limit):
    """Режет длинный текст на куски не длиннее limit, по знакам препинания или пробелам."""
    out = []
    text = text.strip()
    while len(text) > limit:
        cut = -1
        for mark in (". ", "! ", "? ", "; ", ", ", " "):
            pos = text.rfind(mark, 0, limit)
            if pos > limit * 0.4:
                cut = pos + len(mark) - 1
                break
        if cut <= 0:
            cut = limit
        out.append(text[:cut].strip())
        text = text[cut:].strip()
    if text:
        out.append(text)
    return out


def _text_segments(text, mode="auto"):
    text = text.strip()
    if not text:
        return []
    if mode in ("ru", "en"):
        runs = [(mode, text)]
    else:
        # авто: язык определяется по всему сообщению, отдельные слова уточняют
        dominant = langs.detect(text)
        runs = []
        for word in text.split(" "):
            lang = _word_lang(word, dominant)
            if runs and (lang is None or lang == runs[-1][0]):
                runs[-1] = (runs[-1][0], runs[-1][1] + " " + word)
            elif not runs:
                runs.append((lang, word))
            elif runs[-1][0] is None:
                runs[-1] = (lang, runs[-1][1] + " " + word)
            else:
                runs.append((lang, word))
        if runs and runs[0][0] is None:
            runs[0] = (dominant, runs[0][1])
        # короткие вставки (1-2 буквы: gg, ok) приклеиваем к соседнему куску
        merged = []
        for lang, part in runs:
            if not merged:
                merged.append((lang, part))
                continue
            prev_lang, prev_part = merged[-1]
            if _letters(part) < 3:
                merged[-1] = (prev_lang, prev_part + " " + part)
            elif _letters(prev_part) < 3:
                merged[-1] = (lang, prev_part + " " + part)
            elif prev_lang == lang:
                merged[-1] = (lang, prev_part + " " + part)
            else:
                merged.append((lang, part))
        runs = merged
    out = []
    for lang, part in runs:
        for chunk in _split_long(part, CHUNK_LIMIT.get(lang, 160)):
            out.append((lang, chunk))
    return out


MARK_SPLIT_RE = re.compile(r"\{([a-z]+)\}")


def segments(text, mode="auto"):
    """Возвращает список (язык, текст). mode: auto | ru | en.

    Метки эмоций {laugh} становятся отдельными кусками ("emote", "laugh").
    """
    out = []
    parts = MARK_SPLIT_RE.split(text)
    for n, part in enumerate(parts):
        if n % 2:
            out.append(("emote", part))
        else:
            out.extend(_text_segments(part, mode))
    return out


def norm_key(name):
    """Ключ для сравнения имён голосов: без регистра, пробелов и ё."""
    name = name.lower().replace("ё", "е")
    return re.sub(r"[^0-9a-zа-я]", "", name)
