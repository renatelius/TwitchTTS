# -*- coding: utf-8 -*-
"""Проверка новых фич: события чата, курс валют, перевод, BRB-экран, плеер для OBS."""
import os
import sys
import threading

os.environ["TTS_MOCK"] = "1"
APP = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app")
sys.path.insert(0, APP)

import features as featlib     # noqa: E402
import chat_rules as chatrules  # noqa: E402
import main as appmod           # noqa: E402

FAILED = []


def check(name, got, want):
    ok = got == want
    if not ok:
        FAILED.append(name)
    print("%-58s %s | %s" % (name, "OK" if ok else "FAIL",
                             got if ok else "got=%r want=%r" % (got, want)))


# --- события чата ---
check("событие: рейд по msg-id", featlib.detect_event("raid", ""), "raid")
check("событие: подписка по msg-id", featlib.detect_event("midnightblue", ""), "sub")
check("событие: рейд по тексту", featlib.detect_event("", "Bob is now raiding the channel."), "raid")
check("событие: подписка по тексту", featlib.detect_event("", "Bob just subscribed!"), "sub")
check("событие: битсы по тексту", featlib.detect_event("", "Bob cheered 100 Bits"), "bits")
check("событие: обычное сообщение", featlib.detect_event("", "привет"), "")
check("событие: html убирается", featlib.clean_system("<strong>Bob</strong> subscribed"), "Bob subscribed")
check("событие: текст без system-msg", featlib.event_text("raid", "Bob", ""), "Рейд от Bob")

# --- курс валют ---
check("курс: разбор '100 usd'", featlib.parse_rate("100 usd"), (100.0, "usd", "rub"))
check("курс: разбор '100 usd в eur'", featlib.parse_rate("100 usd в eur"), (100.0, "usd", "eur"))
check("курс: разбор '100,5 usd'", featlib.parse_rate("100,5 usd"), (100.5, "usd", "rub"))
check("курс: мусор не ломает", featlib.parse_rate("погода"), (None, "", "rub"))
check("курс: формат числа", featlib.fmt_num(9500.5), "9 500,50")
check("курс: формат миллионов", featlib.fmt_num(1234567.891), "1 234 567,89")

_saved_rates = featlib.rates
featlib.rates = lambda timeout=8: {"USD": 1.0, "EUR": 0.9, "RUB": 80.0}
check("курс: 100 usd в rub", featlib.convert(100, "USD", "RUB"), 8000.0)
check("курс: 100 usd в eur", round(featlib.convert(100, "USD", "EUR"), 4), 90.0)
check("курс: неизвестная валюта", featlib.convert(100, "USD", "XYZ"), None)
check("курс: одинаковые валюты", featlib.convert(100, "USD", "USD"), 100.0)
featlib.rates = _saved_rates

# --- язык перевода ---
check("перевод: код языка в конце", featlib.split_language("hello en"), ("hello", "en"))
check("перевод: неизвестный хвост - часть текста", featlib.split_language("hello world"), ("hello world", "ru"))
check("перевод: без языка", featlib.split_language("привет"), ("привет", "ru"))
check("перевод: немецкий", featlib.split_language("привет de"), ("привет", "de"))

# --- заглушка: слепок от настоящего XTTS ---
import tempfile
from engine import MockEngine
_tmp = os.path.join(tempfile.mkdtemp(), "latents.pth")
with open(_tmp, "wb") as _f:
    _f.write(b"\x80\x03\x00binary")
check("заглушка: бинарный слепок читается", MockEngine().load_latents(_tmp)["freq"] > 0, True)

# --- перевод ---
_saved_get = featlib._get_json
featlib._get_json = lambda url, timeout=8: [[["привет", "hello", None, None, 10]], None, "en"]
check("перевод: склейка кусков", featlib.translate("hello"), "привет")
featlib._get_json = _saved_get

# --- BRB ---
check("BRB: '10' -> 10 минут", featlib.parse_brb("10"), (600, ""))
check("BRB: '90с' -> 90 секунд", featlib.parse_brb("90с"), (90, ""))
check("BRB: '5 минут попить'", featlib.parse_brb("5 минут попить"), (300, "попить"))

# --- приложение ---
for key in ("player_only", "highlight_rewards", "event_rules", "brb_seconds", "brb_message"):
    check("настройка есть: " + key, key in appmod.DEFAULTS, True)

app = appmod.App()
app.save_settings = lambda: None      # тест не должен трогать настоящие настройки
app.wake = threading.Event()
app.skip_event = threading.Event()
events = []
app.broadcast = lambda ev: events.append(ev)

def decide(message, settings=None):
    """Решение правил чата для сообщения."""
    ctx = chatrules.Context(settings=app.settings if settings is None else settings)
    return chatrules.decide(message, ctx)


app.settings["event_rules"] = {"raid": "announce"}
msg = {"id": "1", "name": "Bob", "msg_id": "raid", "system_msg": "Bob is now raiding the channel."}
d = decide(msg)
check("событие: решено объявить", d.kind, chatrules.EVENT)
check("событие: правило announce", d.rule, "announce")
app.run_event(d, msg)
check("событие: рейд попал в чат", events[-1].get("event"), "raid")
check("событие: текст рейда", events[-1].get("text"), "Bob is now raiding the channel.")

events.clear()
app.settings["event_rules"] = {"raid": "ignore"}
d = decide(msg)
check("событие: правило ignore", d.rule, "ignore")
app.run_event(d, msg)
check("событие: правило ignore молчит", len(events), 0)

events.clear()
mod = {"id": "2", "name": "Mod", "color": "#fff", "is_mod": True, "is_broadcaster": False,
       "is_reward": False, "is_sub": False, "is_vip": False, "login": "mod",
       "text": "!выделить привет", "emotes": [], "msg_id": "", "system_msg": ""}
d = decide(mod)
check("команда: !выделить распознана", (d.kind, d.command), (chatrules.COMMAND, "highlight"))
app.run_command(d, mod)
check("команда: !выделить помечает сообщение", events[-1].get("highlight"), True)

events.clear()
plain = dict(mod, is_mod=False)
d = decide(plain)
check("команда: !выделить не для простого зрителя", d.kind == chatrules.COMMAND, False)

events.clear()
d = decide(dict(mod, text="!курс 100 usd"))
check("команда: !курс распознана", (d.kind, d.command), (chatrules.COMMAND, "rate"))
app.run_command(d, mod)
check("команда: !курс без сети не падает", len(events), 0)

state = app.set_brb(60, "Пока")
check("BRB: таймер включён", state["active"], True)
check("BRB: текст сохранён", state["message"], "Пока")
check("BRB: выключение", app.set_brb(0)["active"], False)

paths = [getattr(r, "path", "") for r in appmod.routes]
check("маршрут: /player", "/player" in paths, True)
check("маршрут: /brb", "/brb" in paths, True)
check("маршрут: /api/brb", "/api/brb" in paths, True)

print("")
print("Провалено: %d" % len(FAILED))
if FAILED:
    for name in FAILED:
        print("  - " + name)
    sys.exit(1)
