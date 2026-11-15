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
check("cosy_text: у CosyVoice нет тега плача - вокализация", text2, "у-у-у-у...")
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
check("resolve: нет записи -> кусок остаётся эмоцией (её генерирует голос)",
      res_b, [("ru", "привет"), ("emote", "breath")])

res = store.resolve([("ru", "привет"), ("emote", "laugh"), ("ru", "как дела")], None, "ru")
check("resolve без записи -> эмоция не превращается в слово", [k for k, _ in res], ["ru", "emote", "ru"])
check("resolve без записи -> ключ эмоции сохранён", res[1][1], "laugh")

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

# --- вокализация: у каждой эмоции есть свой звук ---
missing = [k for k in emotes.EMOTES if not emotes.vocal_text(k)]
check("у всех эмоций есть вокализация", missing, [])
check("вокализация смеха", emotes.vocal_text("laugh"), "ха-ха-ха!")
check("вокализация вдоха", emotes.vocal_text("breath"), "фх... фх...")
check("вокализация на английском", emotes.vocal_text("gasp", "en"), "Aaah!")

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

# --- CosyVoice 3 берёт записи эмоций из sounds/ ---
import main as appmod  # noqa: E402
import synthesis as synthmod  # noqa: E402


class _FakeEmotes:
    def __init__(self, clips):
        self.clips = clips

    def clip(self, key, voice=None):
        return self.clips.get(key)


class _Stub:
    settings = {"language": "ru"}

    def __init__(self, clips):
        self.emotes = _FakeEmotes(clips)


segs_cosy = [("ru", "привет"), ("emote", "laugh"), ("ru", "как дела")]

parts_clip = synthmod.Synthesizer.cosy_parts(_Stub({"laugh": [0.1, 0.2]}), segs_cosy, None)
check("Cosy: запись из sounds/ отделяется от текста",
      [p[0] for p in parts_clip], ["text", "audio", "text"])
check("Cosy: текст до записи без тега", parts_clip[0][1], "привет")
check("Cosy: текст после записи без тега", parts_clip[2][1], "как дела")
check("Cosy: запись на своём месте", parts_clip[1][1], [0.1, 0.2])

parts_tag = synthmod.Synthesizer.cosy_parts(_Stub({}), segs_cosy, None)
check("Cosy: без записи - один текстовый кусок", [p[0] for p in parts_tag], ["text"])
check("Cosy: без записи остаётся тег модели", parts_tag[0][1], "привет [laughter] как дела")
check("Cosy: тег помечен как tagged", parts_tag[0][2], True)

parts_two = synthmod.Synthesizer.cosy_parts(_Stub({"laugh": [1.0], "sigh": [2.0]}),
                                    [("ru", "а"), ("emote", "laugh"), ("ru", "б"),
                                     ("emote", "sigh"), ("ru", "в")], None)
check("Cosy: две записи - две части звука",
      [p[0] for p in parts_two], ["text", "audio", "text", "audio", "text"])
check("Cosy: порядок частей сохранён", parts_two[3][1], [2.0])

print()
print("Провалено: %d" % len(FAILED))
if FAILED:
    for f in FAILED:
        print("  - " + f)
sys.exit(1 if FAILED else 0)
