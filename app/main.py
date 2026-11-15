# -*- coding: utf-8 -*-
"""Озвучка чата Twitch. Запуск: start.bat (или python app/main.py)."""
import asyncio
import contextlib
import hashlib
import inspect
import json
import logging
import os
import shutil
import sys
import threading
import time
import uuid
import webbrowser
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import uvicorn  # noqa: E402
from starlette.applications import Starlette  # noqa: E402
try:
    from starlette.middleware.cors import CORSMiddleware  # noqa: E402
except Exception:  # очень старые версии Starlette - работаем без CORS
    CORSMiddleware = None




from paths import APP_DIR, ROOT, DATA, CACHE, TMP, WEB  # noqa: E402
from settings import Settings, load_json  # noqa: E402
from engine import OUT_SR, REF_SR, MockEngine, XttsEngine, write_wav  # noqa: E402
import rvc as rvclib  # noqa: E402
import cosyvoice as cvlib  # noqa: E402
from textproc import clean_text, segments  # noqa: E402
import styles as stylelib  # noqa: E402
import features as featlib  # noqa: E402
import chat_rules as chatrules  # noqa: E402
import api as apilib  # noqa: E402
from chat_rules import login_allowed, split_prefix, split_voice_prefix, voice_blocked  # noqa: E402
import emotes as emotelib  # noqa: E402
import langs  # noqa: E402
from twitch import TwitchChat  # noqa: E402
import twitch_api as tapilib  # noqa: E402
from voices import VoiceStore, decode as decode_audio  # noqa: E402
from rvc_train import RvcTrainer  # noqa: E402

HOST = os.environ.get("TTS_HOST", "127.0.0.1")
PORT = int(os.environ.get("TTS_PORT", "8765"))

log = logging.getLogger("main")

def _vol_factor(value):
    """Громкость голоса (проценты) -> коэффициент 0.0-2.0."""
    try:
        return max(0.0, min(float(value), 200.0)) / 100.0
    except (TypeError, ValueError):
        return 1.0


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


class App:
    def __init__(self):
        self.settings = Settings(DATA / "settings.json")
        self.user_voices = load_json(DATA / "user_voices.json", {})
        self.style_presets = {}      # подбор стилей под каждый голос
        self._presets_mtime = 0.0
        if os.environ.get("TTS_MOCK") == "1":
            self.engine = MockEngine()
        else:
            self.engine = XttsEngine(license_ok=(DATA / "license_ok.txt").exists())
        self.voices = VoiceStore(ROOT / "voices", self.engine, lambda: self.settings["disabled_voices"])
        self.twitch = TwitchChat(self.on_chat, self.push_status)
        bridge = rvclib.MockBridge if os.environ.get("TTS_MOCK") == "1" else rvclib.RvcBridge
        self.rvc = bridge(APP_DIR / "rvc_worker.py", DATA / "rvc_log.txt", self.push_status_threadsafe)
        self.trainer = RvcTrainer(self.settings["applio_path"], DATA / "rvc_models",
                                  DATA / "rvc_train_log.txt", self.push_status_threadsafe)
        self.cosyvoice = cvlib.CosyVoiceBridge(APP_DIR / "cosyvoice_worker.py",
                                               DATA / "cosyvoice_log.txt",
                                               self.push_status_threadsafe)
        # записи эмоций для движков без собственных тегов (XTTS, RVC): sounds/<эмоция>/
        self.emotes = emotelib.EmoteSounds(ROOT / "sounds", OUT_SR,
                                           lambda p: (decode_audio(p), REF_SR))
        self.emotes.prepare_folders()
        self._emote_cache = {}      # (голос, эмоция) -> сгенерированный звук
        self._emote_gen = {}        # эмоция -> сколько раз звук генерировался
        self._train_done = False
        self.progress = ""
        self.executor = ThreadPoolExecutor(max_workers=1)   # модель одна, работаем по очереди
        self.clients = set()
        self.history = deque(maxlen=150)
        self.pending = deque()
        self.gen = 0
        self.now = None
        self.paused = False
        self.last_spoke = {}
        self.loop = None
        self.brb = {"until": 0.0, "message": self.settings.get("brb_message") or "Скоро вернусь"}
        self.tapi = tapilib.TwitchApi(self.settings.get("twitch_client_id", ""),
                                      self.settings.get("twitch_token", ""))
        self._rewards = {}        # id награды -> название
        self._rewards_at = 0.0
        self._followers = set()
        self._followers_ready = False

    # ---------- события для браузера ----------
    def broadcast(self, event):
        if event.get("type") in ("chat", "note"):
            self.history.append(event)
        for q in list(self.clients):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                self.clients.discard(q)

    def status(self):
        return {
            "type": "status",
            "engine": self.engine.info(),
            "rvc": self.rvc.info(),
            "cosyvoice": self.cosyvoice.info(),
            "train": self.trainer.status(),
            "twitch": {"status": self.twitch.status, "detail": self.twitch.detail, "channel": self.twitch.channel},
            "queue": len(self.pending) + (1 if self.now else 0),
            "paused": self.paused,
            "now": self.now and {"id": self.now["id"], "user": self.now["user"], "voice": self.now["voice_name"]},
        }

    def push_status(self):
        self.broadcast(self.status())
        self.register_trained_voice()

    def register_trained_voice(self):
        """Когда обучение закончилось - добавляем модель в список голосов."""
        st = self.trainer.status()
        if st["state"] != "done" or self._train_done:
            return
        self._train_done = True
        res = st.get("result") or {}
        opts = self.trainer.opts or {}
        try:
            voice = self.voices.create_rvc(st.get("voice_name") or "Голос",
                                           st.get("aliases") or [],
                                           res.get("pth"), res.get("index"),
                                           opts.get("base", ""), int(opts.get("pitch") or 0))
            log.info("обученная модель добавлена как голос %s", voice["name"])
            self.broadcast({"type": "voices"})
        except Exception as e:
            log.exception("не удалось добавить обученную модель")
            self.trainer.set_error("Модель обучена, но добавить её в список не получилось: %s" % e)

    def push_status_threadsafe(self):
        if self.loop:
            self.loop.call_soon_threadsafe(self.push_status)

    def start_rvc(self):
        """Запускает RVC, если папка Applio известна (или находится сама)."""
        path = self.settings["applio_path"] or rvclib.find_applio(ROOT)
        if not path:
            return
        try:
            self.rvc.start(path)
            self.trainer.applio_dir = Path(path)
            if path != self.settings["applio_path"]:
                self.settings["applio_path"] = path
                self.save_settings()
        except Exception as e:
            log.warning("RVC не запущен: %s", e)
            self.rvc.status, self.rvc.error, self.rvc.path = "error", str(e), path

    def detect_cosyvoice(self):
        """Ищет папку с моделью CosyVoice 3, но не запускает её.

        Модель грузится лениво: пока она не нужна, видеокарта остаётся свободной.
        """
        path = self.settings["cosyvoice_path"] or cvlib.find_cosyvoice(ROOT)
        if path and path != self.settings["cosyvoice_path"]:
            self.settings["cosyvoice_path"] = path
            self.save_settings()
        return path

    def start_cosyvoice(self):
        """Запускает CosyVoice 3, если папка с моделью известна (или находится сама)."""
        path = self.detect_cosyvoice()
        if not path:
            return
        try:
            self.cosyvoice.start(path)
        except Exception as e:
            log.warning("CosyVoice 3 не запущен: %s", e)
            self.cosyvoice.status, self.cosyvoice.error = "error", str(e)

    def ensure_cosyvoice(self):
        """Гарантирует, что воркер CosyVoice 3 запущен."""
        if self.cosyvoice.status in ("ready", "starting"):
            return
        path = self.settings["cosyvoice_path"] or cvlib.find_cosyvoice(ROOT)
        if not path:
            raise cvlib.CosyVoiceError("Не найдена папка CosyVoice 3. Укажите её на вкладке «Голоса».")
        self.cosyvoice.start(path)

    def save_settings(self):
        self.settings.save()

    # ---------- голоса ----------
    def default_voice(self):
        enabled = self.voices.enabled()
        for v in enabled:
            if v["id"] == self.settings["default_voice"]:
                return v
        return enabled[0] if enabled else None

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

    def voice_for(self, login):
        enabled = self.voices.enabled()
        chosen = self.user_voices.get(login)
        for v in enabled:
            if v["id"] == chosen and not voice_blocked(self.settings, v):
                return v
        if self.settings["random_voice"] and enabled:
            n = int(hashlib.md5(login.encode()).hexdigest(), 16)
            return enabled[n % len(enabled)]
        return self.default_voice()

    # ---------- чат ----------
    def on_chat(self, m):
        """Сообщение из чата.

        Правила решают (app/chat_rules.py), App исполняет: озвучивает,
        пишет в чат, меняет настройки.
        """
        reward_title = ""
        if m.get("is_reward"):
            self.api_sync()
            reward_title = self.reward_title(m.get("reward_id", ""))
        ctx = chatrules.Context(settings=self.settings,
                                last_spoke=self.last_spoke.get(m["login"], 0),
                                queue_len=len(self.pending),
                                now=time.time(),
                                reward_title=reward_title)
        d = chatrules.decide(m, ctx)
        event = {"type": "chat", "id": m["id"] or uuid.uuid4().hex, "user": m["name"],
                 "color": m["color"], "text": d.text, "tts": False, "note": ""}
        if d.reward:
            event["reward"] = d.reward
        if d.highlight:
            event["highlight"] = True
        if d.kind == chatrules.COMMAND:
            self.broadcast(event)
            self.run_command(d, m)
            return
        if d.kind == chatrules.EVENT:
            self.broadcast(event)
            self.run_event(d, m)
            return
        if d.kind == chatrules.SKIP:
            event["note"] = d.note
            self.broadcast(event)
            return
        voice = self.voice_for(m["login"])
        if d.is_me and self.settings.get("me_voice"):
            mv = self.voices.get(self.settings["me_voice"])
            if mv and mv.get("enabled"):
                voice = mv
        if d.voice_hint:
            if d.voice_hint.lower() in ("random", "случайный", "рандом"):
                pool = self.voices.enabled()
                if pool:
                    voice = pool[int(time.time() * 1000) % len(pool)]
            else:
                picked = self.voices.resolve(d.voice_hint)
                if picked:
                    voice = picked
        if not voice:
            event["note"] = "нет голосов"
            self.broadcast(event)
            return
        self.last_spoke[m["login"]] = time.time()
        event["tts"] = True
        event["voice"] = voice["name"]
        self.broadcast(event)
        speak = ("%s говорит: %s" % (m["name"].replace("_", " "), d.speak_text)
                 if self.settings["read_names"] else d.speak_text)
        self.enqueue(event["id"], m["name"], speak, voice, d.style_key)

    def clean(self, text, emote_ranges=None, max_len=None):
        """Чистка текста с учётом настройки «эмоции»."""
        return clean_text(text, emote_ranges, int(max_len or self.settings["max_len"]),
                          emotes=bool(self.settings["emotes"]))

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
            # наложит общий RVC-проход в _synth, как и для записей из sounds/
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

    def enqueue(self, msg_id, user, speak_text, voice, style=""):
        self.pending.append({"id": msg_id, "user": user, "text": speak_text, "voice_id": voice["id"],
                             "voice_name": voice["name"], "style": style,
                             "vol": _vol_factor(voice.get("volume")),
                             "gen": self.gen})
        self.wake.set()
        self.push_status()

    # ---------- CosyVoice 3 с записями эмоций ----------
    def _cosy_parts(self, segs, voice):
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

    def _render_cosy(self, segs, voice, out_path, style=None):
        """Синтез CosyVoice 3 с учётом записей эмоций из sounds/ и стиля речи."""
        sp = style or {}
        parts = self._cosy_parts(segs, voice)
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
    def _synth(self, job):
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
                return out, self._render_cosy(segs, voice, out, sp)
            # Смешанный текст: CosyVoice 3 читает то, что умеет (китайский, русский,
            # английский), остальное уходит в XTTS - тембр остаётся тем же голосом.
            parts = []
            for is_cosy, group in groups:
                tmp = CACHE / ("part_%s.wav" % uuid.uuid4().hex)
                try:
                    if is_cosy:
                        # CosyVoice 3 читает свой кусок (и записи эмоций из sounds/)
                        self._render_cosy(group, voice, tmp, sp)
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

    async def worker(self):
        """Берёт сообщения по одному: озвучивает, отправляет в браузер, ждёт конца звучания."""
        while True:
            if not self.pending or self.paused or self.engine.status != "ready":
                self.wake.clear()
                try:
                    await asyncio.wait_for(self.wake.wait(), timeout=1.0)
                except asyncio.TimeoutError:
                    pass
                continue
            job = self.pending.popleft()
            self.now = job
            self.push_status()
            self.skip_event.clear()
            try:
                path, dur = await self.loop.run_in_executor(self.executor, self._synth, job)
                if job["gen"] == self.gen and not self.skip_event.is_set():
                    self.broadcast({"type": "speak", "id": job["id"], "url": "/audio/" + path.name,
                                    "user": job["user"], "voice": job["voice_name"],
                                    "voice_id": job.get("voice_id", ""),
                                    "vol": job.get("vol", 1.0), "duration": dur})
                    try:
                        await asyncio.wait_for(self.skip_event.wait(), timeout=dur + 0.35)
                        self.broadcast({"type": "stop"})
                    except asyncio.TimeoutError:
                        pass
            except Exception as e:
                log.exception("ошибка озвучки")
                self.broadcast({"type": "tts_error", "id": job["id"], "error": str(e)})
            self.broadcast({"type": "done", "id": job["id"]})
            self.now = None
            self.push_status()
            self.cleanup_cache()

    # ---------- Twitch API ----------
    def api_ready(self):
        """Задан ли Client ID и токен Twitch."""
        return bool(str(self.settings.get("twitch_client_id") or "").strip()
                    and str(self.settings.get("twitch_token") or "").strip())

    def api_sync(self):
        """Обновляет список наград канала (кэш на 5 минут)."""
        self.tapi = tapilib.TwitchApi(self.settings.get("twitch_client_id", ""),
                                      self.settings.get("twitch_token", ""))
        if not self.tapi.ready:
            return
        now = time.time()
        if now - self._rewards_at < 300:
            return
        self._rewards_at = now
        try:
            bid = self.tapi.user_id(self.settings.get("channel", ""))
            if not bid:
                return
            for reward in self.tapi.rewards(bid):
                self._rewards[reward.get("id", "")] = reward.get("title", "")
        except Exception as e:
            log.warning("не удалось обновить награды Twitch: %r", e)

    def reward_title(self, reward_id):
        """Название награды по её id (из кэша Twitch API)."""
        return self._rewards.get(reward_id or "", "")

    def _do_timeout(self, login, seconds):
        """Таймаут зрителя через Twitch API. Возвращает (получилось, текст)."""
        if not self.api_ready():
            return False, "Нужен Client ID и токен Twitch (Настройки -> События и интеграции)."
        try:
            bid = self.tapi.user_id(self.settings.get("channel", ""))
            uid = self.tapi.user_id(login)
            if not uid:
                return False, "Зритель %s не найден." % login
            self.tapi.timeout(bid, uid, seconds)
        except Exception as e:
            log.warning("таймаут не выполнен: %r", e)
            return False, "Таймаут не выполнен: %s" % e
        return True, "%s получил таймаут на %d с." % (login, int(seconds))

    def _fetch_new_followers(self):
        """Новые подписчики канала (список имён)."""
        if not self.api_ready() or not self.settings.get("channel"):
            return []
        bid = self.tapi.user_id(self.settings.get("channel", ""))
        if not bid:
            return []
        ids = set()
        names = {}
        for f in self.tapi.followers(bid):
            uid = f.get("user_id", "")
            ids.add(uid)
            names[uid] = f.get("user_login") or f.get("user_name") or uid
        if not self._followers_ready:
            self._followers = ids
            self._followers_ready = True
            return []
        new = [names.get(u, u) for u in (ids - self._followers)]
        self._followers = ids
        return new

    # ---------- события, команды, BRB ----------
    def spawn(self, coro):
        """Запускает корутину из обработчика чата."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            coro.close()
            return None
        try:
            if self.loop is not None:
                return self.loop.create_task(coro)
            return loop.create_task(coro)
        except RuntimeError:
            coro.close()
            return None

    def run_event(self, d, m):
        """Событие Twitch: объявить в чате и (по правилу) озвучить."""
        if d.rule == "ignore" or not d.event_text:
            return
        ev = {"type": "chat", "id": m.get("id") or uuid.uuid4().hex,
              "user": m.get("name") or "", "color": "#f59e0b", "text": d.event_text,
              "tts": False, "event": d.event, "highlight": True}
        self.broadcast(ev)
        if d.rule == "speak":
            voice = self.default_voice()
            if voice:
                self.enqueue(ev["id"], ev["user"], d.event_text, voice)

    def run_command(self, d, m):
        """Исполняет команду чата: решение принято в chat_rules, действие здесь."""
        cmd = d.command
        rest = (d.args or "").strip()
        if cmd == "voice":
            if not rest or rest.lower() in ("default", "сброс", "по умолчанию"):
                self.user_voices.pop(m["login"], None)
                save_json(DATA / "user_voices.json", self.user_voices)
                self.broadcast({"type": "note", "text": "%s снова говорит голосом канала" % m["name"]})
                return
            voice = self.voices.resolve(rest)
            if voice and voice_blocked(self.settings, voice):
                self.broadcast({"type": "note", "text": "%s: голос «%s» запрещён" % (m["name"], voice["name"])})
                return
            if voice:
                self.user_voices[m["login"]] = voice["id"]
                save_json(DATA / "user_voices.json", self.user_voices)
                self.broadcast({"type": "note", "text": "%s теперь говорит голосом «%s»" % (m["name"], voice["name"])})
            else:
                self.broadcast({"type": "note", "text": "%s: голос «%s» не найден" % (m["name"], rest)})
            return
        if cmd == "skip":
            self.skip()
            return
        if cmd == "rate":
            amount, src, dst = featlib.parse_rate(rest)
            if not amount or not src:
                self.broadcast({"type": "note", "text": "Формат: !курс 100 usd [rub]"})
                return
            self.spawn(self._rate_reply(amount, src, dst))
            return
        if cmd == "translate":
            if not rest:
                self.broadcast({"type": "note", "text": "Формат: !перевод привет [en]"})
                return
            rest, lang = featlib.split_language(rest, "ru")
            self.spawn(self._translate_reply(rest, lang))
            return
        if cmd == "highlight":
            self.broadcast({"type": "chat", "id": uuid.uuid4().hex, "user": m["name"],
                            "color": m["color"], "text": rest or "(пусто)", "tts": False,
                            "highlight": True})
            return
        if cmd == "pin":
            self.broadcast({"type": "pin", "text": rest or "(пусто)", "user": m["name"]})
            return
        if cmd == "brb":
            seconds, message = featlib.parse_brb(rest)
            self.set_brb(seconds, message)
            return
        if cmd == "timeout":
            parts = rest.split()
            if not parts:
                self.broadcast({"type": "note", "text": "Формат: !таймаут ник 600"})
                return
            target = parts[0].lstrip("@").lower()
            seconds = 600
            if len(parts) > 1 and parts[1].isdigit():
                seconds = max(1, min(int(parts[1]), 1209600))
            self.spawn(self._timeout_reply(target, seconds))
            return

    async def _timeout_reply(self, login, seconds):
        """Ответ на команду таймаута."""
        try:
            loop = asyncio.get_running_loop()
            done, text = await loop.run_in_executor(None, self._do_timeout, login, seconds)
        except Exception as e:
            log.warning("таймаут не выполнен: %r", e)
            done, text = False, "Таймаут не выполнен: %s" % e
        await self._reply(text, speak=False)
        return done

    async def poll_followers(self):
        """Следит за новыми подписчиками (нужен токен Twitch)."""
        while True:
            await asyncio.sleep(60)
            if not self.api_ready() or not self.settings.get("channel"):
                continue
            rule = str(self.settings.get("follow_rule") or "ignore").lower()
            if rule == "ignore":
                continue
            try:
                loop = asyncio.get_running_loop()
                names = await loop.run_in_executor(None, self._fetch_new_followers)
            except Exception as e:
                log.warning("ошибка проверки подписчиков: %r", e)
                continue
            for name in names[:5]:
                text = "Новый подписчик: %s" % name
                ev = {"type": "chat", "id": uuid.uuid4().hex, "user": name, "color": "#34d399",
                      "text": text, "tts": False, "event": "follow", "highlight": True}
                self.broadcast(ev)
                if rule == "speak":
                    voice = self.default_voice()
                    if voice:
                        self.enqueue(ev["id"], name, text, voice)

    async def _reply(self, text, speak=True):
        """Ответ в чат и (по желанию) голосом."""
        self.broadcast({"type": "note", "text": text})
        if not speak:
            return
        voice = self.default_voice()
        if voice:
            self.enqueue(uuid.uuid4().hex, "Бот", text, voice)

    async def _rate_reply(self, amount, src, dst):
        try:
            loop = asyncio.get_running_loop()
            value = await loop.run_in_executor(None, featlib.convert, amount, src, dst)
        except Exception as e:
            log.warning("не удалось узнать курс: %r", e)
            value = None
        if value is None:
            await self._reply("Не удалось узнать курс %s." % src.upper())
            return
        await self._reply("%s %s = %s %s" % (featlib.fmt_num(amount, 0), src.upper(),
                                            featlib.fmt_num(value), dst.upper()))

    async def _translate_reply(self, text, lang="ru"):
        try:
            loop = asyncio.get_running_loop()
            got = await loop.run_in_executor(None, featlib.translate_safe, text, lang)
        except Exception as e:
            log.warning("не удалось перевести: %r", e)
            got = ""
        if not got:
            await self._reply("Не удалось перевести текст.")
            return
        await self._reply(got)

    def brb_state(self):
        """Состояние BRB-экрана."""
        until = float(self.brb.get("until") or 0)
        left = max(0.0, until - time.time())
        return {"until": until, "left": left, "active": left > 0,
                "message": self.brb.get("message") or ""}

    def set_brb(self, seconds, message=""):
        """Включает или выключает BRB-экран с таймером."""
        try:
            seconds = max(0, int(seconds or 0))
        except (TypeError, ValueError):
            seconds = 0
        msg = (message or "").strip() or (self.settings.get("brb_message") or "Скоро вернусь")
        self.brb = {"until": (time.time() + seconds) if seconds else 0.0, "message": msg}
        self.settings["brb_seconds"] = seconds
        self.settings["brb_message"] = msg
        self.save_settings()
        self.broadcast({"type": "brb", **self.brb_state()})
        return self.brb_state()

    def skip(self):
        self.skip_event.set()

    def clear(self):
        self.gen += 1
        for job in self.pending:
            self.broadcast({"type": "done", "id": job["id"]})
        self.pending.clear()
        self.skip()
        self.push_status()

    def cleanup_cache(self):
        limit = time.time() - 300
        for f in CACHE.glob("*.wav"):
            try:
                if f.stat().st_mtime < limit:
                    f.unlink()
            except OSError:
                pass

    async def watch_engine(self):
        """Сообщает браузеру, когда модель загрузилась."""
        while self.engine.status == "loading":
            await asyncio.sleep(1)
        self.push_status()
        self.broadcast({"type": "voices"})
        self.wake.set()

    async def startup(self):
        self.loop = asyncio.get_running_loop()
        self.wake = asyncio.Event()
        self.skip_event = asyncio.Event()
        shutil.rmtree(TMP, ignore_errors=True)
        TMP.mkdir(exist_ok=True)
        threading.Thread(target=self.engine.load, daemon=True).start()
        self.start_rvc()
        self.detect_cosyvoice()
        self.tasks = [asyncio.create_task(self.worker()),
                      asyncio.create_task(self.watch_engine()),
                      asyncio.create_task(self.poll_followers())]
        if self.settings["autoconnect"] and self.settings["channel"]:
            await self.twitch.connect(self.settings["channel"])


app_state = App()


routes = apilib.build_routes(app_state)

@contextlib.asynccontextmanager
async def lifespan(_app):
    await app_state.startup()
    yield


class PrivateNetworkMiddleware:
    """Запасной вариант для старых Starlette: добавляет заголовок для запросов
    с локальной страницы (file://) к 127.0.0.1 (Private Network Access)."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = dict(scope.get("headers") or {})
        if headers.get(b"access-control-request-private-network") != b"true":
            await self.app(scope, receive, send)
            return

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                raw = message.setdefault("headers", [])
                if not any(k.lower() == b"access-control-allow-private-network" for k, _ in raw):
                    raw.append((b"access-control-allow-private-network", b"true"))
            await send(message)

        await self.app(scope, receive, send_wrapper)


app = Starlette(routes=routes, lifespan=lifespan)
# Страница голосования открывается как локальный файл (origin "null") и должна
# обращаться к API - поэтому разрешаем локальные источники.
if CORSMiddleware is not None:
    cors_kwargs = dict(
        allow_origins=[],
        allow_origin_regex=r"^(null|https?://(127\.0\.0\.1|localhost)(:\d+)?)$",
        allow_credentials=False,
        allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
        allow_headers=["*"],
        expose_headers=[],
        max_age=600,
    )
    if "allow_private_network" in inspect.signature(CORSMiddleware.__init__).parameters:
        cors_kwargs["allow_private_network"] = True
    app.add_middleware(CORSMiddleware, **cors_kwargs)
app.add_middleware(PrivateNetworkMiddleware)


if __name__ == "__main__":
    url = "http://%s:%d" % (HOST, PORT)
    print("\n  Озвучка чата Twitch запущена: %s\n  Чтобы выключить, закройте это окно.\n" % url)
    if "--no-browser" not in sys.argv:
        threading.Timer(1.5, lambda: webbrowser.open(url)).start()
    try:
        uvicorn.run(app, host=HOST, port=PORT, log_level="warning", timeout_graceful_shutdown=1)
    finally:
        app_state.rvc.stop()
