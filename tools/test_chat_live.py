# -*- coding: utf-8 -*-
"""Сквозная проверка чата через HTTP: сообщения и команды.

Нужен сервер, запущенный с заглушкой и отладочным входом:

    set TTS_PORT=8769 & set TTS_MOCK=1 & set TTS_DEBUG_CHAT=1
    python app\main.py --no-browser
    python tools\test_chat_live.py

Проверяет то, чего не видно в юнит-тестах: что команда из чата доходит до
обработчика без исключения и что бот отвечает в чат.
"""
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request

BASE = os.environ.get("TTS_BASE", "http://127.0.0.1:8769")

FAILED = []
EVENTS = []


def check(name, ok, detail=""):
    if not ok:
        FAILED.append(name)
    print("%-58s %s%s" % (name, "OK" if ok else "FAIL", (" | " + str(detail)) if detail else ""))


def reader():
    try:
        resp = urllib.request.urlopen(BASE + "/events", timeout=120)
        for raw in resp:
            line = raw.decode("utf-8", "replace").strip()
            if line.startswith("data: "):
                EVENTS.append(json.loads(line[6:]))
    except Exception:
        pass


def post(text, user="test", mod=False):
    payload = json.dumps({"user": user, "text": text, "mod": mod}).encode("utf-8")
    req = urllib.request.Request(BASE + "/api/debug/chat", data=payload,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, {"error": e.read().decode("utf-8", "replace")[:200]}


def wait_event(pred, before=0, timeout=20):
    """Ищет событие только среди новых (после индекса before)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        for ev in list(EVENTS[before:]):
            if pred(ev):
                return ev
        time.sleep(0.1)
    return None


threading.Thread(target=reader, daemon=True).start()
time.sleep(1.0)

# 1) обычное сообщение: должно уйти в озвучку
before = len(EVENTS)
status, res = post("привет всем")
check("обычное сообщение принято без ошибки", status == 200 and res.get("ok") is True,
      res.get("error", status))
ev = wait_event(lambda e: e.get("type") == "chat" and e.get("text") == "привет всем", before)
check("обычное сообщение дошло до чата", ev is not None)
check("обычное сообщение помечено для озвучки", bool(ev and ev.get("tts")), ev)

# 2) команда выбора голоса: раньше здесь падало 500 из-за забытого импорта
before = len(EVENTS)
status, res = post("!г %s" % (ev or {}).get("voice", ""))
check("команда !г отработала без ошибки", status == 200 and res.get("ok") is True,
      res.get("error", status))
check("команда !г дала ответ в чат",
      wait_event(lambda e: e.get("type") == "note", before) is not None)

# 3) остальные команды
def is_answer(ev, cmd):
    """Ответ бота: событие note, реплика в чате или пометка в поле note.

    Часть команд (выделить, закрепить, брб) подтверждается прямо в эхе
    самой команды - в поле note стоит "команда".
    """
    if ev.get("type") == "note":
        return True
    if ev.get("type") != "chat":
        return False
    if ev.get("text") != cmd:
        return True
    return bool(ev.get("note"))


for cmd, word in (("!курс 100 usd", "USD"), ("!перевод hello world", "мир"),
                   ("!выделить", ""), ("!закрепить", ""), ("!брб 5", "")):
    before = len(EVENTS)
    status, res = post(cmd)
    check("команда %s отработала" % cmd.split()[0], status == 200 and res.get("ok") is True,
          res.get("error", status))
    got = wait_event(lambda e: is_answer(e, cmd), before)
    check("команда %s дала ответ" % cmd.split()[0], got is not None)
    if word and got:
        text = str(got.get("text") or "")
        check("ответ %s содержит %s" % (cmd.split()[0], word), word.lower() in text.lower(), text)

print("")
print("Провалено: %d" % len(FAILED))
sys.exit(1 if FAILED else 0)
