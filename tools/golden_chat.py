# -*- coding: utf-8 -*-
"""Золотой слепок поведения чата.

Прогоняет текущий код по набору сообщений и записывает, что он решил.
Используется как страховка при разбиении app/main.py: слепок снят ДО
рефакторинга сравнивается со слепком ПОСЛЕ - расхождение означает регресс.

Запуск: python tools/golden_chat.py --out <файл.json>
"""
import argparse
import json
import os
import sys
import time
from collections import deque
from pathlib import Path

os.environ["TTS_MOCK"] = "1"
APP = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app")
sys.path.insert(0, APP)

import main as appmod   # noqa: E402


class FakeVoices:
    """Голоса-заглушки: результат не должен зависеть от папки voices."""

    VOICES = [
        {"id": "v1", "name": "Дудя", "enabled": True, "kind": "custom", "volume": 100, "pitch": 0},
        {"id": "v2", "name": "Женя", "enabled": True, "kind": "custom", "volume": 100, "pitch": 0},
    ]

    def enabled(self):
        return list(self.VOICES)

    def resolve(self, name):
        n = str(name or "").strip().lower()
        for v in self.VOICES:
            if v["name"].lower() == n or (n and n in v["name"].lower()):
                return v
        return None

    def get(self, voice_id):
        for v in self.VOICES:
            if v["id"] == voice_id:
                return v
        return None


def msg(text, user="zviewer", mod=False, sub=False, vip=False, reward=False,
        reward_id="", msg_id="", system_msg=""):
    return {
        "id": "m%d" % (sum(map(ord, text + user)) % 100000),
        "login": user.lower(),
        "name": user,
        "color": "#ff0000",
        "text": text,
        "emotes": [],
        "is_mod": mod,
        "is_broadcaster": False,
        "is_reward": reward,
        "reward_id": reward_id,
        "is_sub": sub,
        "is_vip": vip,
        "msg_id": msg_id,
        "system_msg": system_msg,
    }


# каждый случай: имя, настройки поверх базовых, сообщение
CASES = [
    ("обычное сообщение", {}, msg("привет чат")),
    ("пустое сообщение", {}, msg("    ")),
    ("только эмоция", {}, msg("(смех)")),
    ("префикс без настройки", {}, msg("привет")),
    ("стиль в ремарке", {}, msg("(радостно) привет")),
    ("стиль шёпотом", {}, msg("(шёпотом) тише")),
    ("голос на сообщение", {}, msg("!!!#дудя привет")),
    ("случайный голос", {}, msg("!!!#random привет")),
    ("действие /me", {}, msg("/me машет рукой")),
    ("раскладка", {"fix_layout": True}, msg("Ghbdtn vbh")),
    ("ударение", {}, msg("т+орты")),
    ("префикс без сообщения", {"tts_prefix": "!!!"}, msg("привет")),
    ("префикс с сообщением", {"tts_prefix": "!!!"}, msg("!!! привет")),
    ("голос командой", {}, msg("!г дудя")),
    ("голос сбросом", {}, msg("!г default")),
    ("голос не найден", {}, msg("!г кривой")),
    ("голос запрещён", {"blocked_voices": ["Дудя"]}, msg("!г дудя")),
    ("пропуск модератором", {}, msg("!скип", user="mod", mod=True)),
    ("пропуск не модератором", {}, msg("!скип")),
    ("курс валют", {}, msg("!курс 100 usd")),
    ("перевод", {}, msg("!перевод hello world")),
    ("выделить модератором", {}, msg("!выделить важно", user="mod", mod=True)),
    ("выделить не модератором", {}, msg("!выделить важно")),
    ("закрепить модератором", {}, msg("!закрепить наверх", user="mod", mod=True)),
    ("закрепить не модератором", {}, msg("!закрепить наверх")),
    ("BRB модератором", {}, msg("!брб 5", user="mod", mod=True)),
    ("BRB не модератором", {}, msg("!брб 5")),
    ("таймаут модератором", {}, msg("!таймаут vasya 300", user="mod", mod=True)),
    ("таймаут не модератором", {}, msg("!таймаут vasya 300")),
    ("бот в стоп-листе", {}, msg("привет", user="Nightbot")),
    ("зритель в запрет-списке", {"tts_deny": ["zviewer"]}, msg("привет")),
    ("только для своих", {"tts_allow": ["друг"]}, msg("привет")),
    ("цифра", {}, msg("123")),
    ("запрещённое слово", {"blocked_words": ["плохое"]}, msg("это плохое слово")),
    ("только подписчикам", {"audience": "subs"}, msg("привет")),
    ("подписчику можно", {"audience": "subs"}, msg("привет", user="sub", sub=True)),
    ("многие раз подряд", {"cooldown": 60}, msg("привет")),
    ("очередь полна", {"max_queue": 2}, msg("привет")),
    ("озвучка выключена", {"muted": True}, msg("привет")),
    ("награда при выключенном пропуске", {"skip_rewards": False}, msg("сообщение с наградой", reward=True, reward_id="r1")),
    ("награда при пропуске", {"skip_rewards": True}, msg("сообщение с наградой", reward=True, reward_id="r1")),
    ("награда выделяется", {"skip_rewards": True, "highlight_rewards": True},
     msg("сообщение с наградой", reward=True, reward_id="r1")),
    ("рейд по тегу", {}, msg("Bob is now raiding the channel.", msg_id="raid")),
    ("рейд по тексту", {}, msg("что-то", system_msg="Bob is now raiding the channel.")),
    ("подписка по тегу", {}, msg("Bob just subscribed!", msg_id="midnightblue")),
    ("битсы по тегу", {}, msg("Bob cheered 100 bits", msg_id="bitsbadgetier")),
    ("обычный текст без события", {}, msg("просто текст", system_msg="")),
]


def build_app(base_settings):
    app = appmod.App()
    app.voices = FakeVoices()
    app.user_voices = {}
    app.last_spoke = {}
    app.pending = deque()
    app.wake = __import__("threading").Event()
    app.skip_event = __import__("threading").Event()
    app.save_settings = lambda: None          # настройки не трогаем
    appmod.save_json = lambda path, data: None  # user_voices.json не трогаем
    app.settings = dict(base_settings)
    return app


def snapshot(app, message):
    events = []
    queued = []
    app.broadcast = lambda ev: events.append(ev)
    app.enqueue = lambda msg_id, user, text, voice, style="": queued.append(
        {"user": user, "text": text, "voice": voice.get("name", ""), "style": style})
    app.on_chat(message)
    out = []
    for ev in events:
        item = {"type": ev.get("type")}
        for key in ("text", "note", "tts", "voice", "highlight", "reward", "user", "event", "message"):
            if key in ev and ev[key] not in ("", None, False):
                item[key] = ev[key]
        out.append(item)
    if "!!!#random" in message["text"]:
        # случайный голос выбирается по времени - для слепка это не годится
        for item in out:
            if "voice" in item:
                item["voice"] = "<random>"
        for item in queued:
            item["voice"] = "<random>"
    return {"events": out, "queued": queued, "user_voices": dict(app.user_voices)}


def build_snapshot():
    """Слепок решений по всем случаям. Порядок и содержимое должны быть стабильны."""
    base = dict(appmod.DEFAULTS)
    base["channel"] = "test"
    result = []
    for name, override, message in CASES:
        settings = dict(base)
        settings.update(override)
        app = build_app(settings)
        if name == "многие раз подряд":
            app.last_spoke[message["login"]] = time.time()
        result.append({"case": name, "result": snapshot(app, message)})
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    result = build_snapshot()
    payload = json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True)
    Path(args.out).write_text(payload, encoding="utf-8")
    print("случаев: %d, файл: %s" % (len(result), args.out))


if __name__ == "__main__":
    main()
