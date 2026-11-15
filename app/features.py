# -*- coding: utf-8 -*-
"""Дополнительные возможности: события чата Twitch, курс валют, перевод текста."""
import json
import re
import time
import urllib.parse
import urllib.request

# Twitch помечает системные сообщения тегом msg-id
MSG_ID_EVENTS = {
    "raid": "raid",
    "midnightblue": "sub",
    "highlighted-them": "sub",
    "resub": "sub",
    "sub": "sub",
    "subgift": "gift",
    "anonsubmysterygift": "gift",
    "giftpaidupgrade": "gift",
    "bitsbadgetier": "bits",
    "ritual": "ritual",
    "announcement": "announcement",
    "viewermilestone": "milestone",
    "charitydonation": "donation",
}

EVENT_LABELS = {
    "raid": "Рейд",
    "sub": "Подписка",
    "gift": "Подарочная подписка",
    "bits": "Битсы",
    "ritual": "Ритуал",
    "announcement": "Объявление",
    "milestone": "Отметка зрителей",
    "donation": "Донат",
}

# что делать с событием, если правило не задано
DEFAULT_RULES = {"raid": "announce", "sub": "announce", "gift": "announce", "bits": "ignore"}

_UA = "TwitchTTS/1.0"
_RATE_CACHE = {"at": 0.0, "rates": {}}
_TRANSLATE_CACHE = {}


def clean_system(text):
    """Выкидывает html-теги из системного сообщения Twitch."""
    text = re.sub(r"<[^>]+>", " ", text or "")
    return re.sub(r"\s+", " ", text).strip()


def detect_event(msg_id, system_msg):
    """Тип события по тегам IRC: рейд, подписка, битсы... Пустая строка - обычное сообщение."""
    kind = MSG_ID_EVENTS.get((msg_id or "").strip().lower())
    if kind:
        return kind
    text = clean_system(system_msg).lower()
    if not text:
        return ""
    if "raiding" in text or "raid" in text:
        return "raid"
    if "subscribed" in text or "subscription" in text:
        return "sub"
    if "gift" in text and ("sub" in text or "subscription" in text):
        return "gift"
    if "bits" in text:
        return "bits"
    return ""


def event_text(kind, user, system_msg):
    """Человеческий текст события для чата."""
    text = clean_system(system_msg)
    if text:
        return text
    label = EVENT_LABELS.get(kind, kind)
    if kind == "raid":
        return "%s от %s" % (label, user)
    return "%s: %s" % (label, user)


def _get_json(url, timeout=8):
    req = urllib.request.Request(url, headers={"User-Agent": _UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def rates(timeout=8):
    """Курсы валют к доллару. Кэш на 10 минут."""
    now = time.time()
    if now - _RATE_CACHE["at"] < 600 and _RATE_CACHE["rates"]:
        return _RATE_CACHE["rates"]
    data = _get_json("https://open.er-api.com/v6/latest/USD", timeout=timeout)
    got = data.get("rates") or {}
    if not got:
        raise ValueError("пустой ответ с курсами валют")
    _RATE_CACHE.update(at=now, rates=got)
    return got


def convert(amount, src, dst, timeout=8):
    """Сумма в одной валюте в другой. None - валюта неизвестна."""
    src = (src or "").strip().upper()
    dst = (dst or "").strip().upper()
    if not src or not dst or amount is None:
        return None
    try:
        amount = float(amount)
    except (TypeError, ValueError):
        return None
    if src == dst:
        return amount
    table = rates(timeout=timeout)
    if src not in table or dst not in table:
        return None
    usd = amount / float(table[src])
    return usd * float(table[dst])


def fmt_num(value, digits=2):
    """Число с пробелами между разрядами: 9500.5 -> '9 500,50'."""
    try:
        value = float(value)
    except (TypeError, ValueError):
        return str(value)
    text = ("%%.%df" % digits) % abs(value)
    whole, _, frac = text.partition(".")
    whole = re.sub(r"\B(?=(\d{3})+(?!\d))", " ", whole)
    out = whole + ("," + frac if frac else "")
    return ("-" if value < 0 else "") + out


_RATE_RE = re.compile(r"([0-9]+(?:[.,][0-9]+)?)\s*([a-zA-Z]{3})\s*(?:в\s+)?([a-zA-Z]{3})?")


def parse_rate(text, default_dst="rub"):
    """'100 usd' или '100 usd в eur' -> (сумма, откуда, куда)."""
    text = (text or "").strip()
    m = _RATE_RE.search(text)
    if not m:
        return None, "", default_dst
    try:
        amount = float(m.group(1).replace(",", "."))
    except ValueError:
        return None, "", default_dst
    return amount, m.group(2).lower(), (m.group(3) or default_dst).lower()


def translate(text, target="ru", source="auto", timeout=8):
    """Перевод текста через публичный endpoint переводчика."""
    text = (text or "").strip()
    if not text:
        return ""
    key = (source, target, text[:200])
    hit = _TRANSLATE_CACHE.get(key)
    now = time.time()
    if hit and now - hit[0] < 600:
        return hit[1]
    q = urllib.parse.quote(text)
    url = ("https://translate.googleapis.com/translate_a/single?client=gtx&sl=%s&tl=%s&dt=t&q=%s"
           % (source, target, q))
    data = _get_json(url, timeout=timeout)
    parts = []
    for chunk in (data[0] or []):
        if isinstance(chunk, (list, tuple)) and chunk and isinstance(chunk[0], str):
            parts.append(chunk[0])
    out = "".join(parts).strip()
    if out:
        _TRANSLATE_CACHE[key] = (now, out)
    return out


def translate_safe(text, target="ru"):
    """Перевод с запасным сервисом, если основной не ответил."""
    try:
        got = translate(text, target)
        if got:
            return got
    except Exception:
        pass
    try:
        url = ("https://api.mymemory.translated.net/get?q=%s&langpair=en|%s"
               % (urllib.parse.quote((text or "").strip()), target))
        data = _get_json(url)
        return ((data.get("responseData") or {}).get("translatedText") or "").strip()
    except Exception:
        return ""


def parse_brb(text):
    """'10' -> 600 секунд, '90с' -> 90 секунд, '0' -> выключить. Возвращает (секунды, текст)."""
    text = (text or "").strip()
    if not text:
        return 0, ""
    m = re.search(r"([0-9]+)\s*(минут\w*|мин|м|секунд\w*|сек|с)?(?![а-яёa-z])", text)
    if not m:
        return 0, ""
    num = int(m.group(1))
    unit = (m.group(2) or "м").lower()
    seconds = num if unit.startswith("с") else num * 60
    message = text[m.end():].strip(" -—:,")
    return max(0, min(seconds, 6 * 3600)), message
