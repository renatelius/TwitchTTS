# -*- coding: utf-8 -*-
"""Движок озвучки. XttsEngine - настоящий (XTTS v2), MockEngine - заглушка для проверки интерфейса."""
import logging
import math
import os
import threading
import wave
from pathlib import Path

import numpy as np


def _setup_hf_cache():
    """Кэш моделей HuggingFace (XTTS) держим рядом с программой, а не на диске C:.

    Папка: <корень проекта>/models/hf, где <корень проекта> - папка на уровень выше
    папки программы. Если переменная HF_HOME уже задана - не трогаем её.
    """
    if os.environ.get("HF_HOME"):
        return
    try:
        project_root = Path(__file__).resolve().parent.parent.parent
        target = project_root / "models" / "hf"
        target.mkdir(parents=True, exist_ok=True)
        os.environ["HF_HOME"] = str(target)
        # TTS держит служебные папки в %LOCALAPPDATA%\tts - тоже уводим на диск проекта
        if not os.environ.get("TTS_HOME"):
            tts_home = project_root / "models" / "tts"
            tts_home.mkdir(parents=True, exist_ok=True)
            os.environ["TTS_HOME"] = str(tts_home)
    except OSError:
        pass


_setup_hf_cache()

log = logging.getLogger("engine")

OUT_SR = 24000   # частота, с которой XTTS отдаёт звук
REF_SR = 22050   # частота образцов голоса
MODEL_NAME = "tts_models/multilingual/multi-dataset/xtts_v2"


def write_wav(path, samples, sr):
    samples = np.clip(np.asarray(samples, dtype=np.float32), -1.0, 1.0)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes((samples * 32767).astype("<i2").tobytes())


def shift_pitch(samples, sr, steps):
    """Сдвигает тон на steps полутоонов. Длительность не меняется."""
    try:
        import librosa
    except Exception as exc:
        log.warning("не удалось сдвинуть тон: %s", exc)
        return samples
    y = np.asarray(samples, dtype=np.float32)
    if abs(float(steps)) < 0.01 or len(y) < 256:
        return y
    out = librosa.effects.pitch_shift(y, sr=int(sr), n_steps=float(steps))
    return np.asarray(out, dtype=np.float32)


def read_wav(path):
    with wave.open(str(path), "rb") as w:
        data = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2")
    return data.astype(np.float32) / 32768.0


class BaseEngine:
    def __init__(self):
        self.status = "loading"   # loading | ready | error
        self.error = ""
        self.device = ""
        self.lock = threading.Lock()

    def info(self):
        return {"status": self.status, "error": self.error, "device": self.device}

    def load(self):
        raise NotImplementedError

    def standard_voices(self):
        return []

    def standard_latents(self, name):
        raise NotImplementedError

    def make_latents(self, clip_paths):
        raise NotImplementedError

    def save_latents(self, latents, path):
        raise NotImplementedError

    def load_latents(self, path):
        raise NotImplementedError

    def synthesize(self, segs, latents, out_path, speed=1.0, pitch=0.0):
        """segs: [(язык, текст) | ("audio", готовая запись с частотой OUT_SR)].

        Куски "audio" (записи эмоций: смех, вздох) вклеиваются между фразами как есть.
        Пишет wav, возвращает длительность в секундах."""
        raise NotImplementedError


class XttsEngine(BaseEngine):
    def __init__(self, license_ok):
        super().__init__()
        self.license_ok = license_ok
        self.model = None
        self.config = None
        self.torch = None
        self._speakers = {}

    def load(self):
        try:
            if self.license_ok:
                os.environ["COQUI_TOS_AGREED"] = "1"
            import torch
            from TTS.tts.configs.xtts_config import XttsConfig
            from TTS.tts.models.xtts import Xtts
            from TTS.utils.manage import ModelManager

            self.torch = torch
            res = ModelManager().download_model(MODEL_NAME)
            model_dir = res[0] if isinstance(res, (tuple, list)) else res
            model_dir = str(model_dir)
            if not os.path.isdir(model_dir):
                model_dir = os.path.dirname(model_dir)

            config = XttsConfig()
            config.load_json(os.path.join(model_dir, "config.json"))
            model = Xtts.init_from_config(config)
            model.load_checkpoint(config, checkpoint_dir=model_dir, eval=True)
            if torch.cuda.is_available():
                model.cuda()
                self.device = "Видеокарта: " + torch.cuda.get_device_name(0)
            else:
                self.device = "Процессор (видеокарта NVIDIA не найдена — будет медленно)"
            self.model, self.config = model, config
            self._load_speakers(model_dir)
            self.status = "ready"
            log.info("модель загружена. %s. Стандартных голосов: %d", self.device, len(self._speakers))
        except Exception as e:
            log.exception("не удалось загрузить модель")
            self.status = "error"
            self.error = "%s: %s" % (type(e).__name__, e)

    def _load_speakers(self, model_dir):
        """Готовые голоса, которые идут в комплекте с XTTS v2."""
        speakers = None
        try:
            speakers = getattr(self.model.speaker_manager, "speakers", None)
        except Exception:
            speakers = None
        if not speakers:
            try:
                path = os.path.join(model_dir, "speakers_xtts.pth")
                try:
                    speakers = self.torch.load(path, map_location="cpu", weights_only=True)
                except TypeError:
                    speakers = self.torch.load(path, map_location="cpu")
            except Exception:
                log.exception("не удалось прочитать стандартные голоса")
                speakers = {}
        good = {}
        for name, val in dict(speakers).items():
            if isinstance(val, dict) and "gpt_cond_latent" in val and "speaker_embedding" in val:
                good[str(name)] = val
        self._speakers = good

    def _dev(self):
        return next(self.model.parameters()).device

    def _to_dev(self, latents):
        dev = self._dev()
        return {"gpt_cond_latent": latents["gpt_cond_latent"].to(dev),
                "speaker_embedding": latents["speaker_embedding"].to(dev)}

    def standard_voices(self):
        return sorted(self._speakers)

    def standard_latents(self, name):
        return self._to_dev(self._speakers[name])

    def make_latents(self, clip_paths):
        torch, cfg = self.torch, self.config
        clip_paths = [str(p) for p in clip_paths]
        with self.lock, torch.inference_mode():
            try:
                # Считаем сами из wav, чтобы не зависеть от аудио-кодеков torchaudio
                dev = self._dev()
                max_len = int(REF_SR * getattr(cfg, "max_ref_len", 30))
                audios, embs = [], []
                for p in clip_paths:
                    t = torch.from_numpy(read_wav(p)).unsqueeze(0).to(dev)[:, :max_len]
                    embs.append(self.model.get_speaker_embedding(t, REF_SR))
                    audios.append(t)
                full = torch.cat(audios, dim=-1)
                gpt = self.model.get_gpt_cond_latents(
                    full, REF_SR, length=getattr(cfg, "gpt_cond_len", 30),
                    chunk_length=getattr(cfg, "gpt_cond_chunk_len", 4))
                spk = torch.stack(embs).mean(dim=0)
            except (AttributeError, TypeError):
                log.warning("ручной расчёт слепка не сработал, использую стандартный способ")
                gpt, spk = self.model.get_conditioning_latents(audio_path=clip_paths)
        return {"gpt_cond_latent": gpt, "speaker_embedding": spk}

    def save_latents(self, latents, path):
        self.torch.save({k: v.detach().cpu() for k, v in latents.items()}, str(path))

    def load_latents(self, path):
        try:
            data = self.torch.load(str(path), map_location="cpu", weights_only=True)
        except TypeError:
            data = self.torch.load(str(path), map_location="cpu")
        return self._to_dev(data)

    def synthesize(self, segs, latents, out_path, speed=1.0, pitch=0.0):
        torch = self.torch
        parts = []
        gap = np.zeros(int(OUT_SR * 0.12), dtype=np.float32)
        with self.lock, torch.inference_mode():
            for lang, text in segs:
                if lang == "audio":
                    # запись эмоции: тон не сдвигаем, это не синтез
                    parts.append(np.asarray(text, dtype=np.float32).reshape(-1))
                    parts.append(gap)
                    continue
                out = self.model.inference(
                    text, lang, latents["gpt_cond_latent"], latents["speaker_embedding"],
                    temperature=0.7, speed=float(speed), enable_text_splitting=True)
                wav = out["wav"]
                if hasattr(wav, "detach"):
                    wav = wav.detach().cpu().numpy()
                wav = np.asarray(wav, dtype=np.float32).reshape(-1)
                parts.append(shift_pitch(wav, OUT_SR, float(pitch or 0)))
                parts.append(gap)
        audio = np.concatenate(parts[:-1]) if parts else np.zeros(1, dtype=np.float32)
        write_wav(out_path, audio, OUT_SR)
        return len(audio) / OUT_SR


class MockEngine(BaseEngine):
    """Вместо речи генерирует гудок. Нужен только для проверки интерфейса без видеокарты."""

    def load(self):
        self.device = "ЗАГЛУШКА (TTS_MOCK=1): вместо голоса гудок"
        self.status = "ready"

    def standard_voices(self):
        return ["Ana Florence", "Damien Black", "Viktor Menelaos"]

    def standard_latents(self, name):
        return {"freq": 180 + (sum(map(ord, name)) % 200)}

    def make_latents(self, clip_paths):
        return {"freq": 300 + 20 * len(clip_paths)}

    def save_latents(self, latents, path):
        with open(path, "w", encoding="utf-8") as f:
            f.write(str(latents["freq"]))

    def load_latents(self, path):
        with open(path, encoding="utf-8") as f:
            return {"freq": float(f.read())}

    def synthesize(self, segs, latents, out_path, speed=1.0, pitch=0.0):
        parts = []
        for lang, text in segs:
            if lang == "audio":
                parts.append(np.asarray(text, dtype=np.float32).reshape(-1))
                continue
            dur = max(0.4, min(len(text) * 0.06, 12.0)) / max(speed, 0.1)
            t = np.arange(int(OUT_SR * dur)) / OUT_SR
            beep = 0.2 * np.sin(2 * math.pi * latents["freq"] * t) * (0.6 + 0.4 * np.sin(2 * math.pi * 4 * t))
            parts.append(shift_pitch(beep, OUT_SR, float(pitch or 0)))
        audio = np.concatenate(parts) if parts else np.zeros(int(OUT_SR * 0.4), dtype=np.float32)
        write_wav(out_path, audio, OUT_SR)
        return len(audio) / OUT_SR
