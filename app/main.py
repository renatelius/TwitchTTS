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
from settings import Settings, load_json, save_json  # noqa: E402
from engine import OUT_SR, REF_SR, MockEngine, XttsEngine  # noqa: E402
import rvc as rvclib  # noqa: E402
import cosyvoice as cvlib  # noqa: E402
from textproc import clean_text  # noqa: E402
import features as featlib  # noqa: E402
import chat_rules as chatrules  # noqa: E402
import api as apilib  # noqa: E402
import speak_queue as queuelib  # noqa: E402
import synth_lane as synthlane  # noqa: E402
import synthesis as synthmod  # noqa: E402
from chat_rules import login_allowed, split_prefix, split_voice_prefix, voice_blocked  # noqa: E402
import emotes as emotelib  # noqa: E402
from twitch import TwitchChat  # noqa: E402
import twitch_api as tapilib  # noqa: E402
from voices import VoiceStore, decode as decode_audio  # noqa: E402
from rvc_train import RvcTrainer  # noqa: E402

HOST = os.environ.get("TTS_HOST", "127.0.0.1")
PORT = int(os.environ.get("TTS_PORT", "8765"))

log = logging.getLogger("main")

def _same_path(a, b):
    """Одинаковые ли пути (с учётом регистра, ссылок и ..)."""
    try:
        return os.path.normcase(os.path.realpath(str(a))) == os.path.normcase(os.path.realpath(str(b)))
    except Exception:
        return str(a) == str(b)


def _vol_factor(value):
    """Громкость голоса (проценты) -> коэффициент 0.0-2.0."""
    try:
        return max(0.0, min(float(value), 200.0)) / 100.0
    except (TypeError, ValueError):
        return 1.0



class App:
    def __init__(self):
        self.settings = Settings(DATA / "settings.json")
        self.user_voices = load_json(DATA / "user_voices.json", {})
        self.mock = os.environ.get("TTS_MOCK") == "1"
        if self.mock:
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
        # один запуск CosyVoice: ранний старт и запуск по требованию не должны
        # поднимать второй воркер поверх первого
        self._cosy_lock = threading.Lock()
        # записи эмоций для движков без собственных тегов (XTTS, RVC): sounds/<эмоция>/
        self.emotes = emotelib.EmoteSounds(ROOT / "sounds", OUT_SR,
                                           lambda p: (decode_audio(p), REF_SR))
        self.emotes.prepare_folders()
        # Синтез всегда один за раз (полоса), но ждут разные очереди:
        # реплики чата и предпрослушивание голосов не блокируют друг друга.
        self.lane = synthlane.SynthLane()
        self.executor = ThreadPoolExecutor(max_workers=1)        # очередь реплик чата
        self.preview_executor = ThreadPoolExecutor(max_workers=1)  # предпрослушивание голосов
        # синтез живёт в отдельном модуле: app/synthesis.py
        self.synth = synthmod.Synthesizer(engine=self.engine, voices=self.voices,
                                          settings=self.settings, emotes=self.emotes,
                                          rvc=self.rvc, cosyvoice=self.cosyvoice,
                                          ensure_cosyvoice=self.ensure_cosyvoice,
                                          lane=self.lane)
        self._train_done = False
        self.progress = ""
        self.clients = set()
        self.history = deque(maxlen=150)
        self.queue = queuelib.SpeakQueue(synthesize=self._synthesize_job,
                                        broadcast=self.broadcast,
                                        is_ready=lambda: self.engine.status == "ready",
                                        on_state=self.push_status,
                                        on_finished=self.cleanup_cache)
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
        q = self.queue.state()
        return {
            "type": "status",
            "engine": self.engine.info(),
            "rvc": self.rvc.info(),
            "cosyvoice": self.cosyvoice.info(),
            "train": self.trainer.status(),
            "twitch": {"status": self.twitch.status, "detail": self.twitch.detail, "channel": self.twitch.channel},
            "queue": q["queue"],
            "paused": q["paused"],
            "now": q["now"],
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

    def start_cosyvoice(self, path=None):
        """Запускает CosyVoice 3, если папка с моделью известна (или находится сама).

        Возвращает True, если воркер запущен или уже работает.
        """
        with self._cosy_lock:
            if self.cosyvoice.status in ("ready", "starting"):
                if not path or _same_path(self.cosyvoice.path, path):
                    return True
                # указана другая папка - поднимаем воркер заново на ней
            path = path or self.detect_cosyvoice()
            if not path:
                return False
            return self._launch_cosyvoice(path)

    def _launch_cosyvoice(self, path):
        """Запуск воркера CosyVoice. Вызывать под self._cosy_lock."""
        try:
            self.cosyvoice.start(path)
            return True
        except Exception as e:
            log.warning("CosyVoice 3 не запущен: %s", e)
            self.cosyvoice.status, self.cosyvoice.error = "error", str(e)
            return False

    def warmup_cosyvoice(self):
        """Грузит CosyVoice 3 заранее, чтобы первая реплика не ждала модель.

        Модель поднимается в фоне: запуск программы не задерживается.
        """
        if self.mock or not self.settings.get("cosyvoice_warmup", True):
            return
        path = self.detect_cosyvoice()
        if not path:
            return
        if self.cosyvoice.status in ("ready", "starting"):
            return
        log.info("ранний старт CosyVoice 3: %s", path)
        self.start_cosyvoice(path)

    def ensure_cosyvoice(self):
        """Гарантирует, что воркер CosyVoice 3 запущен."""
        if self.cosyvoice.status in ("ready", "starting"):
            return
        with self._cosy_lock:
            if self.cosyvoice.status in ("ready", "starting"):
                return
            path = self.settings["cosyvoice_path"] or cvlib.find_cosyvoice(ROOT)
            if not path:
                raise cvlib.CosyVoiceError("Не найдена папка CosyVoice 3. Укажите её на вкладке «Голоса».")
            try:
                self.cosyvoice.start(path)
            except Exception as e:
                raise cvlib.CosyVoiceError("Не удалось запустить CosyVoice 3: %s" % e)

    def save_settings(self):
        self.settings.save()

    # ---------- голоса ----------
    def default_voice(self):
        enabled = self.voices.enabled()
        for v in enabled:
            if v["id"] == self.settings["default_voice"]:
                return v
        return enabled[0] if enabled else None

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
                                queue_len=self.queue.pending_len(),
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

    def enqueue(self, msg_id, user, speak_text, voice, style=""):
        self.queue.enqueue({"id": msg_id, "user": user, "text": speak_text, "voice_id": voice["id"],
                            "voice_name": voice["name"], "style": style,
                            "vol": _vol_factor(voice.get("volume"))})

    # ---------- CosyVoice 3 с записями эмоций ----------
    async def _synthesize_job(self, job):
        """Синтез реплики в отдельном потоке: модель одна, работаем по очереди."""
        return await self.loop.run_in_executor(self.executor, self.synth.synthesize, job)

    async def _preview_job(self, job):
        """Синтез для предпрослушивания: отдельный поток, приоритет ниже чата.

        Отдельный поток нужен, чтобы предпрослушивание не занимало место в
        очереди реплик: иначе прослушивание голосов глушит озвучку чата.
        """
        return await self.loop.run_in_executor(self.preview_executor, self.synth.synthesize, job)


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
        self.queue.skip()

    def clear(self):
        self.queue.clear()

    def pause(self):
        self.queue.pause()

    def resume(self):
        self.queue.resume()

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
        self.queue.wake_up()

    async def startup(self):
        self.loop = asyncio.get_running_loop()
        self.queue.start(self.loop)
        shutil.rmtree(TMP, ignore_errors=True)
        TMP.mkdir(exist_ok=True)
        threading.Thread(target=self.engine.load, daemon=True).start()
        self.start_rvc()
        self.detect_cosyvoice()
        # CosyVoice 3 поднимается заранее в фоне: первая реплика не ждёт загрузки модели
        threading.Thread(target=self.warmup_cosyvoice, daemon=True).start()
        self.tasks = [asyncio.create_task(self.queue.worker()),
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
