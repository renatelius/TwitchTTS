# -*- coding: utf-8 -*-
"""Проверка Twitch API: награды, подписчики, таймауты."""
import os
import sys

os.environ["TTS_MOCK"] = "1"
APP = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app")
sys.path.insert(0, APP)

import twitch_api as tapilib   # noqa: E402
import chat_rules as chatrules  # noqa: E402
import main as appmod          # noqa: E402

FAILED = []


def check(name, got, want):
    ok = got == want
    if not ok:
        FAILED.append(name)
    print("%-58s %s | %s" % (name, "OK" if ok else "FAIL",
                             got if ok else "got=%r want=%r" % (got, want)))


# --- сам API ---
api = tapilib.TwitchApi()
check("API: без ключей не готов", api.ready, False)
try:
    api.user_id("test")
    check("API: без ключей бросает ошибку", False, True)
except tapilib.TwitchApiError:
    check("API: без ключей бросает ошибку", True, True)

calls = []


def fake_request(self, method, path, params=None, body=None):
    calls.append((method, path, params, body))
    if path == "/users":
        return {"data": [{"id": "42", "login": (params or {}).get("login", "nobody")}]}
    if path == "/channel_points/custom_rewards":
        return {"data": [{"id": "r1", "title": "выделить сообщение"}]}
    if path == "/channels/followers":
        return {"data": [{"user_id": "7", "user_login": "newbie"},
                         {"user_id": "8", "user_login": "old"}]}
    return {}


api = tapilib.TwitchApi("cid", "token")
tapilib.TwitchApi.request = fake_request
check("API: id пользователя", api.user_id("test"), "42")
check("API: список наград", api.rewards("42")[0]["title"], "выделить сообщение")
check("API: подписчики", len(api.followers("42")), 2)
api.timeout("42", "7", 300)
check("API: таймаут - метод", calls[-1][0], "POST")
check("API: таймаут - путь", calls[-1][1], "/moderation/bans")
check("API: таймаут - тело", calls[-1][3]["data"]["duration"], 300)

# --- приложение ---
app = appmod.App()
app.save_settings = lambda: None
events = []
app.broadcast = lambda ev: events.append(ev)

check("API: приложение не готово без токена", app.api_ready(), False)
done, text = app._do_timeout("nobody", 600)
check("API: таймаут без токена не выполняется", done, False)
check("API: таймаут без токена объясняет", "токен" in text, True)

app.settings["twitch_client_id"] = "cid"
app.settings["twitch_token"] = "token"
check("API: приложение готово с токеном", app.api_ready(), True)

app.api_sync()
check("API: награда найдена по id", app.reward_title("r1"), "выделить сообщение")
check("API: неизвестная награда", app.reward_title("r2"), "")

# награда по названию выделяется
app.settings["highlight_reward"] = "выделить сообщение"
app._rewards = {"r1": "выделить сообщение"}
msg = {"id": "1", "login": "v", "name": "Vasya", "color": "#fff", "text": "привет",
       "emotes": [], "is_mod": False, "is_broadcaster": False, "is_reward": True,
       "reward_id": "r1", "is_sub": False, "is_vip": False, "msg_id": "", "system_msg": ""}
app.on_chat(msg)
chat = [ev for ev in events if ev.get("type") == "chat"]
check("награда: сообщение выделено", bool(chat) and chat[-1].get("highlight"), True)
check("награда: в сообщении видно название", bool(chat) and chat[-1].get("reward"), "выделить сообщение")

# новые подписчики
app._followers_ready = False
check("фоллы: первый обход молчит", app._fetch_new_followers(), [])
app._followers = {"8"}
app._followers_ready = True
check("фоллы: новый подписчик найден", app._fetch_new_followers(), ["newbie"])

# команда таймаута
events.clear()
mod = {"id": "2", "login": "mod", "name": "Mod", "color": "#fff", "text": "!таймаут vasya 300",
       "emotes": [], "is_mod": True, "is_broadcaster": False, "is_reward": False,
       "is_sub": False, "is_vip": False, "msg_id": "", "system_msg": ""}


def decide(message):
    ctx = chatrules.Context(settings=app.settings)
    return chatrules.decide(message, ctx)


d = decide(mod)
check("команда: !таймаут распознана", (d.kind, d.command), (chatrules.COMMAND, "timeout"))
check("команда: аргументы таймаута", d.args, "vasya 300")
app.run_command(d, mod)          # ответ уходит в Twitch API - падать не должно

plain = dict(mod, is_mod=False)
d = decide(plain)
check("команда: !таймаут не для простого зрителя", d.kind == chatrules.COMMAND, False)

print("")
print("Провалено: %d" % len(FAILED))
if FAILED:
    for name in FAILED:
        print("  - " + name)
    sys.exit(1)
