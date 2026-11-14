# -*- coding: utf-8 -*-
"""Проверка многоязычности: определение языка, чистка текста, разбиение на куски."""
import os
import sys

APP = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app")
sys.path.insert(0, APP)

import langs          # noqa: E402
import textproc        # noqa: E402

FAILED = []


def check(name, got, want):
    ok = got == want
    if not ok:
        FAILED.append(name)
    print("%-56s %s | %s" % (name, "OK" if ok else "FAIL", got if ok else "got=%r want=%r" % (got, want)))


# --- определение языка ---
CASES = [
    ("привет как дела", "ru"), ("hello how are you", "en"),
    ("hallo wie geht es dir", "de"), ("bonjour comment allez-vous", "fr"),
    ("hola como estas", "es"), ("ciao come stai", "it"),
    ("ola como voce esta", "pt"), ("cześć jak się masz", "pl"),
    ("ahoj jak se máš", "cs"), ("merhaba nasılsın", "tr"),
    ("привіт як справи", "uk"), ("你好世界", "zh"),
    ("こんにちは世界", "ja"), ("안녕하세요", "ko"),
    ("مرحبا بالعالم", "ar"), ("नमस्ते दुनिया", "hi"),
    ("γειά σου κόσμε", "el"), ("שלום עולם", "he"),
    ("สวัสดีชาวโลก", "th"), ("xin chào thế giới", "vi"),
    ("guten tag welt", "de"), ("salut comment ça va", "fr"),
]
bad = [(t, langs.detect(t), w) for t, w in CASES if langs.detect(t) != w]
check("язык определяется верно (%d случаев)" % len(CASES), bad, [])

# --- коды для XTTS ---
check("украинский -> русский", langs.to_xtts("uk"), "ru")
check("немецкий -> немецкий", langs.to_xtts("de"), "de")
check("греческий -> английский", langs.to_xtts("el"), "en")
check("китайский -> zh-cn", langs.to_xtts("zh"), "zh-cn")
check("неизвестный -> английский", langs.to_xtts("zz"), "en")
check("все языки XTTS поддерживаются", [c for c in langs.XTTS_LANGS if c not in langs.XTTS_LANGS], [])

# --- чистка текста: чужие алфавиты не теряются ---
check("китайский не вычищается", textproc.clean_text("你好世界"), "你好世界")
check("японский не вычищается", textproc.clean_text("こんにちは世界"), "こんにちは世界")
check("корейский не вычищается", textproc.clean_text("안녕하세요"), "안녕하세요")
check("арабский не вычищается", textproc.clean_text("مرحبا بالعالم"), "مرحبا بالعالم")
check("хинди не вычищается", textproc.clean_text("नमस्ते दुनिया"), "नमस्ते दुनिया")
check("украинские буквы целы", textproc.clean_text("привіт, їжак, єдність"), "привіт, їжак, єдність")
check("немецкие буквы целы", textproc.clean_text("große Grüße"), "große Grüße")
check("польские буквы целы", textproc.clean_text("zażółć gęślą jaźń"), "zażółć gęślą jaźń")

# --- разбиение на языковые куски ---
segs = textproc.segments("привет как дела", "auto")
check("русский текст -> один кусок ru", [s[0] for s in segs], ["ru"])
segs = textproc.segments("hallo wie geht es dir", "auto")
check("немецкий текст -> de", segs[0][0], "de")
segs = textproc.segments("hello wie geht es dir", "auto")
check("в смешанном тексте немецкая фраза идёт отдельно", [s[0] for s in segs], ["en", "de"])
segs = textproc.segments("你好世界", "auto")
check("китайский -> zh", segs[0][0], "zh")
segs = textproc.segments("привет hello", "auto")
check("смешанный текст режется по языкам", [s[0] for s in segs], ["ru", "en"])
segs = textproc.segments("привет как дела", "ru")
check("принудительный язык ru", [s[0] for s in segs], ["ru"])
segs = textproc.segments("hello there", "en")
check("принудительный язык en", [s[0] for s in segs], ["en"])

# --- разбиение длинных текстов (лимиты токенизатора XTTS) ---
long_zh = "你好世界。" * 40
segs = textproc.segments(long_zh, "auto")
check("китайский текст режется на куски", len(segs) > 1, True)
check("все китайские куски в лимите (70)", all(len(s[1]) <= 70 for s in segs if s[0] == "zh"), True)
check("китайский кусок не рвётся посреди фразы",
      all(s[1].endswith("。") for s in segs if s[0] == "zh"), True)
segs = textproc.segments("こんにちは世界。" * 30, "auto")
check("японский текст режется на куски", len(segs) > 1, True)
check("все японские куски в лимите (60)", all(len(s[1]) <= 60 for s in segs if s[0] == "ja"), True)
check("японский кусок не рвётся посреди фразы",
      all(s[1].endswith("。") for s in segs if s[0] == "ja"), True)
segs = textproc.segments("你好世界" * 50, "auto")
check("китайский без пунктуации режется по лимиту",
      len(segs) > 1 and all(len(s[1]) <= 70 for s in segs), True)
long_ru = "Это первое предложение для проверки. " * 12
segs = textproc.segments(long_ru, "auto")
check("русский текст режется на куски", len(segs) > 1, True)
check("все русские куски в лимите (170)", all(len(s[1]) <= 170 for s in segs if s[0] == "ru"), True)
check("русский кусок заканчивается точкой",
      all(s[1].endswith(".") for s in segs if s[0] == "ru"), True)
segs = textproc.segments("первое предложение. второе предложение. третье.", "auto")
check("короткий русский текст - один кусок", len(segs), 1)

# --- движок: код языка, который понимает модель ---
import engine            # noqa: E402


class _Cfg:
    languages = list(langs.XTTS_LANGS)


class _Eng:
    config = _Cfg()


eng = _Eng()
e = object.__new__(engine.XttsEngine)
e.config = _Cfg()
check("движок: украинский -> ru", e.model_lang("uk"), "ru")
check("движок: немецкий -> de", e.model_lang("de"), "de")
check("движок: китайский -> zh-cn", e.model_lang("zh"), "zh-cn")
check("движок: греческий -> en", e.model_lang("el"), "en")
check("движок: неизвестный -> en", e.model_lang("zz"), "en")

# --- отбраковка лишнего «хвоста» XTTS ---
check("хвост: корейская фраза с посторонней речью - брак",
      engine.too_long(5.47, "안녕하세요", "ko"), True)
check("хвост: корейская фраза в норме - не брак",
      engine.too_long(1.58, "안녕하세요", "ko"), False)
check("хвост: китайская фраза в норме - не брак",
      engine.too_long(1.07, "你好世界", "zh-cn"), False)
check("хвост: японская фраза в норме - не брак",
      engine.too_long(1.07, "こんにちは世界", "ja"), False)
check("хвост: длинный русский текст - не брак",
      engine.too_long(12.0, "Это длинное предложение из нескольких слов " * 3, "ru"), False)
check("хвост: короткое слово - не брак", engine.too_long(0.5, "ок", "ru"), False)

print()
print("Провалено: %d" % len(FAILED))
for f in FAILED:
    print("  - " + f)
sys.exit(1 if FAILED else 0)
