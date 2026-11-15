# -*- coding: utf-8 -*-
"""Синтез реплики: XTTS, RVC и CosyVoice 3, записи эмоций, стили речи.

Модуль отвечает за один путь: текст -> звуковой файл в data/cache и его
длительность. Очереди и HTTP его не знают: App отдаёт сюда задание и получает
(путь, длительность).

Точность вычислений не меняется: модель работает в fp32 (ADR 0004).
"""
import logging
import os
import sys
import uuid
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

from paths import CACHE, DATA  # noqa: E402
from engine import OUT_SR, write_wav  # noqa: E402
from settings import load_json  # noqa: E402
from textproc import segments  # noqa: E402
import styles as stylelib  # noqa: E402
import langs  # noqa: E402
import emotes as emotelib  # noqa: E402

log = logging.getLogger("synthesis")


def _read_samples(path, target_sr=OUT_SR):
    """Читает wav -> моно float32 нужной частоты (для вклейки звуков эмоций)."""
    import numpy as np
    import wave
    try:
        with wave.open(str(path), "rb") as w:
            sr = w.getframerate()
            raw = w.readframes(w.getnframes())
    except Exception as e:
        log.warning("не удалось прочитать wav %s: %s", path, e)
        return None
    if not raw:
        return None
    x = np.frombuffer(raw, dtype="<i2").astype("float32") / 32768.0
    if len(x) > 1 and sr != target_sr:
        n = max(1, int(round(len(x) * target_sr / float(sr))))
        x = np.interp(np.linspace(0, len(x) - 1, n), np.arange(len(x)), x).astype("float32")
    return x


class Synthesizer:
    """Синтез одной реплики голосом.

    Сотрудники передаются явно: движок, голоса, настройки, записи эмоций,
    мосты RVC и CosyVoice. Глобальных переменных нет.
    """

    def __init__(self, engine, voices, settings, emotes, rvc, cosyvoice, ensure_cosyvoice):
        self.engine = engine
        self.voices = voices
        self.settings = settings
        self.emotes = emotes
        self.rvc = rvc
        self.cosyvoice = cosyvoice
        self.ensure_cosyvoice = ensure_cosyvoice
        self.style_presets = {}      # подбор стилей под каждый голос
        self._presets_mtime = 0.0
        self._emote_cache = {}      # (голос, эмоция) -> сгенерированный звук
        self._emote_gen = {}        # эмоция -> сколько раз звук генерировался

    def emote_stats(self):
        """Сколько раз звук эмоции генерировался голосом."""
        return dict(self._emote_gen)

    def reset_emote_cache(self):
        """Сбрасывает кэш сгенерированных звуков эмоций."""
        self._emote_cache.clear()

    def style_params(self, voice_id, style_key):
        """Параметры стиля речи: свои для голоса, иначе общие.

        Свои параметры подбирает tools/gen_style_presets.py: у каждого голоса
        своя "радостность" и свой "шёпот". Файл перечитывается, если он
        изменился.
        """
        sp = stylelib.params(style_key)
        if not style_key:
            return sp
        try:
            path = DATA / "style_presets.json"
            mt = path.stat().st_mtime
            if mt != self._presets_mtime:
                self.style_presets = load_json(path, {})
                self._presets_mtime = mt
        except OSError:
            pass
        preset = (self.style_presets.get(voice_id) or {}).get(style_key)
        if preset:
            for k in ("speed", "pitch", "temp", "gain", "lowpass"):
                if k in preset:
                    sp[k] = preset[k]
        return sp

    def emote_voice_audio(self, key, voice):
        """Звук эмоции, сгенерированный выбранным голосом.

        Используется, когда в sounds/<эмоция>/ нет своей записи: программа
        синтезирует короткую вокализацию («фх…», «ха-ха-ха…») тем же голосом,
        которым читается сообщение, и вклеивает её отдельным куском.
        Слово («вдох», «смех») не читается никогда.
        """
        if not voice:
            return None
        ckey = (voice["id"], key)
        hit = self._emote_cache.get(ckey)
        if hit is not None:
            return hit
        lang = "en" if self.settings["language"] == "en" else "ru"
        text = emotelib.vocal_text(key, lang)
        if not text:
            return None
        tmp = CACHE / ("emo_%s.wav" % uuid.uuid4().hex)
        out = CACHE / ("emo_%s.wav" % uuid.uuid4().hex)
        samples = None
        try:
            pitch = self.voices.effective_pitch(voice)
            # для RVC-голоса latents() даёт базовый голос: тембр персонажа
            # наложит общий RVC-проход в synthesize, как и для записей из sounds/
            latents = self.voices.latents(voice["id"])
            self.engine.synthesize([(lang, text)], latents, tmp, self.settings["speed"], pitch)
            os.replace(str(tmp), str(out))
            samples = _read_samples(out, OUT_SR)
        except Exception as e:
            log.warning("не удалось сгенерировать звук эмоции %s: %s", key, e)
            samples = None
        finally:
            for p in (tmp, out):
                try:
                    p.unlink()
                except OSError:
                    pass
        if samples is None or not len(samples):
            return None
        self._emote_cache[ckey] = samples
        self._emote_gen[key] = self._emote_gen.get(key, 0) + 1
        return samples

    def emote_segs(self, segs, voice):
        """Эмоции без записи из sounds/ превращаются в звук, сгенерированный голосом."""
        out = []
        for kind, val in segs:
            if kind != "emote":
                out.append((kind, val))
                continue
            samples = self.emote_voice_audio(val, voice)
            if samples is not None and len(samples):
                out.append(("audio", samples))
            # не получилось - эмоция пропускается, слово не читается
        return out

    def cosy_parts(self, segs, voice):
        """Разбивает куски на части для CosyVoice 3: записи из sounds/ и текст.

        Запись из sounds/ важнее собственных тегов модели: если для этого голоса
        есть клип (настоящий смех, крик), он вклеивается, а остальные эмоции
        CosyVoice 3 произносит сам тегами ([laughter], [sigh]...).
        """
        parts = []
        run = []

        def flush():
            if not run:
                return
            text, tagged = emotelib.cosy_text(run, self.settings["language"])
            if text:
                parts.append(("text", text, tagged))
            del run[:]

        for kind, val in segs:
            if kind != "emote":
                run.append((kind, val))
                continue
            samples = None
            try:
                samples = self.emotes.clip(val, voice)
            except Exception:
                samples = None
            if samples is not None and len(samples):
                flush()
                parts.append(("audio", samples, False))
            else:
                run.append((kind, val))
        flush()
        return parts

    def render_cosy(self, segs, voice, out_path, style=None):
        """Синтез CosyVoice 3 с учётом записей эмоций из sounds/ и стиля речи."""
        sp = style or {}
        parts = self.cosy_parts(segs, voice)
        if not parts:
            raise ValueError("нечего читать")
        prompt, prompt_text = self.voices.cosyvoice_prompt(voice["id"])
        self.ensure_cosyvoice()
        if len(parts) == 1 and parts[0][0] == "text":
            text, tagged = parts[0][1], parts[0][2]
            tmp = CACHE / ("cv_%s.wav" % uuid.uuid4().hex)
            try:
                self.cosyvoice.synthesize(text, prompt, prompt_text, tmp,
                                          self.settings["speed"] * float(sp.get("speed") or 1.0),
                                          tags=tagged)
                x = _read_samples(tmp, OUT_SR)
            finally:
                try:
                    tmp.unlink()
                except OSError:
                    pass
            if x is None or not len(x):
                raise ValueError("нечего читать")
            x = stylelib.post(x, OUT_SR, sp, do_pitch=True)
            write_wav(out_path, x, OUT_SR)
            return len(x) / float(OUT_SR)
        import numpy as np
        chunks = []
        for kind, val, tagged in parts:
            if kind == "audio":
                chunks.append(np.asarray(val, dtype=np.float32).reshape(-1))
                continue
            tmp = CACHE / ("cv_%s.wav" % uuid.uuid4().hex)
            try:
                self.cosyvoice.synthesize(val, prompt, prompt_text, tmp,
                                          self.settings["speed"] * float(sp.get("speed") or 1.0),
                                          tags=tagged)
                x = _read_samples(tmp, OUT_SR)
                if x is not None and len(x):
                    chunks.append(x)
            finally:
                try:
                    tmp.unlink()
                except OSError:
                    pass
        if not chunks:
            raise ValueError("нечего читать")
        gap = np.zeros(int(0.12 * OUT_SR), dtype=np.float32)
        joined = []
        for i, x in enumerate(chunks):
            if i:
                joined.append(gap)
            joined.append(x)
        audio = np.concatenate(joined).astype(np.float32)
        audio = stylelib.post(audio, OUT_SR, sp, do_pitch=True)
        write_wav(out_path, audio, OUT_SR)
        return len(audio) / float(OUT_SR)

    # ---------- очередь: синтез и проигрывание ----------
    def synthesize(self, job):
        out = CACHE / ("%s.wav" % uuid.uuid4().hex)
        sp = self.style_params(job.get("voice_id", ""),
                               job.get("style") or self.settings.get("style") or "")
        segs = segments(job["text"], self.settings["language"])
        latents = self.voices.latents(job["voice_id"])
        voice = self.voices.get(job["voice_id"])
        pitch = self.voices.effective_pitch(voice)
        if voice and voice.get("engine") == "cosy":
            groups = langs.split_by_supported(segs, langs.COSY_LANGS)
            if len(groups) == 1 and groups[0][0]:
                # CosyVoice 3 в режиме in-context: образец голоса + его точный текст
                return out, self.render_cosy(segs, voice, out, sp)
            # Смешанный текст: CosyVoice 3 читает то, что умеет (китайский, русский,
            # английский), остальное уходит в XTTS - тембр остаётся тем же голосом.
            parts = []
            for is_cosy, group in groups:
                tmp = CACHE / ("part_%s.wav" % uuid.uuid4().hex)
                try:
                    if is_cosy:
                        # CosyVoice 3 читает свой кусок (и записи эмоций из sounds/)
                        self.render_cosy(group, voice, tmp, sp)
                    else:
                        g = self.emotes.resolve(group, voice, self.settings["language"])
                        g = self.emote_segs(g, voice)
                        if not g:
                            continue
                        self.engine.synthesize(g, self.voices.latents(voice["id"]),
                                               tmp, self.settings["speed"] * sp["speed"],
                                               sp["pitch"], style=sp)
                    x = _read_samples(tmp, OUT_SR)
                    if x is not None and len(x):
                        parts.append(x)
                finally:
                    try:
                        tmp.unlink()
                    except OSError:
                        pass
            if not parts:
                raise ValueError("нечего читать")
            import numpy as np
            gap = np.zeros(int(0.12 * OUT_SR), dtype=np.float32)
            joined = []
            for i, x in enumerate(parts):
                if i:
                    joined.append(gap)
                joined.append(x)
            audio = np.concatenate(joined).astype(np.float32)
            write_wav(out, audio, OUT_SR)
            return out, len(audio) / float(OUT_SR)
        # XTTS и RVC тегов эмоций не знают: вклеиваем записи из папки sounds/,
        # а если их нет - генерируем звук тем же голосом
        segs = self.emotes.resolve(segs, voice, self.settings["language"])
        segs = self.emote_segs(segs, voice)
        if not segs:
            raise ValueError("нечего читать")
        if voice and voice["kind"] == "rvc":
            # сначала читаем текст обычным голосом, потом RVC превращает его в голос персонажа
            pre = CACHE / ("pre_%s.wav" % uuid.uuid4().hex)
            try:
                self.engine.synthesize(segs, latents, pre, self.settings["speed"] * sp["speed"],
                                       pitch + sp["pitch"], style=sp)
                model, index = self.voices.model_files(voice["id"])
                dur = self.rvc.convert(pre, out, model, index, voice["pitch"], voice["index_rate"])
            finally:
                try:
                    pre.unlink()
                except OSError:
                    pass
        else:
            dur = self.engine.synthesize(segs, latents, out, self.settings["speed"] * sp["speed"],
                                         pitch + sp["pitch"], style=sp)
        return out, dur

