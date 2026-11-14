# -*- coding: utf-8 -*-
"""Проверка логики эмоций: ремарки -> метки -> куски -> теги/записи."""
import os
import sys
import wave

APP = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app")
sys.path.insert(0, APP)

import emotes          # noqa: E402
import textproc        # noqa: E402

FAILED = []


def check(name, got, want):
    ok = got == want
    if not ok:
        FAILED.append(name)
    print("%-58s %s | %s" % (name, "OK" if ok else "FAIL", got if ok else "got=%r want=%r" % (got, want)))


# --- разбор ремарок ---
check("(смех) -> смех", emotes.match_emote("смех"), "laugh")
check("громкий смех -> смех", emotes.match_emote("громкий смех"), "laugh")
check("(вздыхает) -> вздох", emotes.match_emote("вздыхает"), "sigh")
check("[кашель] -> кашель", emotes.match_emote("кашель"), "cough")
check("*смех* -> смех", emotes.match_emote("смех"), "laugh")
check("(ха-ха) -> смех", emotes.match_emote("ха-ха"), "laugh")
check("(lol) -> смех", emotes.match_emote("lol"), "laugh")
check("(со скидкой) -> неизвестно", emotes.match_emote("со скидкой"), None)
check("(крутой чел) -> неизвестно", emotes.match_emote("крутой чел"), None)

segs = emotes.split_emotes("привет (смех) как дела")
check("split: текст+эмоция+текст", [k for k, _ in segs], ["text", "emote", "text"])
check("split: ключ эмоции", segs[1][1], "laugh")
check("split: неизвестная ремарка остаётся текстом",
      [k for k, _ in emotes.split_emotes("привет (со скидкой)")], ["text"])
check("split: одинаковые подряд - один раз",
      [k for k, _ in emotes.split_emotes("(смех)(смех)(смех)")], ["emote"])
check("split: не больше 4 эмоций",
      len([k for k, _ in emotes.split_emotes("(смех)(вздох)(кашель)(хм)(крик)")]), 4)

# --- чистка текста ---
cleaned = textproc.clean_text("привет (смех) как дела", None, 200, emotes=True)
check("clean_text: метка в тексте", cleaned, "привет {laugh} как дела")
check("clean_text: без эмоций меток нет",
      "{laugh}" in textproc.clean_text("привет (смех)", None, 200, emotes=False), False)
check("clean_text: только ремарка - не пусто",
      textproc.clean_text("(смех)", None, 200, emotes=True), "{laugh}")
check("clean_text: неизвестная ремарка читается как текст",
      textproc.clean_text("привет (со скидкой)", None, 200, emotes=True), "привет (со скидкой)")

# --- куски ---
parts = textproc.segments("привет {laugh} как дела", "auto")
check("segments: метка стала отдельным куском", [k for k, _ in parts], ["ru", "emote", "ru"])
check("segments: ключ эмоции", parts[1][1], "laugh")

# --- CosyVoice 3 ---
text, tagged = emotes.cosy_text([("ru", "привет"), ("emote", "laugh"), ("ru", "как дела")], "ru")
check("cosy_text: тег модели", text, "привет [laughter] как дела")
check("cosy_text: флаг тегов", tagged, True)
text2, tagged2 = emotes.cosy_text([("emote", "cry")], "ru")
check("cosy_text: у CosyVoice нет тега плача - замена текстом", text2, "Хнык-хнык.")
check("cosy_text: плач не помечен как тег", tagged2, False)
text3, _ = emotes.cosy_text([("emote", "laugh")], "ru")
check("cosy_text: одно слово смита с тегом", text3, "[laughter]")

# --- записи из sounds/ ---
sounds = os.path.join(os.path.dirname(APP), "sounds")
store = emotes.EmoteSounds(sounds, 24000, lambda p: (None, 22050))
store.prepare_folders()
check("папки эмоций созданы", os.path.isdir(os.path.join(sounds, "laugh")), True)
check("ПРОЧТИ.txt создан", os.path.isfile(os.path.join(sounds, "ПРОЧТИ.txt")), True)

res_b = store.resolve([("ru", "привет"), ("emote", "breath")], None, "ru")
check("resolve: нет ни записи, ни замены -> читаем слово", res_b[1], ("ru", "вдох"))

res = store.resolve([("ru", "привет"), ("emote", "laugh"), ("ru", "как дела")], None, "ru")
check("resolve без записи -> текст-замена", [k for k, _ in res], ["ru", "ru", "ru"])
check("resolve без записи -> смех заменён на «Ха-ха-ха!»", res[1][1], "Ха-ха-ха!")

# кладём настоящую запись и проверяем, что она подхватывается
laugh_dir = os.path.join(sounds, "laugh")
wav_path = os.path.join(laugh_dir, "test.wav")
with wave.open(wav_path, "wb") as w:
    w.setnchannels(1)
    w.setsampwidth(2)
    w.setframerate(24000)
    w.writeframes((b"\x00\x10" * 24000))
try:
    import numpy as np

    def decode(p):
        with wave.open(str(p), "rb") as w:
            raw = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2")
        return raw.astype("float32") / 32768.0, w.getframerate()

    store2 = emotes.EmoteSounds(sounds, 24000, decode)
    res2 = store2.resolve([("ru", "привет"), ("emote", "laugh")], None, "ru")
    check("resolve с записью -> кусок audio", res2[1][0], "audio")
    check("resolve с записью -> это numpy-сигнал", hasattr(res2[1][1], "__len__"), True)
finally:
    os.remove(wav_path)

# --- воркер CosyVoice: проверка, что нормализация не портит теги ---
import cosyvoice_worker  # noqa: E402


class _Frontend:
    def __init__(self, mode):
        self.mode = mode

    def text_normalize(self, text, split=False, text_frontend=True):
        if self.mode == "broken":
            return text.replace("[laughter]", "").strip()
        if self.mode == "notstr":
            return ["[laughter]"]
        return text


class _Model:
    def __init__(self, mode):
        self.frontend = _Frontend(mode)


check("теги пережили нормализацию", cosyvoice_worker.tags_survive(_Model("ok"), "привет [laughter] как дела"), True)
check("нормализация портит тег -> нормализация отключается",
      cosyvoice_worker.tags_survive(_Model("broken"), "привет [laughter] как дела"), False)
check("нормализация вернула не строку -> отключаем",
      cosyvoice_worker.tags_survive(_Model("notstr"), "привет [laughter]"), False)
check("без тегов нормализация не проверяется",
      cosyvoice_worker.tags_survive(_Model("broken"), "привет как дела"), True)

print()
print("Провалено: %d" % len(FAILED))
if FAILED:
    for f in FAILED:
        print("  - " + f)
sys.exit(1 if FAILED else 0)
