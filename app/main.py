# -*- coding: utf-8 -*-
"""Озвучка чата Twitch. Запуск: start.bat (или python app/main.py)."""
import asyncio
import contextlib
import hashlib
import inspect
import json
import logging
import math
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

APP_DIR = Path(__file__).resolve().parent
ROOT = APP_DIR.parent
sys.path.insert(0, str(APP_DIR))

import uvicorn  # noqa: E402
from starlette.applications import Starlette  # noqa: E402
try:
    from starlette.middleware.cors import CORSMiddleware  # noqa: E402
except Exception:  # очень старые версии Starlette - работаем без CORS
    CORSMiddleware = None
from starlette.responses import FileResponse, JSONResponse, RedirectResponse, StreamingResponse  # noqa: E402
from starlette.routing import Mount, Route  # noqa: E402
from starlette.staticfiles import StaticFiles  # noqa: E402

from engine import OUT_SR, REF_SR, MockEngine, XttsEngine, write_wav  # noqa: E402
import rvc as rvclib  # noqa: E402
import cosyvoice as cvlib  # noqa: E402
from textproc import clean_text, fix_layout, segments, strip_stress  # noqa: E402
import styles as stylelib  # noqa: E402
import features as featlib  # noqa: E402
import emotes as emotelib  # noqa: E402
import langs  # noqa: E402
from twitch import TwitchChat, normalize_channel  # noqa: E402
import twitch_api as tapilib  # noqa: E402
from voices import VoiceError, VoiceStore, decode as decode_audio, measure_f0  # noqa: E402
from rvc_train import RvcTrainer, TrainError  # noqa: E402

HOST = os.environ.get("TTS_HOST", "127.0.0.1")
PORT = int(os.environ.get("TTS_PORT", "8765"))
DATA = ROOT / "data"
CACHE = DATA / "cache"
TMP = DATA / "tmp"
WEB = APP_DIR / "web"
MAX_UPLOAD = 500 * 1024 * 1024

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


DEFAULTS = {
    "channel": "",
    "autoconnect": True,
    "default_voice": "",
    "random_voice": False,     # зрителям без выбранного голоса давать постоянный случайный
    "volume": 0.9,           # 0.0 - 2.0 (0-200 %)
    "muted": False,          # мьют озвучки чата: реплики не читаются
    "speed": 1.0,
    "language": "auto",        # auto | ru | en
    "max_len": 200,
    "max_queue": 15,
    "cooldown": 0,             # секунд между сообщениями одного зрителя
    "audience": "all",         # all | subs (подписчики, VIP, модераторы)
    "read_names": False,
    "skip_commands": True,
    "skip_digits": True,       # не читать сообщения из одних цифр (голоса в опросах)
    "skip_rewards": True,      # не читать сообщения наград за баллы канала
    "emotes": True,            # (смех), *вздыхает* - звучат как эмоция, а не читаются словом
    "ignore_users": ["nightbot", "streamelements", "moobot", "fossabot", "streamlabs", "wizebot"],
    "blocked_words": [],
    "disabled_voices": [],
    "applio_path": "",
    "cosyvoice_path": "",
    "tts_prefix": "",          # читать только сообщения с этим префиксом (пусто - все)
    "fix_layout": False,       # "Ghbdtn vbh" -> "Привет мир"
    "style": "",               # стиль речи по умолчанию: happy/sad/angry/whisper/calm
    "me_voice": "",            # голос для действий /me (пусто - голос по умолчанию)
    "blocked_voices": [],      # голоса, которые зрители не могут выбрать
    "tts_allow": [],           # озвучивать только этих зрителей
    "tts_deny": [],            # не озвучивать этих зрителей
    "player_only": False,      # звук только на странице-плеере (для OBS)
    "highlight_rewards": False,  # выделять сообщения, отправленные за награду
    "event_rules": {},         # правила по событиям: рейд/подписка/битсы -> announce|speak|ignore
    "brb_seconds": 0,          # таймер BRB-экрана, секунд
    "brb_message": "Скоро вернусь",
    "twitch_client_id": "",     # Client ID приложения Twitch (нужен для API)
    "twitch_token": "",         # OAuth-токен Twitch: модератора или стримера
    "highlight_reward": "",     # название награды, сообщения из которой выделяются
    "speak_reward": "",         # название награды, сообщения из которой озвучиваются
    "follow_rule": "ignore",    # реакция на нового подписчика: ignore|announce|speak
}

def voice_blocked(settings, voice):
    """Голос запрещён для зрителей (список «Запрещённые голоса»)."""
    names = {str(x).strip().lower() for x in (settings.get("blocked_voices") or []) if str(x).strip()}
    if not names or not voice:
        return False
    return (str(voice.get("name", "")).strip().lower() in names
            or str(voice.get("id", "")) in names)


def login_allowed(settings, login):
    """Кого озвучивать: список «только своих» и список «не озвучивать»."""
    lg = str(login or "").strip().lower()
    deny = {str(x).strip().lower() for x in (settings.get("tts_deny") or []) if str(x).strip()}
    allow = {str(x).strip().lower() for x in (settings.get("tts_allow") or []) if str(x).strip()}
    if lg and lg in deny:
        return False
    if allow and lg not in allow:
        return False
    return True


def split_prefix(text, prefix):
    """Отделить префикс озвучки от текста: ("привет", "!!!") или ("привет", "").

    Префикс задаёт стример (например "!!!"): такие сообщения озвучиваются
    голосом зрителя, а сам префикс не читается.
    """
    if not prefix:
        return text, ""
    p = prefix.strip()
    if text.lower().startswith(p.lower()):
        return text[len(p):].strip(), p
    return text, ""


def split_voice_prefix(text):
    """«!!!#дудя привет» -> («дудя», «привет»). Голос только для этого сообщения."""
    if not text.startswith("!!!#"):
        return "", text
    head, _, tail = text[4:].partition(" ")
    return head.strip(), tail.strip()


for d in (DATA, CACHE, TMP):
    d.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    handlers=[logging.StreamHandler(), logging.FileHandler(DATA / "log.txt", encoding="utf-8")])
log = logging.getLogger("app")


def load_json(path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return default


def save_json(path, data):
    tmp = Path(str(path) + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


class App:
    def __init__(self):
        self.settings = dict(DEFAULTS)
        self.settings.update({k: v for k, v in load_json(DATA / "settings.json", {}).items() if k in DEFAULTS})
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
        save_json(DATA / "settings.json", self.settings)

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
        s = self.settings
        text = m["text"].strip()
        # действие /me - отдельный голос, префикс не читается
        is_me = text.lower().startswith("/me ")
        if is_me:
            text = text[4:].strip()
        # голос только для этого сообщения: !!!#дудя привет
        one_voice, rest_text = split_voice_prefix(text)
        # префикс озвучки (например "!!!"): без него сообщение не читается
        prefix = str(s.get("tts_prefix") or "").strip()
        text, used_prefix = split_prefix(text, prefix)
        no_prefix = bool(prefix) and not used_prefix
        if one_voice:
            text = rest_text
        if s.get("fix_layout"):
            text = fix_layout(text)
        text = strip_stress(text)
        # стиль речи: (радостно) привет - читаем радостно
        style_key, speak_text = stylelib.match_style(text)
        event = {"type": "chat", "id": m["id"] or uuid.uuid4().hex, "user": m["name"],
                 "color": m["color"], "text": text, "tts": False, "note": ""}
        low = text.lower()
        first, _, rest = low.partition(" ")
        first_raw, _, rest_raw = text.partition(" ")

        if first in ("!г", "!голос", "!voice", "!tts", "!ттс"):
            self.broadcast(event)
            if not rest.strip() or rest.strip().lower() in ("default", "сброс", "по умолчанию"):
                self.user_voices.pop(m["login"], None)
                save_json(DATA / "user_voices.json", self.user_voices)
                self.broadcast({"type": "note", "text": "%s снова говорит голосом канала" % m["name"]})
                return
            voice = self.voices.resolve(rest)
            if voice and voice_blocked(s, voice):
                self.broadcast({"type": "note", "text": "%s: голос «%s» запрещён" % (m["name"], voice["name"])})
                return
            if voice:
                self.user_voices[m["login"]] = voice["id"]
                save_json(DATA / "user_voices.json", self.user_voices)
                self.broadcast({"type": "note", "text": "%s теперь говорит голосом «%s»" % (m["name"], voice["name"])})
            else:
                self.broadcast({"type": "note", "text": "%s: голос «%s» не найден" % (m["name"], rest.strip())})
            return
        if first in ("!скип", "!skip") and (m["is_mod"] or m["is_broadcaster"]):
            self.broadcast(event)
            self.skip()
            return

        # события Twitch: рейд, подписка, битсы - по системным тегам IRC
        kind = featlib.detect_event(m.get("msg_id", ""), m.get("system_msg", ""))
        if kind:
            self.broadcast(event)
            self.handle_event(kind, m)
            return

        # команды: курс валют, перевод, выделение, закрепление, BRB
        if self.handle_command(m, first_raw.lower(), rest_raw, event):
            return

        # сообщение, отправленное за награду: выделяем и/или озвучиваем по названию
        reward_speak = False
        if m.get("is_reward"):
            self.api_sync()
            title = self.reward_title(m.get("reward_id", ""))
            if title:
                event["reward"] = title
            low_title = title.lower()
            if low_title and low_title == str(s.get("highlight_reward") or "").strip().lower():
                event["highlight"] = True
            if low_title and low_title == str(s.get("speak_reward") or "").strip().lower():
                reward_speak = True
            elif s.get("highlight_rewards"):
                event["highlight"] = True

        note = ""
        if s.get("muted"):
            note = "озвучка выключена"
        elif no_prefix:
            note = "без префикса"
        elif m["login"] in [u.lower() for u in s["ignore_users"]]:
            note = "бот"
        elif not login_allowed(s, m["login"]):
            note = "не озвучивать этого зрителя"
        elif text.startswith("!") and s["skip_commands"]:
            note = "команда"
        elif s["skip_digits"] and text.strip().isdigit():
            # голоса в опросах: зритель пишет "1", "2" или "3" - вслух это не читаем
            note = "цифра"
        elif m.get("is_reward") and s["skip_rewards"] and not reward_speak:
            note = "награда за баллы"
        elif s["audience"] == "subs" and not (m["is_sub"] or m["is_vip"] or m["is_mod"] or m["is_broadcaster"]):
            note = "только для подписчиков"
        elif any(w and w.lower() in low for w in s["blocked_words"]):
            note = "запрещённое слово"
        elif s["cooldown"] and time.time() - self.last_spoke.get(m["login"], 0) < s["cooldown"]:
            note = "слишком часто"
        if note:
            event["note"] = note
            self.broadcast(event)
            return

        cleaned = self.clean(speak_text or text, m["emotes"])
        if not cleaned:
            event["note"] = "нечего читать"
            self.broadcast(event)
            return
        if len(self.pending) >= int(s["max_queue"]):
            event["note"] = "очередь заполнена"
            self.broadcast(event)
            return
        voice = self.voice_for(m["login"])
        if is_me and s.get("me_voice"):
            mv = self.voices.get(s["me_voice"])
            if mv and mv.get("enabled"):
                voice = mv
        if one_voice:
            if one_voice.lower() in ("random", "случайный", "рандом"):
                pool = self.voices.enabled()
                if pool:
                    voice = pool[int(time.time() * 1000) % len(pool)]
            else:
                picked = self.voices.resolve(one_voice)
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
        speak = ("%s говорит: %s" % (m["name"].replace("_", " "), cleaned)) if s["read_names"] else cleaned
        self.enqueue(event["id"], m["name"], speak, voice, style_key)

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

    def handle_event(self, kind, m):
        """Правило на событие Twitch: рейд, подписка, битсы."""
        rules = self.settings.get("event_rules") or {}
        action = str(rules.get(kind, featlib.DEFAULT_RULES.get(kind, "ignore"))).lower()
        if action == "ignore":
            return
        text = featlib.event_text(kind, m.get("name", ""), m.get("system_msg", ""))
        if not text:
            return
        ev = {"type": "chat", "id": m.get("id") or uuid.uuid4().hex,
              "user": m.get("name") or "", "color": "#f59e0b", "text": text,
              "tts": False, "event": kind, "highlight": True}
        self.broadcast(ev)
        if action == "speak":
            voice = self.default_voice()
            if voice:
                self.enqueue(ev["id"], ev["user"], text, voice)

    def handle_command(self, m, cmd, rest, event):
        """Команды чата. True - сообщение обработано, озвучивать его не нужно."""
        mod = bool(m["is_mod"] or m["is_broadcaster"])
        rest = (rest or "").strip()
        if cmd in ("!курс", "!курсы", "!rate", "!fx", "!валюта"):
            self.broadcast(event)
            amount, src, dst = featlib.parse_rate(rest)
            if not amount or not src:
                self.broadcast({"type": "note", "text": "Формат: !курс 100 usd [rub]"})
                return True
            self.spawn(self._rate_reply(amount, src, dst))
            return True
        if cmd in ("!перевод", "!переведи", "!translate", "!tr"):
            self.broadcast(event)
            if not rest:
                self.broadcast({"type": "note", "text": "Формат: !перевод привет [en]"})
                return True
            rest, lang = featlib.split_language(rest, "ru")
            self.spawn(self._translate_reply(rest, lang))
            return True
        if cmd in ("!выделить", "!highlight") and mod:
            self.broadcast(event)
            self.broadcast({"type": "chat", "id": uuid.uuid4().hex, "user": m["name"],
                            "color": m["color"], "text": rest or "(пусто)", "tts": False,
                            "highlight": True})
            return True
        if cmd in ("!закрепить", "!pin") and mod:
            self.broadcast(event)
            self.broadcast({"type": "pin", "text": rest or "(пусто)", "user": m["name"]})
            return True
        if cmd in ("!брб", "!brb") and mod:
            self.broadcast(event)
            seconds, message = featlib.parse_brb(rest)
            self.set_brb(seconds, message)
            return True
        if cmd in ("!таймаут", "!timeout", "!то") and mod:
            self.broadcast(event)
            parts = rest.split()
            if not parts:
                self.broadcast({"type": "note", "text": "Формат: !таймаут ник 600"})
                return True
            target = parts[0].lstrip("@").lower()
            seconds = 600
            if len(parts) > 1 and parts[1].isdigit():
                seconds = max(1, min(int(parts[1]), 1209600))
            self.spawn(self._timeout_reply(target, seconds))
            return True
        return False

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


# ---------- HTTP ----------
def ok(**data):
    return JSONResponse({"ok": True, **data})


def fail(message, code=400):
    return JSONResponse({"ok": False, "error": message}, status_code=code)


async def body(request):
    try:
        data = await request.json()
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


async def page_index(request):
    return FileResponse(WEB / "index.html")


async def page_root(request):
    """Главная страница теперь - вкладка «Опрос»."""
    return RedirectResponse("/poll", status_code=303)


async def page_voices(request):
    return FileResponse(WEB / "voices.html")


async def page_overlay(request):
    return FileResponse(WEB / "overlay.html")


async def page_poll(request):
    return FileResponse(WEB / "poll.html")


async def page_player(request):
    """Плеер озвучки отдельной страницей - для OBS (свой звуковой канал)."""
    return FileResponse(WEB / "player.html")


async def page_brb(request):
    """BRB-экран с таймером."""
    return FileResponse(WEB / "brb.html")


async def api_brb(request):
    """BRB-экран: GET - текущее состояние, POST - поставить таймер."""
    a = app_state
    if request.method == "POST":
        data = await body(request)
        try:
            seconds = int(data.get("seconds") or 0)
        except (TypeError, ValueError):
            seconds = 0
        a.set_brb(seconds, str(data.get("message") or ""))
    return ok(brb=a.brb_state())


async def events(request):
    a = app_state
    q = asyncio.Queue(maxsize=1000)
    a.clients.add(q)

    async def stream():
        try:
            yield "retry: 2000\n\n"
            hello = {"type": "hello", "history": list(a.history), "status": a.status()}
            yield "data: %s\n\n" % json.dumps(hello, ensure_ascii=False)
            while True:
                try:
                    ev = await asyncio.wait_for(q.get(), timeout=15)
                    yield "data: %s\n\n" % json.dumps(ev, ensure_ascii=False)
                except asyncio.TimeoutError:
                    yield ": ping\n\n"
        finally:
            a.clients.discard(q)

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


async def api_state(request):
    a = app_state
    return ok(status=a.status(), settings=a.settings, voices=a.voices.list(), brb=a.brb_state())


async def api_settings(request):
    a = app_state
    data = await body(request)
    was_muted = bool(a.settings.get("muted"))
    for key, default in DEFAULTS.items():
        if key not in data or key in ("channel", "disabled_voices", "applio_path", "cosyvoice_path"):
            continue
        val = data[key]
        try:
            if isinstance(default, bool):
                val = bool(val)
            elif isinstance(default, int):
                val = max(0, min(int(val), 2000))
            elif isinstance(default, float):
                val = float(val)
            elif isinstance(default, list):
                val = [str(x).strip() for x in val if str(x).strip()][:200]
            elif isinstance(default, dict):
                if not isinstance(val, dict):
                    continue
                val = {str(k)[:40]: str(v)[:40] for k, v in list(val.items())[:50]}
            else:
                val = str(val)
        except (TypeError, ValueError):
            continue
        a.settings[key] = val
    a.settings["speed"] = max(0.7, min(a.settings["speed"], 1.5))
    a.settings["volume"] = max(0.0, min(a.settings["volume"], 2.0))
    a.settings["max_len"] = max(20, min(a.settings["max_len"], 500))
    a.settings["max_queue"] = max(1, min(a.settings["max_queue"], 100))
    a.save_settings()
    if a.settings.get("muted") and not was_muted:
        # мьют: замолкаем сразу - текущая реплика и вся очередь
        a.clear()
    a.broadcast({"type": "settings", "settings": a.settings})
    return ok(settings=a.settings)


async def api_connect(request):
    a = app_state
    data = await body(request)
    channel = normalize_channel(data.get("channel", ""))
    if not channel:
        return fail("Введите имя канала, например: mychannel")
    a.settings["channel"] = channel
    a.save_settings()
    await a.twitch.connect(channel)
    return ok(channel=channel)


async def api_disconnect(request):
    await app_state.twitch.disconnect()
    return ok()


async def api_control(request):
    a = app_state
    action = request.path_params["action"]
    if action == "skip":
        a.skip()
    elif action == "clear":
        a.clear()
    elif action == "pause":
        a.paused = True
    elif action == "resume":
        a.paused = False
        a.wake.set()
    else:
        return fail("неизвестная команда", 404)
    a.push_status()
    return ok()


async def api_say(request):
    """Проверочная фраза: идёт через общую очередь, как сообщение из чата."""
    a = app_state
    data = await body(request)
    style_key, raw_text = stylelib.match_style(str(data.get("text", "")))
    text = a.clean(raw_text)
    if not text:
        return fail("Введите текст")
    voice = a.voices.get(str(data.get("voice", ""))) or a.default_voice()
    if not voice:
        return fail("Нет доступных голосов")
    if a.engine.status != "ready":
        return fail("Модель озвучки ещё не готова")
    event = {"type": "chat", "id": uuid.uuid4().hex, "user": "Проверка", "color": "#9aa4b2",
             "text": text, "tts": True, "note": "", "voice": voice["name"]}
    a.broadcast(event)
    a.enqueue(event["id"], "Проверка", text, voice, style_key)
    return ok()


async def api_preview(request):
    """Прослушать голос на странице голосов (мимо очереди)."""
    a = app_state
    data = await body(request)
    voice = a.voices.get(str(data.get("voice", "")))
    text = a.clean(str(data.get("text", "")), None, 200) or "Привет, чат! Это проверка голоса."
    if not voice:
        return fail("Голос не найден", 404)
    if a.engine.status != "ready":
        return fail("Модель озвучки ещё не готова")
    job = {"text": text, "voice_id": voice["id"]}
    try:
        path, _ = await a.loop.run_in_executor(a.executor, a._synth, job)
    except Exception as e:
        log.exception("ошибка прослушивания")
        return fail("Ошибка озвучки: %s" % e, 500)
    return ok(url="/audio/" + path.name)


async def api_announce(request):
    """Синтез одной фразы вне очереди - для внешних страниц (например, голосования)."""
    a = app_state
    data = await body(request)
    text = a.clean(str(data.get("text", "")))
    if not text:
        return fail("Пустой текст")
    voice = a.voices.get(str(data.get("voice", ""))) or a.default_voice()
    if not voice:
        return fail("Нет доступных голосов")
    if a.engine.status != "ready":
        return fail("Модель озвучки ещё не готова", 503)
    job = {"text": text, "voice_id": voice["id"]}
    try:
        path, dur = await a.loop.run_in_executor(a.executor, a._synth, job)
    except Exception as e:
        log.exception("ошибка озвучки")
        return fail("Ошибка озвучки: %s" % e, 500)
    return ok(url="/audio/" + path.name, duration=dur, voice=voice["name"])


async def api_emotes(request):
    """Список эмоций, записи в sounds/ и счётчик звуков, сгенерированных голосом."""
    a = app_state
    return ok(enabled=bool(a.settings["emotes"]), folder=str(a.emotes.root),
              emotes=a.emotes.summary(), generated=dict(a._emote_gen))


async def api_emotes_reset(request):
    """Сбрасывает кэш сгенерированных звуков - они будут созданы заново."""
    a = app_state
    a._emote_cache.clear()
    return ok(generated=dict(a._emote_gen))


async def api_voices(request):
    return ok(voices=app_state.voices.list())


async def api_voice_create(request):
    a = app_state
    work = TMP / uuid.uuid4().hex
    work.mkdir(parents=True)
    try:
        form = await request.form(max_files=40, max_part_size=MAX_UPLOAD)
        name = str(form.get("name", ""))
        aliases = [x for x in str(form.get("aliases", "")).replace(";", ",").split(",")]
        paths, names, total = [], {}, 0
        for n, f in enumerate(form.getlist("files")):
            if not hasattr(f, "read"):
                continue
            ext = os.path.splitext(f.filename or "")[1][:8]
            ext = "".join(c for c in ext if c.isalnum() or c == ".")
            dst = work / ("src_%02d%s" % (n, ext))
            with open(dst, "wb") as out:
                while True:
                    chunk = await f.read(1024 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > MAX_UPLOAD:
                        return fail("Файлы слишком большие (больше 500 МБ).")
                    out.write(chunk)
            paths.append(dst)
            names[str(dst)] = os.path.basename(f.filename or dst.name)[:80]
        if not paths:
            return fail("Добавьте хотя бы один файл с голосом.")
        try:
            a.voices.check_name(name, aliases)
            voice, report = await a.loop.run_in_executor(a.executor, a.voices.create, name, aliases, paths, names)
        except VoiceError as e:
            return fail(str(e))
        except Exception as e:
            log.exception("ошибка создания голоса")
            return fail("Не получилось создать голос: %s" % e, 500)
        a.broadcast({"type": "voices"})
        return ok(voice=voice, report=report)
    finally:
        shutil.rmtree(work, ignore_errors=True)



async def api_voice_pitch(request):
    """Сдвиг тона своего голоса (полутона)."""
    a = app_state
    data = await body(request)
    try:
        voice = a.voices.update_pitch(str(data.get("voice", "")), data.get("pitch", 0))
    except VoiceError as e:
        return fail(str(e))
    except Exception as e:
        log.exception("ошибка сдвига тона")
        return fail("Не удалось изменить тон: %s" % e, 500)
    a.broadcast({"type": "voices"})
    return ok(voice=voice)


async def api_voice_volume(request):
    """Своя громкость голоса (проценты, 0-200)."""
    a = app_state
    data = await body(request)
    try:
        voice = a.voices.update_volume(str(data.get("voice", "")), data.get("volume", 100))
    except VoiceError as e:
        return fail(str(e))
    except Exception as e:
        log.exception("ошибка громкости голоса")
        return fail("Не удалось изменить громкость: %s" % e, 500)
    a.broadcast({"type": "voices"})
    return ok(voice=voice)


async def api_voice_calibrate(request):
    """Подстраивает тон голоса под тон записи-эталона.

    XTTS переносит тембр, но не абсолютную высоту: клон почти всегда звучит
    выше или ниже оригинала. Здесь мы синтезируем короткую пробу, измеряем её
    основной тон и вычисляем сдвиг, который вернёт голос к высоте эталона.
    """
    a = app_state
    data = await body(request)
    voice = a.voices.get(str(data.get("voice", "")))
    if not voice or voice["kind"] != "custom":
        return fail("Подстройка доступна только для своих голосов.")
    ref = float(voice.get("ref_f0") or 0)
    if ref <= 0:
        return fail("Нет данных эталона. Переимпортируйте голос с записями.")
    if a.engine.status != "ready":
        return fail("Модель озвучки ещё не готова", 503)
    probe = "Привет, это проверка тона голоса."
    path = CACHE / ("cal_%s.wav" % uuid.uuid4().hex)
    try:
        segs = segments(probe, a.settings["language"])
        latents = a.voices.latents(voice["id"])
        await a.loop.run_in_executor(a.executor, a.engine.synthesize, segs, latents, path,
                                     a.settings["speed"], 0.0)
        measured = measure_f0(path)
    except Exception as e:
        log.exception("ошибка подстройки тона")
        return fail("Не удалось подобрать тон: %s" % e, 500)
    finally:
        try:
            path.unlink()
        except OSError:
            pass
    if measured <= 0:
        return fail("Не удалось измерить тон пробной записи.")
    steps = 12.0 * math.log2(ref / measured)
    steps = max(-24.0, min(steps, 24.0))
    try:
        updated = a.voices.update_pitch(voice["id"], steps)
    except VoiceError as e:
        return fail(str(e))
    a.broadcast({"type": "voices"})
    return ok(voice=updated, ref_f0=round(ref, 1), measured=round(measured, 1),
              pitch=round(steps, 1),
              note="Тон голоса подстроен под запись образца: сдвиг %+.1f полутона."
                   % steps)


async def api_voice_delete(request):
    a = app_state
    try:
        a.voices.delete(request.path_params["voice_id"])
    except VoiceError as e:
        return fail(str(e))
    a.broadcast({"type": "voices"})
    return ok()


async def api_voice_toggle(request):
    """Включить/выключить голос."""
    a = app_state
    data = await body(request)
    target, enabled = str(data.get("voice", "")), bool(data.get("enabled"))
    ids = [v["id"] for v in a.voices.list() if v["id"] == target]
    disabled = set(a.settings["disabled_voices"])
    for i in ids:
        (disabled.discard if enabled else disabled.add)(i)
    a.settings["disabled_voices"] = sorted(disabled)
    a.save_settings()
    a.broadcast({"type": "voices"})
    return ok()


async def api_cosyvoice_setup(request):
    """Указать папку CosyVoice 3 и запустить её."""
    a = app_state
    data = await body(request)
    path = str(data.get("path", "")).strip().strip('"') or cvlib.find_cosyvoice(ROOT)
    if not path:
        return fail("Укажите папку с CosyVoice 3 (в ней должны быть env и pretrained_models).")
    try:
        cvlib.check_cosyvoice(path)
    except cvlib.CosyVoiceError as e:
        return fail(str(e))
    try:
        a.cosyvoice.start(path)
    except cvlib.CosyVoiceError as e:
        return fail(str(e))
    except Exception as e:
        log.exception("ошибка запуска CosyVoice 3")
        return fail("Не удалось запустить CosyVoice 3: %s" % e, 500)
    a.settings["cosyvoice_path"] = a.cosyvoice.path
    a.save_settings()
    a.push_status()
    return ok(cosyvoice=a.cosyvoice.info())


async def api_voice_engine(request):
    """Переключить движок озвучки своего голоса: XTTS или CosyVoice 3."""
    a = app_state
    data = await body(request)
    try:
        voice = a.voices.set_engine(str(data.get("voice", "")), str(data.get("engine", "xtts")))
    except (VoiceError, ValueError, TypeError) as e:
        return fail(str(e))
    a.broadcast({"type": "voices"})
    return ok(voice=voice)


async def api_voice_cosyvoice(request):
    """Подготовить образец CosyVoice 3 для голоса и расшифровать его."""
    a = app_state
    data = await body(request)
    voice_id = str(data.get("voice", ""))
    try:
        path, text = a.voices.prepare_cosyvoice(voice_id)
    except (VoiceError, OSError) as e:
        return fail(str(e))
    if not text:
        try:
            a.ensure_cosyvoice()
            text = a.cosyvoice.transcribe(path)
        except Exception as e:
            log.warning("не удалось расшифровать образец: %s", e)
            text = ""
        if text:
            try:
                a.voices.set_transcript(voice_id, text)
            except VoiceError:
                pass
    a.broadcast({"type": "voices"})
    return ok(voice=a.voices.get(voice_id), transcript=text)


async def api_voice_transcript(request):
    """Сохранить текст образца CosyVoice 3."""
    a = app_state
    data = await body(request)
    try:
        voice = a.voices.set_transcript(str(data.get("voice", "")), data.get("text", ""))
    except (VoiceError, ValueError, TypeError) as e:
        return fail(str(e))
    a.broadcast({"type": "voices"})
    return ok(voice=voice)


async def api_rvc_setup(request):
    """Указать папку Applio и запустить RVC."""
    a = app_state
    data = await body(request)
    path = str(data.get("path", "")).strip().strip('"') or rvclib.find_applio(ROOT)
    if not path:
        return fail("Укажите папку, в которую установлен Applio, например D:\\Applio")
    try:
        a.rvc.start(path)
    except rvclib.RvcError as e:
        return fail(str(e))
    except Exception as e:
        log.exception("ошибка запуска RVC")
        return fail("Не удалось запустить Applio: %s" % e, 500)
    a.settings["applio_path"] = a.rvc.path
    a.save_settings()
    a.push_status()
    return ok(rvc=a.rvc.info())


def _set_progress(text):
    a = app_state
    a.progress = text
    a.loop.call_soon_threadsafe(a.broadcast, {"type": "progress", "text": text})


async def api_rvc_train(request):
    """Обучение RVC-модели из загруженных записей."""
    a = app_state
    form = await request.form(max_files=40, max_part_size=MAX_UPLOAD)
    name = str(form.get("name", ""))
    aliases = [x.strip() for x in str(form.get("aliases", "")).replace(";", ",").split(",") if x.strip()]
    try:
        epochs = max(10, min(2000, int(str(form.get("epochs", "200")) or 200)))
        batch = max(1, min(32, int(str(form.get("batch", "4")) or 4)))
        sample_rate = int(str(form.get("sample_rate", "40000")) or 40000)
        pitch = int(str(form.get("pitch", "0")) or 0)
    except ValueError:
        return fail("Неверные параметры обучения.")
    base = str(form.get("base", ""))
    work = TMP / ("train_%s" % uuid.uuid4().hex)
    work.mkdir(parents=True, exist_ok=True)
    files, total = [], 0
    for n, f in enumerate(form.getlist("files")):
        if not hasattr(f, "read"):
            continue
        ext = os.path.splitext(f.filename or "")[1][:8]
        ext = "".join(c for c in ext if c.isalnum() or c == ".") or ".wav"
        dst = work / ("src_%02d%s" % (n, ext))
        with open(dst, "wb") as out:
            while True:
                chunk = await f.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_UPLOAD:
                    shutil.rmtree(work, ignore_errors=True)
                    return fail("Файлы слишком большие (больше 500 МБ).")
                out.write(chunk)
        files.append(dst)
    if not files:
        shutil.rmtree(work, ignore_errors=True)
        return fail("Добавьте записи голоса.")
    a.trainer.opts = {"base": base, "pitch": pitch, "epochs": epochs,
                      "batch": batch, "sample_rate": sample_rate}
    a._train_done = False
    try:
        status = a.trainer.start(name, aliases, files, a.trainer.opts)
    except TrainError as e:
        shutil.rmtree(work, ignore_errors=True)
        return fail(str(e))
    return ok(train=status)


async def api_rvc_train_status(request):
    """Текущее состояние обучения."""
    return ok(train=app_state.trainer.status())


async def api_rvc_train_cancel(request):
    """Остановить обучение."""
    a = app_state
    return ok(canceled=a.trainer.cancel())


async def api_rvc_update(request):
    a = app_state
    data = await body(request)
    try:
        voice = a.voices.update_rvc(str(data.get("voice", "")), data.get("base"), data.get("pitch"),
                                    data.get("index_rate"))
    except (VoiceError, ValueError, TypeError) as e:
        return fail(str(e))
    a.broadcast({"type": "voices"})
    return ok(voice=voice)


async def audio(request):
    name = request.path_params["name"]
    path = CACHE / name
    if "/" in name or "\\" in name or not name.endswith(".wav") or not path.exists():
        return fail("нет файла", 404)
    return FileResponse(path, media_type="audio/wav", headers={"Cache-Control": "no-store"})


async def api_debug_chat(request):
    """Только для проверки с заглушкой: подставить сообщение, как будто оно пришло из чата."""
    d = await body(request)
    app_state.on_chat({"id": uuid.uuid4().hex, "login": d.get("user", "test").lower(), "name": d.get("user", "test"),
                       "color": d.get("color", ""), "text": d.get("text", ""), "emotes": [],
                       "is_mod": bool(d.get("mod")), "is_broadcaster": False, "is_reward": bool(d.get("reward")),
                       "is_sub": False, "is_vip": False})
    return ok()


routes = [
    Route("/", page_root),
    Route("/chat", page_index),
    Route("/voices", page_voices),
    Route("/overlay", page_overlay),
    Route("/poll", page_poll),
    Route("/player", page_player),
    Route("/brb", page_brb),
    Route("/events", events),
    Route("/api/state", api_state),
    Route("/api/settings", api_settings, methods=["POST"]),
    Route("/api/connect", api_connect, methods=["POST"]),
    Route("/api/disconnect", api_disconnect, methods=["POST"]),
    Route("/api/control/{action}", api_control, methods=["POST"]),
    Route("/api/say", api_say, methods=["POST"]),
    Route("/api/preview", api_preview, methods=["POST"]),
    Route("/api/announce", api_announce, methods=["POST"]),
    Route("/api/brb", api_brb, methods=["GET", "POST"]),
    Route("/api/emotes", api_emotes, methods=["GET"]),
    Route("/api/emotes/reset", api_emotes_reset, methods=["POST"]),
    Route("/api/voices", api_voices, methods=["GET"]),
    Route("/api/voices", api_voice_create, methods=["POST"]),
    Route("/api/voices/toggle", api_voice_toggle, methods=["POST"]),
    Route("/api/rvc/setup", api_rvc_setup, methods=["POST"]),
    Route("/api/cosyvoice/setup", api_cosyvoice_setup, methods=["POST"]),
    Route("/api/voice/engine", api_voice_engine, methods=["POST"]),
    Route("/api/voice/cosyvoice", api_voice_cosyvoice, methods=["POST"]),
    Route("/api/voice/transcript", api_voice_transcript, methods=["POST"]),
    Route("/api/rvc/update", api_rvc_update, methods=["POST"]),
    Route("/api/rvc/train", api_rvc_train, methods=["POST"]),
    Route("/api/rvc/train/status", api_rvc_train_status, methods=["GET"]),
    Route("/api/rvc/train/cancel", api_rvc_train_cancel, methods=["POST"]),
    Route("/api/voices/{voice_id:path}", api_voice_delete, methods=["DELETE"]),
    Route("/api/voice/pitch", api_voice_pitch, methods=["POST"]),
    Route("/api/voice/volume", api_voice_volume, methods=["POST"]),
    Route("/api/voice/calibrate", api_voice_calibrate, methods=["POST"]),
    Route("/audio/{name}", audio),
    # тестовое подключение сообщения из чата - включается переменной окружения
    *([Route("/api/debug/chat", api_debug_chat, methods=["POST"])] if os.environ.get("TTS_DEBUG_CHAT") == "1" else []),
    Mount("/static", StaticFiles(directory=WEB), name="static"),
]

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
