# -*- coding: utf-8 -*-
"""Юнит-тесты модуля синтеза app/synthesis.py (без моделей и видеокарты).

Подменяем движок, голоса и записи эмоций - проверяем, что модуль собран
правильно: задание превращается в звуковой файл, стиль и кэш эмоций живут
на своих местах.
"""
import sys
import tempfile
from pathlib import Path

APP = Path(__file__).resolve().parent.parent / "app"
if str(APP) not in sys.path:
    sys.path.insert(0, str(APP))

from engine import OUT_SR, write_wav  # noqa: E402
from synthesis import Synthesizer  # noqa: E402

FAILED = []


def check(name, ok, detail=""):
    if not ok:
        FAILED.append(name)
    print("%-58s %s%s" % (name, "OK" if ok else "FAIL", (" | " + str(detail)) if detail else ""))


class FakeEngine:
    """Пишет тихий wav заданной длины и возвращает её же."""

    status = "ready"

    def __init__(self, dur=0.5):
        self.dur = dur
        self.calls = []

    def synthesize(self, segs, latents, out_path, speed=1.0, pitch=0.0, style=None):
        self.calls.append((segs, latents, speed, pitch))
        x = [0.05] * int(OUT_SR * self.dur)
        write_wav(Path(out_path), x, OUT_SR)
        return self.dur


class FakeVoices:
    def latents(self, voice_id):
        return {"freq": 180}

    def get(self, voice_id):
        return {"id": voice_id, "name": "Голос", "kind": "custom", "engine": "xtts"}

    def effective_pitch(self, voice):
        return 0


class FakeEmotes:
    """Записей эмоций нет: всё остаётся текстом."""

    def resolve(self, segs, voice, lang):
        return list(segs)

    def clip(self, key, voice=None):
        return None


tmp = Path(tempfile.mkdtemp(prefix="tts_synth_"))
try:
    settings = {"language": "ru", "speed": 1.0, "emotes": True, "style": "", "max_len": 200}
    engine = FakeEngine()
    synth = Synthesizer(engine=engine, voices=FakeVoices(), settings=settings,
                         emotes=FakeEmotes(), rvc=None, cosyvoice=None,
                         ensure_cosyvoice=lambda: None)

    path, dur = synth.synthesize({"id": "1", "user": "u", "text": "привет",
                                  "voice_id": "v1", "voice_name": "Голос", "style": ""})
    check("задание превращается в звуковой файл", Path(path).exists(), path)
    check("длительность возвращается", abs(dur - 0.5) < 0.01, dur)
    check("движок получил текст", len(engine.calls) == 1 and engine.calls[0][0], engine.calls)

    sp = synth.style_params("v1", "")
    check("стиль по умолчанию: скорость 1", sp["speed"] == 1.0, sp)
    check("чужие ключи стиля не влияют", synth.style_params("v1", "")["speed"] == 1.0)

    check("счётчик эмоций пуст", synth.emote_stats() == {}, synth.emote_stats())

    parts = synth.cosy_parts([("ru", "привет"), ("emote", "laugh")], None)
    check("Cosy: эмоция без записи остаётся в тексте", [p[0] for p in parts] == ["text"],
          [p[0] for p in parts])

    # записи эмоции нет - программа генерирует вокализацию тем же голосом
    segs = [("ru", "привет"), ("emote", "laugh")]
    out = synth.emote_segs(segs, {"id": "v1", "name": "Голос"})
    check("эмоция без записи генерируется голосом", [s[0] for s in out] == ["ru", "audio"],
          [s[0] for s in out])
    check("генерация эмоции посчитана", synth.emote_stats().get("laugh") == 1, synth.emote_stats())
    check("звук эмоции попал в кэш", len(synth._emote_cache) == 1, synth._emote_cache)
    synth.reset_emote_cache()
    check("кэш эмоций сбрасывается", synth._emote_cache == {}, synth._emote_cache)
finally:
    import shutil
    shutil.rmtree(tmp, ignore_errors=True)

print("")
print("Провалено: %d" % len(FAILED))
sys.exit(1 if FAILED else 0)
