# -*- coding: utf-8 -*-
"""Сквозная проверка эмоций: запись из sounds/ вклеивается в речь, выключатель работает."""
import json
import math
import os
import struct
import sys
import urllib.request
import wave

BASE = os.environ.get("TTS_BASE", "http://127.0.0.1:8765")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SOUNDS = os.path.join(ROOT, "sounds")
CLIP = os.path.join(SOUNDS, "laugh", "audit_tone.wav")
TONE_HZ = 440.0
TONE_SEC = 0.3
TONE_AMP = 0.9
# окно записи эмоции: запись идёт в начале фразы, пауза перед ней - 0.12 с
CLIP_WINDOW = (0.05, 0.45)

import atexit
import shutil


_HIDDEN = []


def hide_voice_clips(voice_name):
    """Свои клипы голоса (sounds/<эмоция>/<голос>/) на время аудита прячем.

    Аудит проверяет общий механизм: запись из sounds/ важнее генерации голосом.
    Если у голоса есть свои клипы, они всегда выигрывают, и проверка общего
    пути становится невозможной - поэтому мы их временно убираем.
    """
    for key in ("laugh",):
        src_dir = os.path.join(SOUNDS, key, voice_name)
        if os.path.isdir(src_dir):
            dst_dir = src_dir + "__audit_hidden"
            if os.path.isdir(dst_dir):
                shutil.rmtree(dst_dir)
            os.rename(src_dir, dst_dir)
            _HIDDEN.append((src_dir, dst_dir))


def restore_voice_clips():
    for src_dir, dst_dir in _HIDDEN:
        if os.path.isdir(dst_dir):
            if os.path.isdir(src_dir):
                shutil.rmtree(src_dir)
            os.rename(dst_dir, src_dir)
    del _HIDDEN[:]


atexit.register(restore_voice_clips)

FAILED = []


def check(name, ok, detail=""):
    if not ok:
        FAILED.append(name)
    print("%-56s %s%s" % (name, "OK" if ok else "FAIL", (" | " + str(detail)) if detail else ""))


def api(path, payload=None):
    if payload is None:
        req = urllib.request.Request(BASE + path)
    else:
        req = urllib.request.Request(BASE + path, data=json.dumps(payload).encode("utf-8"),
                                     headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=600) as r:
        return json.loads(r.read().decode("utf-8"))


def make_clip():
    os.makedirs(os.path.dirname(CLIP), exist_ok=True)
    sr = 24000
    n = int(sr * TONE_SEC)
    data = b"".join(struct.pack("<h", int(max(-1.0, min(1.0, TONE_AMP * math.sin(2 * math.pi * TONE_HZ * i / sr))) * 32767))
                  for i in range(n))
    with wave.open(CLIP, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(data)


def tone_amplitude(wav_bytes, window=None):
    """Амплитуда на частоте тона (Goertzel) в готовом wav.

    window - (начало, конец) в секундах: тон измеряется только в этом куске.
    Без окна амплитуда тем слабее, чем длиннее произнесённая речь,
    и проверка мигает на ровном месте.
    """
    import io
    with wave.open(io.BytesIO(wav_bytes), "rb") as w:
        sr = w.getframerate()
        raw = w.readframes(w.getnframes())
    total = len(raw) // 2
    if total < 100:
        return 0.0, 0.0
    samples = struct.unpack("<%dh" % total, raw[:total * 2])
    dur = total / float(sr)
    if window:
        start = max(0, int(window[0] * sr))
        stop = min(total, int(window[1] * sr))
        samples = samples[start:stop]
        if len(samples) < 100:
            return 0.0, dur
    n = len(samples)
    k = 2 * math.cos(2 * math.pi * TONE_HZ / sr)
    s1 = s2 = 0.0
    for x in samples:
        s0 = x / 32768.0 + k * s1 - s2
        s2, s1 = s1, s0
    amp = math.sqrt(max(0.0, s1 * s1 + s2 * s2 - k * s1 * s2)) / n * 2
    return amp, dur


def announce(text, voice_id=None):
    payload = {"text": text}
    if voice_id:
        payload["voice"] = voice_id
    res = api("/api/announce", payload)
    if not res.get("ok"):
        return None, res
    with urllib.request.urlopen(BASE + res["url"], timeout=60) as r:
        return r.read(), res


voices = api("/api/voices")["voices"]
xtts = next((v for v in voices if v.get("kind") == "custom" and v.get("engine") != "cosy"), None)
rvc = next((v for v in voices if v.get("kind") == "rvc"), None)
if xtts:
    hide_voice_clips(xtts["name"])

# 1) эндпоинт со списком эмоций
info = api("/api/emotes")
check("/api/emotes отвечает", info.get("ok") is True, info.get("error"))
check("эмоций в списке 12", len(info.get("emotes", {})) == 12, len(info.get("emotes", {})))
check("есть laugh и cry", "laugh" in info["emotes"] and "cry" in info["emotes"])
check("папка sounds указана", "sounds" in (info.get("folder") or ""), info.get("folder"))
check("нашёлся голос XTTS для проверки", xtts is not None, xtts["name"] if xtts else "-")

# 2) запись из sounds/ вклеивается в речь голоса XTTS
api("/api/emotes/reset", {})
gen0 = api("/api/emotes").get("generated", {}).get("laugh", 0)
make_clip()
try:
    wav, res = announce("(смех) привет", xtts["id"])
    check("озвучка с эмоцией синтезирована", wav is not None and len(wav) > 1000,
          res.get("error") or len(wav or b""))
    amp, dur = tone_amplitude(wav, CLIP_WINDOW) if wav else (0, 0)
    check("запись смеха вклеена в речь (тон слышен в сигнале)", amp > 0.3,
          "амплитуда %.3f, длительность %.2f с" % (amp, dur))
    check("длительность больше длины записи", dur > TONE_SEC, "%.2f с" % dur)
    gen1 = api("/api/emotes").get("generated", {}).get("laugh", 0)
    check("своя запись из sounds/ важнее генерации", gen1 == gen0,
          "счётчик %s -> %s" % (gen0, gen1))
finally:
    if os.path.exists(CLIP):
        os.remove(CLIP)

# 3) RVC-голос: запись идёт через модель вместе с речью - звук должен получиться без ошибки
if rvc:
    make_clip()
    try:
        wav_r, res_r = announce("(смех) привет", rvc["id"])
        check("RVC-голос: озвучка с эмоцией синтезирована", wav_r is not None and len(wav_r) > 1000,
              res_r.get("error") or len(wav_r or b""))
    finally:
        if os.path.exists(CLIP):
            os.remove(CLIP)

# 4) без записи - звук эмоции генерирует сам голос
gen2 = api("/api/emotes").get("generated", {}).get("laugh", 0)
wav2, res2 = announce("(смех) привет", xtts["id"])
check("без записи тоже озвучено (звук сгенерирован голосом)", wav2 is not None and len(wav2) > 1000,
      res2.get("error"))
amp2, dur2 = tone_amplitude(wav2, CLIP_WINDOW) if wav2 else (0, 0)
check("тона записи нет - голосом сгенерирован другой звук", amp2 < 0.15, "амплитуда %.3f" % amp2)
gen3 = api("/api/emotes").get("generated", {}).get("laugh", 0)
check("звук эмоции сгенерирован голосом", gen3 > gen2, "счётчик %s -> %s" % (gen2, gen3))
announce("(смех) привет", xtts["id"])
gen4 = api("/api/emotes").get("generated", {}).get("laugh", 0)
check("повторный запрос берёт готовый звук из кэша", gen4 == gen3, "счётчик %s" % gen4)

# 5) неизвестная ремарка читается как обычный текст
wav3, res3 = announce("привет (со скидкой)", xtts["id"])
check("неизвестная ремарка читается как текст", wav3 is not None and len(wav3) > 1000, res3.get("error"))

# 6) выключатель «эмоции» в настройках
make_clip()
try:
    old = api("/api/state")["settings"].get("emotes")
    api("/api/settings", {"emotes": False})
    wav4, res4 = announce("(смех) привет", xtts["id"])
    check("при выключенных эмоциях ремарка читается словом", wav4 is not None and len(wav4) > 1000, res4.get("error"))
    amp4, _ = tone_amplitude(wav4) if wav4 else (0, 0)
    check("тона записи нет при выключенных эмоциях", amp4 < 0.15, "амплитуда %.3f" % amp4)
    gen_off = api("/api/emotes").get("generated", {}).get("laugh", 0)
    check("при выключенных эмоциях звук не генерируется", gen_off == gen4, "счётчик %s" % gen_off)
    api("/api/settings", {"emotes": True if old is None else old})
finally:
    if os.path.exists(CLIP):
        os.remove(CLIP)

# 7) у голоса со своими клипами они важнее общих
restore_voice_clips()
if xtts:
    own_dir = os.path.join(SOUNDS, "laugh", xtts["name"])
    has_own = os.path.isdir(own_dir) and any(f.endswith(".wav") for f in os.listdir(own_dir))
    gen5 = api("/api/emotes").get("generated", {}).get("laugh", 0)
    wav5, res5 = announce("(смех) привет", xtts["id"])
    gen6 = api("/api/emotes").get("generated", {}).get("laugh", 0)
    if has_own:
        check("свой клип голоса важнее общего - генерации не было", gen6 == gen5,
              "счётчик %s -> %s" % (gen5, gen6))
        check("озвучка со своим клипом синтезирована", wav5 is not None and len(wav5) > 1000,
              res5.get("error") or len(wav5 or b""))
    else:
        check("у голоса нет своих клипов - звук генерируется голосом", gen6 > gen5,
              "счётчик %s -> %s" % (gen5, gen6))

print()
print("Провалено: %d" % len(FAILED))
for f in FAILED:
    print("  - " + f)
sys.exit(1 if FAILED else 0)
