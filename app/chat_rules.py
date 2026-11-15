# -*- coding: utf-8 -*-
"""Правила чата: что делать с сообщением.

Модуль ничего не выполняет - только принимает решение. Исполняет App.
Благодаря этому правила проверяются чистыми вызовами decide(), а не сборкой
приложения с очередью и синтезом.
"""
from dataclasses import dataclass

import features as featlib
import styles as stylelib
from textproc import clean_text, fix_layout, strip_stress

# виды решения
READ = "read"
SKIP = "skip"
COMMAND = "command"
EVENT = "event"

# реестр команд чата: синоним -> каноническое имя
COMMANDS = {
    "!г": "voice", "!голос": "voice", "!voice": "voice", "!tts": "voice", "!ттс": "voice",
    "!скип": "skip", "!skip": "skip",
    "!курс": "rate", "!курсы": "rate", "!rate": "rate", "!fx": "rate", "!валюта": "rate",
    "!перевод": "translate", "!переведи": "translate", "!translate": "translate", "!tr": "translate",
    "!выделить": "highlight", "!highlight": "highlight",
    "!закрепить": "pin", "!pin": "pin",
    "!брб": "brb", "!brb": "brb",
    "!таймаут": "timeout", "!timeout": "timeout", "!то": "timeout",
}

# команды, доступные только модератору и стримеру
MOD_ONLY = ("skip", "highlight", "pin", "brb", "timeout")


@dataclass
class Context:
    """Снимок состояния, который нужен для решения. Собирает App."""

    settings: dict
    last_spoke: float = 0.0     # когда этот зритель говорил в последний раз
    queue_len: int = 0          # сколько реплик в очереди
    now: float = 0.0            # текущее время
    reward_title: str = ""      # название награды (из Twitch API)


@dataclass
class Decision:
    """Что решили сделать с сообщением."""

    kind: str = READ
    text: str = ""              # текст сообщения для чата
    speak_text: str = ""        # текст для озвучки (очищенный)
    note: str = ""              # причина, почему не читаем
    style_key: str = ""         # стиль из ремарки: happy/sad/...
    voice_hint: str = ""        # голос из !!!#голос или "random"
    is_me: bool = False
    is_reward: bool = False
    reward: str = ""            # название награды
    reward_speak: bool = False  # награду надо озвучить
    highlight: bool = False     # сообщение выделить
    event: str = ""             # тип события Twitch
    event_text: str = ""        # текст события
    rule: str = ""              # реакция на событие: announce|speak|ignore
    command: str = ""           # каноническое имя команды
    args: str = ""              # аргументы команды


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


def _is_mod(msg):
    """Модератор или стример."""
    return bool(msg.get("is_mod") or msg.get("is_broadcaster"))


def decide(msg, ctx):
    """Решает, что делать с сообщением из чата. Ничего не выполняет.

    Возвращает Decision: прочитать, пропустить (с причиной), выполнить команду
    или объявить событие Twitch.
    """
    s = ctx.settings
    text = str(msg.get("text") or "").strip()
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
    low = text.lower()
    first, _, rest = low.partition(" ")
    first_raw, _, rest_raw = text.partition(" ")

    d = Decision(kind=READ, text=text, style_key=style_key, voice_hint=one_voice,
                 is_me=is_me, is_reward=bool(msg.get("is_reward")))

    # --- выбор голоса зрителем: доступен всем ---
    cmd = COMMANDS.get(first_raw.lower())
    if cmd == "voice":
        # имя голоса ищем в нижнем регистре - так было раньше
        d.kind, d.command, d.args = COMMAND, "voice", rest
        return d
    # --- пропуск реплики: только модератор ---
    if cmd == "skip" and _is_mod(msg):
        d.kind, d.command, d.args = COMMAND, "skip", rest
        return d

    # --- события Twitch: рейд, подписка, битсы - по системным тегам IRC ---
    kind = featlib.detect_event(msg.get("msg_id", ""), msg.get("system_msg", ""))
    if kind:
        rules = s.get("event_rules") or {}
        d.kind = EVENT
        d.event = kind
        d.rule = str(rules.get(kind, featlib.DEFAULT_RULES.get(kind, "ignore"))).lower()
        d.event_text = featlib.event_text(kind, msg.get("name", ""), msg.get("system_msg", ""))
        return d

    # --- остальные команды ---
    if cmd and cmd not in ("voice", "skip"):
        if cmd in MOD_ONLY and not _is_mod(msg):
            cmd = None
        else:
            d.kind, d.command, d.args = COMMAND, cmd, rest_raw
            return d

    # --- сообщение, отправленное за награду ---
    if d.is_reward:
        title = ctx.reward_title or ""
        if title:
            d.reward = title
        low_title = title.lower()
        if low_title and low_title == str(s.get("highlight_reward") or "").strip().lower():
            d.highlight = True
        if low_title and low_title == str(s.get("speak_reward") or "").strip().lower():
            d.reward_speak = True
        elif s.get("highlight_rewards"):
            d.highlight = True

    # --- почему не читаем ---
    note = ""
    if s.get("muted"):
        note = "озвучка выключена"
    elif no_prefix:
        note = "без префикса"
    elif msg.get("login") in [u.lower() for u in s.get("ignore_users") or []]:
        note = "бот"
    elif not login_allowed(s, msg.get("login")):
        note = "не озвучивать этого зрителя"
    elif text.startswith("!") and s.get("skip_commands"):
        note = "команда"
    elif s.get("skip_digits") and text.strip().isdigit():
        # голоса в опросах: зритель пишет "1", "2" или "3" - вслух это не читаем
        note = "цифра"
    elif d.is_reward and s.get("skip_rewards") and not d.reward_speak:
        note = "награда за баллы"
    elif s.get("audience") == "subs" and not (msg.get("is_sub") or msg.get("is_vip")
                                              or _is_mod(msg)):
        note = "только для подписчиков"
    elif any(w and w.lower() in low for w in s.get("blocked_words") or []):
        note = "запрещённое слово"
    elif s.get("cooldown") and ctx.now - ctx.last_spoke < s["cooldown"]:
        note = "слишком часто"
    if note:
        d.kind, d.note = SKIP, note
        return d

    # --- что именно читать ---
    cleaned = clean_text(speak_text or text, msg.get("emotes"), int(s.get("max_len") or 200),
                         emotes=bool(s.get("emotes")))
    if not cleaned:
        d.kind, d.note = SKIP, "нечего читать"
        return d
    d.speak_text = cleaned
    if ctx.queue_len >= int(s.get("max_queue") or 15):
        d.kind, d.note = SKIP, "очередь полна"
        return d
    return d
