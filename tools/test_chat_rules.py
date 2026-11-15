# -*- coding: utf-8 -*-
"""Проверка правил чата (app/chat_rules.py) и сверка с золотым слепком.

Правила - чистая функция: их можно проверять, не собирая приложение.
"""
import json
import os
import sys
from pathlib import Path

os.environ["TTS_MOCK"] = "1"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "app"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import chat_rules as chatrules   # noqa: E402
import golden_chat                # noqa: E402
import main as appmod             # noqa: E402
import settings as settingsmod     # noqa: E402

FAILED = []


def check(name, got, want):
    ok = got == want
    if not ok:
        FAILED.append(name)
    print("%-58s %s | %s" % (name, "OK" if ok else "FAIL",
                             got if ok else "got=%r want=%r" % (got, want)))


BASE = dict(settingsmod.DEFAULTS)
BASE["channel"] = "test"


def message(text, user="zviewer", mod=False, **extra):
    data = {"id": "m1", "login": user.lower(), "name": user, "color": "#fff",
            "text": text, "emotes": [], "is_mod": mod, "is_broadcaster": False,
            "is_reward": False, "reward_id": "", "is_sub": False, "is_vip": False,
            "msg_id": "", "system_msg": ""}
    data.update(extra)
    return data


def decide(text, settings=None, ctx=None, **msg_kw):
    """Решение правил для сообщения."""
    context = ctx if ctx is not None else chatrules.Context(settings=settings if settings is not None else BASE)
    return chatrules.decide(message(text, **msg_kw), context)


# --- таблица: сообщение -> решение ---
check("обычное сообщение", decide("привет чат").kind, chatrules.READ)
check("ремарка стиля: радостно", decide("(радостно) привет").style_key, "happy")
check("ремарка стиля: шёпотом", decide("(шёпотом) тише").style_key, "whisper")
check("цифра не читается", decide("123").note, "цифра")
check("пустое сообщение", decide("   ").note, "нечего читать")
check("озвучка выключена", decide("привет", {"muted": True}).note, "озвучка выключена")
check("префикс не совпал", decide("привет", {"tts_prefix": "!!!"}).note, "без префикса")
check("префикс совпал", decide("!!! привет", {"tts_prefix": "!!!"}).kind, chatrules.READ)
check("бот в стоп-листе", decide("привет", {"ignore_users": ["Nightbot"]}, user="Nightbot").note, "бот")
check("зритель в запрет-списке", decide("привет", {"tts_deny": ["zviewer"]}).note,
      "не озвучивать этого зрителя")
check("запрещённое слово", decide("это плохое", {"blocked_words": ["плохое"]}).note,
      "запрещённое слово")
check("только подписчикам", decide("привет", {"audience": "subs"}).note,
      "только для подписчиков")
check("подписчику можно", decide("привет", {"audience": "subs"}, user="sub", is_sub=True).kind,
      chatrules.READ)
check("задача голоса на сообщение", decide("!!!#дудя привет").voice_hint, "дудя")
check("действие /me", decide("/me машет рукой").is_me, True)

# --- команды ---
d = decide("!г дудя")
check("команда: выбор голоса", (d.kind, d.command), (chatrules.COMMAND, "voice"))
check("команда: выбор голоса - аргумент", d.args, "дудя")
d = decide("!скип", mod=True)
check("команда: пропуск модератором", (d.kind, d.command), (chatrules.COMMAND, "skip"))
d = decide("!скип")
check("команда: пропуск не модератором", d.kind, chatrules.SKIP)
d = decide("!курс 100 usd")
check("команда: курс валют", (d.kind, d.command), (chatrules.COMMAND, "rate"))
d = decide("!перевод hello")
check("команда: перевод", (d.kind, d.command), (chatrules.COMMAND, "translate"))
d = decide("!выделить важно", mod=True)
check("команда: выделить модератором", (d.kind, d.command), (chatrules.COMMAND, "highlight"))
d = decide("!выделить важно")
check("команда: выделить не модератором", d.kind, chatrules.SKIP)
d = decide("!закрепить наверх", mod=True)
check("команда: закрепить модератором", (d.kind, d.command), (chatrules.COMMAND, "pin"))
d = decide("!брб 5", mod=True)
check("команда: BRB модератором", (d.kind, d.command), (chatrules.COMMAND, "brb"))
d = decide("!таймаут vasya 300", mod=True)
check("команда: таймаут модератором", (d.kind, d.command), (chatrules.COMMAND, "timeout"))
check("команда: таймаут - аргументы", d.args, "vasya 300")
d = decide("!таймаут vasya 300")
check("команда: таймаут не модератором", d.kind, chatrules.SKIP)

# --- события Twitch ---
d = decide("Bob is now raiding the channel.", msg_id="raid")
check("событие: рейд", (d.kind, d.event), (chatrules.EVENT, "raid"))
check("событие: правило по умолчанию", d.rule, "announce")
d = decide("Bob is now raiding the channel.", msg_id="raid",
           settings={"event_rules": {"raid": "ignore"}})
check("событие: правило ignore", d.rule, "ignore")
d = decide("что-то", system_msg="Bob just subscribed!")
check("событие: подписка по тексту", d.event, "sub")

# --- награда ---
d = decide("сообщение с наградой", is_reward=True, reward_id="r1",
           settings={"highlight_rewards": True})
check("награда: выделяется", d.highlight, True)
d = decide("сообщение с наградой", is_reward=True, reward_id="r1")
check("награда: без настройки не выделяется", d.highlight, False)
d = decide("сообщение с наградой", is_reward=True, reward_id="r1",
           ctx=chatrules.Context(settings={"highlight_reward": "выделить сообщение"},
                                 reward_title="выделить сообщение"))
check("награда: по названию", d.highlight, True)

# --- очередь и кулдаун ---
d = decide("привет", ctx=chatrules.Context(settings=BASE, queue_len=99))
check("очередь полна", d.note, "очередь полна")
d = decide("привет", ctx=chatrules.Context(settings=dict(BASE, cooldown=60),
                                           now=1000.0, last_spoke=990.0))
check("слишком часто", d.note, "слишком часто")

# --- золотой слепок: поведение не должно измениться ---
reference = Path(ROOT) / "tools" / "golden_chat.json"
if reference.exists():
    want = json.loads(reference.read_text(encoding="utf-8"))
    got = golden_chat.build_snapshot()
    check("золотой слепок совпадает с эталоном", got == want, True)
else:
    print("эталон tools/golden_chat.json не найден - сверка пропущена")

print("")
print("Провалено: %d" % len(FAILED))
if FAILED:
    for name in FAILED:
        print("  - " + name)
    sys.exit(1)
