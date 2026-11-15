
# -*- coding: utf-8 -*-
"""Стили речи: эмоция как свойство самой озвучки.

Кроме звуков эмоций из sounds/ (смех, вздох) речь можно прочитать
с разной интонацией: радостно, грустно, зло, шёпотом. Стиль задаётся
настройкой «Стиль речи по умолчанию» или ремаркой в начале сообщения:
(радостно) привет чат!

Параметры каждого стиля:
  speed   - множитель скорости речи
  pitch   - сдвиг тона в полутонах
  temp    - "температура" XTTS: выше - живее и эмоциональнее
  lowpass - срез верхних частот, Гц (шёпот)
  gain    - громкость
"""
import re

import numpy as np

STYLES = {
    "happy": {"title": "радостно",
              "words": [r"радостн\w*", r"весел\w*", r"счастлив\w*", r"радость", r"happy\w*"],
              "speed": 1.06, "pitch": 1.5, "temp": 0.9, "lowpass": 0, "gain": 1.05},
    "sad": {"title": "грустно",
            "words": [r"грустн\w*", r"печальн\w*", r"тосклив\w*", r"sad\w*"],
            "speed": 0.92, "pitch": -1.5, "temp": 0.6, "lowpass": 0, "gain": 0.95},
    "angry": {"title": "зло",
              "words": [r"злобн\w*", r"злой", r"зло\b", r"сердит\w*", r"злюсь", r"angry\w*"],
              "speed": 1.12, "pitch": -0.8, "temp": 0.95, "lowpass": 0, "gain": 1.15},
    "whisper": {"title": "шёпотом",
                "words": [r"ш[её]пот\w*", r"тихо", r"whisper\w*"],
                "speed": 0.95, "pitch": 0.0, "temp": 0.6, "lowpass": 2600, "gain": 0.85},
    "calm": {"title": "спокойно",
             "words": [r"спокойн\w*", r"ровн\w*", r"нейтральн\w*", r"calm\w*"],
             "speed": 0.96, "pitch": 0.0, "temp": 0.65, "lowpass": 0, "gain": 1.0},
}

_LEAD = re.compile(r"^[\s]*[\(\[]\s*([^()\[\]\*<>\n]{1,30})\s*[\)\]]")
_KEY_RE = {k: re.compile(r"(?:" + "|".join(s["words"]) + r")\s*$", re.I)
           for k, s in STYLES.items()}


def match_style(text):
    """«(радостно) привет» -> («happy», «привет»).

    Ремарка стиля стоит в начале сообщения. Если такого слова нет,
    возвращается пустой ключ и текст без изменений.
    """
    if not text:
        return "", text or ""
    m = _LEAD.match(text)
    if not m:
        return "", text
    word = m.group(1).strip()
    for key, rx in _KEY_RE.items():
        if rx.search(word):
            return key, text[m.end():].strip()
    return "", text


def params(key):
    """Параметры синтеза для стиля (пусто - нейтрально)."""
    p = {"speed": 1.0, "pitch": 0.0, "temp": 0.7, "lowpass": 0, "gain": 1.0}
    spec = STYLES.get(key or "")
    if spec:
        for k in ("speed", "pitch", "temp", "lowpass", "gain"):
            p[k] = spec[k]
    return p


def lowpass(x, sr, hz):
    """Низкочастотный фильтр: речь звучит шёпотом."""
    if not hz or len(x) < 32:
        return x
    try:
        from scipy.signal import butter, lfilter
        b, a = butter(4, min(0.99, float(hz) / (sr / 2.0)), btype="low")
        return np.asarray(lfilter(b, a, np.asarray(x, dtype=np.float32)), dtype=np.float32)
    except Exception:
        return np.asarray(x, dtype=np.float32)


def post(x, sr, p, do_pitch=False):
    """Финальная обработка речи: тон (если надо), срез частот и громкость."""
    x = np.asarray(x, dtype=np.float32).reshape(-1)
    p = p or {}
    if do_pitch and abs(float(p.get("pitch") or 0)) > 0.01:
        try:
            import librosa
            x = librosa.effects.pitch_shift(x, sr=int(sr), n_steps=float(p["pitch"]))
        except Exception:
            pass
    x = lowpass(x, sr, p.get("lowpass") or 0)
    g = float(p.get("gain") or 1.0)
    if abs(g - 1.0) > 0.01:
        x = x * g
    return np.asarray(x, dtype=np.float32)
