# -*- coding: utf-8 -*-
"""Разбор эмоций в тексте: ремарки, метки и вокализация.

Только текст - без звука и без файлов. Отдельный модуль нужен, чтобы
чистка текста (app/textproc.py) и звук эмоций (app/emotes.py) не зависели
друг от друга.
"""
import re

MAX_PER_MESSAGE = 4      # больше эмоций в одном сообщении не озвучиваем (защита от спама)

# key: слова-ремарки (регулярные выражения, целиком), тег CosyVoice 3, вокализация.
# Теги CosyVoice 3 - из cosyvoice/tokenizer/tokenizer.py (CosyVoice3Tokenizer).
# "vocal" - короткий звук, который произносит голос, когда своего тега у модели нет.
EMOTES = {
    "laugh": {
        "title": "смех",
        "words": [r"смех\w*", r"сме[её]тся", r"смеюсь", r"смеясь", r"хохо\w+", r"рж[её]т", r"ржу", r"ору",
                  r"хихик\w*", r"а?(?:ха[\s-]*){2,}х?", r"(?:хи[\s-]*){2,}", r"(?:хе[\s-]*){2,}", r"лол", r"кек",
                  r"laugh\w*", r"(?:ha){2,}h?", r"lol", r"lmao", r"giggl\w+", r"chuckl\w+"],
        "cosy": "[laughter]",
        "vocal": {"ru": "ха-ха-ха!", "en": "Ha ha ha!"},
    },
    "sigh": {
        "title": "вздох",
        "words": [r"вздох\w*", r"вздыха\w+", r"вздохнул\w*", r"sigh\w*"],
        "cosy": "[sigh]",
        "vocal": {"ru": "э-э-э...", "en": "Ah-ah..."},
    },
    "breath": {
        "title": "вдох",
        "words": [r"вдох\w*", r"выдох\w*", r"выдыха\w+", r"дышит", r"breath\w*", r"inhale\w*", r"exhale\w*"],
        "cosy": "[breath]",
        "vocal": {"ru": "фх... фх...", "en": "Hh... hh..."},
    },
    "cough": {
        "title": "кашель",
        "words": [r"кашел\w*", r"кашля\w+", r"кхм+", r"кхе(?:-?кхе)*", r"cough\w*", r"ahem"],
        "cosy": "[cough]",
        "vocal": {"ru": "кхе-кхе!", "en": "Ahem!"},
    },
    "hmm": {
        "title": "хм",
        "words": [r"хм+", r"мм+", r"задумал\w*", r"думает", r"hm+", r"mm+", r"thinking", r"thinks"],
        "cosy": "[mn]",
        "vocal": {"ru": "хм-м-м...", "en": "Hmm..."},
    },
    "tsk": {
        "title": "цоканье",
        "words": [r"цок\w*", r"цык\w*", r"тц+", r"tsk\w*"],
        "cosy": "[clucking]",
        "vocal": {"ru": "тс-тс!", "en": "Tsk-tsk!"},
    },
    "smack": {
        "title": "чмок",
        "words": [r"чмок\w*", r"причмок\w+", r"поцелу\w+", r"целует", r"smack\w*", r"kiss\w*", r"mwah"],
        "cosy": "[lipsmack]",
        "vocal": {"ru": "м-м-м!", "en": "Mwah!"},
    },
    "hiss": {
        "title": "шипение",
        "words": [r"шипит", r"шипение", r"шиплю", r"шш+", r"hiss\w*", r"sh{2,}"],
        "cosy": "[hissing]",
        "vocal": {"ru": "ссссс...", "en": "Sssss..."},
    },
    # У CosyVoice 3 нет тегов для этих эмоций: звучит вокализация, сгенерированная голосом.
    "cry": {
        "title": "плач",
        "words": [r"плач\w*", r"плачу", r"рыда\w+", r"всхлип\w+", r"хнык\w+", r"cry\w*", r"cries", r"sob\w*"],
        "cosy": "",
        "vocal": {"ru": "у-у-у-у...", "en": "Sob sob..."},
    },
    "scream": {
        "title": "крик",
        "words": [r"крик\w*", r"крич\w+", r"вопль", r"вопит", r"ор[её]т", r"визж\w+",
                  r"scream\w*", r"yell\w*", r"shout\w*"],
        "cosy": "",
        "vocal": {"ru": "а-а-а-а-а!", "en": "Aaaah!"},
    },
    "yawn": {
        "title": "зевок",
        "words": [r"зевок", r"зева\w+", r"зевнул\w*", r"yawn\w*"],
        "cosy": "",
        "vocal": {"ru": "а-а-а-а-а...", "en": "Ah-ah-ah..."},
    },
    "gasp": {
        "title": "ах",
        "words": [r"а+х+", r"о+х+", r"ахает", r"ахнул\w*", r"охает", r"удивл\w+", r"шок", r"в шоке", r"gasp\w*", r"shocked"],
        "cosy": "",
        "vocal": {"ru": "а-а!", "en": "Aaah!"},
    },
}

# Ремарка - это короткий текст целиком в скобках одного вида: (смех), [смех], *смех*, <смех>
BRACKET_RE = re.compile(r"\(([^()\[\]*<>]{1,40})\)|\[([^()\[\]*<>]{1,40})\]"
                        r"|\*+([^()\[\]*<>]{1,40}?)\*+|<([^()\[\]*<>]{1,40})>")
# перед словом может стоять одно уточнение: (громкий смех), (тихо вздыхает), (злобно смеётся)
_MODIFIER = r"(?:[а-яёa-z]+\s+)?"
_KEY_RE = {key: re.compile(_MODIFIER + "(?:" + "|".join(spec["words"]) + ")[.!?]*", re.I)
           for key, spec in EMOTES.items()}

MARK_RE = re.compile(r"\{([a-z]+)\}")


def vocal_text(key, lang="ru"):
    """Вокализация для эмоции: её произносит голос, получается звук, а не слово."""
    vocal = (EMOTES.get(key) or {}).get("vocal") or {}
    return vocal.get(lang) or vocal.get("ru") or ""


def match_emote(inner):
    """Ключ эмоции по тексту внутри скобок или None."""
    inner = " ".join(inner.lower().split())
    for key, rx in _KEY_RE.items():
        if rx.fullmatch(inner):
            return key
    return None


def split_emotes(text):
    """Режет текст на [("text", кусок) | ("emote", ключ)]. Неизвестные ремарки остаются текстом."""
    out, pos, count, last = [], 0, 0, None
    for m in BRACKET_RE.finditer(text):
        key = match_emote(next(g for g in m.groups() if g is not None))
        if not key:
            continue
        between = text[pos:m.start()]
        if between.strip():
            out.append(("text", between))
            last = None
        pos = m.end()
        if key == last or count >= MAX_PER_MESSAGE:
            continue                     # (смех)(смех)(смех) звучит один раз
        out.append(("emote", key))
        last, count = key, count + 1
    if text[pos:].strip():
        out.append(("text", text[pos:]))
    return out


def mark(key):
    return "{%s}" % key


def cosy_text(segs, mode="auto"):
    """Собирает текст для CosyVoice 3: эмоции становятся его тегами.

    Возвращает (текст, есть_ли_теги)."""
    parts, tagged, lang = [], False, ("en" if mode == "en" else "ru")
    for kind, val in segs:
        if kind != "emote":
            parts.append(val)
            lang = kind if kind in ("ru", "en") else lang
            continue
        spec = EMOTES.get(val) or {}
        if spec.get("cosy"):
            parts.append(spec["cosy"])
            tagged = True
        else:
            text = vocal_text(val, lang)
            if text:
                parts.append(text)
    return " ".join(p for p in parts if p).strip(), tagged
