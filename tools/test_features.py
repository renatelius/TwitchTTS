
# -*- coding: utf-8 -*-
"""Проверка новых фич (по мотивам JeetBot): раскладка, ударения,
префикс озвучки, голос на одно сообщение, запрет голосов, списки зрителей."""
import os
import sys

os.environ["TTS_MOCK"] = "1"
APP = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app")
sys.path.insert(0, APP)

import textproc        # noqa: E402
import main as appmod  # noqa: E402

FAILED = []


def check(name, got, want):
    ok = got == want
    if not ok:
        FAILED.append(name)
    print("%-58s %s | %s" % (name, "OK" if ok else "FAIL", got if ok else "got=%r want=%r" % (got, want)))


# --- раскладка ---
check("раскладка: Ghbdtn vbh", textproc.fix_layout("Ghbdtn vbh"), "Привет мир")
check("раскладка: обычный русский не трогаем", textproc.fix_layout("привет мир"), "привет мир")
check("раскладка: английский не трогаем", textproc.fix_layout("hello world"), "hello world")
check("раскладка: lol не трогаем", textproc.fix_layout("lol"), "lol")
check("раскладка: смешанный текст не трогаем", textproc.fix_layout("ghbdtn мир"), "ghbdtn мир")

# --- ударения ---
check("ударение: т+орты -> торты", textproc.strip_stress("т+орты"), "торты")
check("ударение: без плюса не меняем", textproc.strip_stress("торты"), "торты")

# --- префикс и голос на сообщение ---
check("префикс: без префикса текст целиком", appmod.split_prefix("привет", "!!!"), ("привет", ""))
check("префикс: с префиксом убираем его", appmod.split_prefix("!!! привет", "!!!"), ("привет", "!!!"))
check("голос на сообщение: !!!#дудя привет", appmod.split_voice_prefix("!!!#дудя привет"), ("дудя", "привет"))
check("голос на сообщение: обычный текст", appmod.split_voice_prefix("привет"), ("", "привет"))

# --- запрет голосов и списки зрителей ---
S = {"blocked_voices": ["флекс"], "tts_allow": [], "tts_deny": ["спамер"]}
check("запрет голоса по имени", appmod.voice_blocked(S, {"name": "Флекс", "id": "x1"}), True)
check("запрет голоса по id", appmod.voice_blocked(S, {"name": "Флексдум", "id": "флекс"}), True)
check("запрет: другой голос разрешён", appmod.voice_blocked(S, {"name": "дудя", "id": "x2"}), False)
check("список «не озвучивать»", appmod.login_allowed(S, "спамер"), False)
check("список «только своих»: посторонний", appmod.login_allowed({"tts_allow": ["друг"]}, "кто"), False)
check("список «только своих»: свой", appmod.login_allowed({"tts_allow": ["друг"]}, "Друг"), True)

# --- живой путь чата ---
a = appmod.App()
events = []
a.broadcast = lambda ev: events.append(ev)


def last_note():
    for ev in reversed(events):
        if ev.get("type") == "note":
            return ev
    return {}


def last_chat():
    for ev in reversed(events):
        if ev.get("type") == "chat":
            return ev
    return {}


def msg(text, login="зритель", **kw):
    m = {"id": "t1", "name": login, "login": login, "color": "#fff", "text": text,
         "emotes": [], "is_mod": False, "is_broadcaster": False, "is_sub": False,
         "is_vip": False, "is_reward": False}
    m.update(kw)
    return m


a.settings["tts_prefix"] = "!!!"
a.clear()
a.on_chat(msg("привет"))
check("префикс: без префикса не озвучиваем", last_chat().get("note"), "без префикса")

a.on_chat(msg("!!! привет"))
check("префикс: с префиксом озвучиваем", last_chat().get("tts"), True)
check("префикс: текст без префикса", last_chat().get("text"), "привет")

a.clear()
a.on_chat(msg("!!!#дудя привет"))
v = a.voices.resolve("дудя")
check("голос на сообщение: взят указанный голос", last_chat().get("voice"), v["name"] if v else "дудя")

a.settings["tts_prefix"] = ""
a.settings["fix_layout"] = True
a.on_chat(msg("Ghbdtn vbh"))
check("раскладка в чате: текст исправлен", last_chat().get("text"), "Привет мир")

a.settings["fix_layout"] = False
a.settings["blocked_voices"] = [v["name"]] if v else ["флекс"]
a.on_chat(msg("!г %s" % (v["name"] if v else "флекс")))
check("запрет голоса: зритель не может выбрать", "запрещён" in last_note().get("text", ""), True)

a.settings["blocked_voices"] = []
a.settings["tts_deny"] = ["зритель"]
a.on_chat(msg("привет"))
check("список «не озвучивать»: не озвучиваем", last_chat().get("note"), "не озвучивать этого зрителя")

print()
print("Провалено: %d" % len(FAILED))
sys.exit(1 if FAILED else 0)
