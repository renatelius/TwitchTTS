# -*- coding: utf-8 -*-
"""Настройки приложения: значения по умолчанию, загрузка, сохранение, нормализация.

`Settings` - словарь, поэтому `app_state.settings["volume"]` работает как раньше,
но загрузка из файла, сохранение и приведение правок из интерфейса живут
в одном месте.
"""
import json
import os
from pathlib import Path

DEFAULTS = {
    "channel": "",
    "autoconnect": True,
    "default_voice": "",
    "random_voice": False,     # зрителям без выбранного голоса давать постоянный случайный
    "volume": 0.9,           # 0.0 - 2.0 (0-200 %)
    "muted": False,          # мьют озвучки чата: реплики не читаются
    "speed": 1.0,
    "language": "auto",        # auto | ru | en
    "max_len": 200,
    "max_queue": 15,
    "cooldown": 0,             # секунд между сообщениями одного зрителя
    "audience": "all",         # all | subs (подписчики, VIP, модераторы)
    "read_names": False,
    "skip_commands": True,
    "skip_digits": True,       # не читать сообщения из одних цифр (голоса в опросах)
    "skip_rewards": True,      # не читать сообщения наград за баллы канала
    "emotes": True,            # (смех), *вздыхает* - звучат как эмоция, а не читаются словом
    "ignore_users": ["nightbot", "streamelements", "moobot", "fossabot", "streamlabs", "wizebot"],
    "blocked_words": [],
    "disabled_voices": [],
    "applio_path": "",
    "cosyvoice_path": "",
    "tts_prefix": "",          # читать только сообщения с этим префиксом (пусто - все)
    "fix_layout": False,       # "Ghbdtn vbh" -> "Привет мир"
    "style": "",               # стиль речи по умолчанию: happy/sad/angry/whisper/calm
    "me_voice": "",            # голос для действий /me (пусто - голос по умолчанию)
    "blocked_voices": [],      # голоса, которые зрители не могут выбрать
    "tts_allow": [],           # озвучивать только этих зрителей
    "tts_deny": [],            # не озвучивать этих зрителей
    "player_only": False,      # звук только на странице-плеере (для OBS)
    "highlight_rewards": False,  # выделять сообщения, отправленные за награду
    "event_rules": {},         # правила по событиям: рейд/подписка/битсы -> announce|speak|ignore
    "brb_seconds": 0,          # таймер BRB-экрана, секунд
    "brb_message": "Скоро вернусь",
    "twitch_client_id": "",     # Client ID приложения Twitch (нужен для API)
    "twitch_token": "",         # OAuth-токен Twitch: модератора или стримера
    "highlight_reward": "",     # название награды, сообщения из которой выделяются
    "speak_reward": "",         # название награды, сообщения из которой озвучиваются
    "follow_rule": "ignore",    # реакция на нового подписчика: ignore|announce|speak
}

# поля, которые нельзя менять из интерфейса
READONLY = ("channel", "disabled_voices", "applio_path", "cosyvoice_path")

# секреты: наружу отдаются замаскированными
SECRET_KEYS = ("twitch_token", "twitch_client_id")
MASK = "********"


def load_json(path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return default


def save_json(path, data):
    tmp = Path(str(path) + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


class Settings(dict):
    """Настройки приложения: значения по умолчанию плюс то, что прислал интерфейс."""

    def __init__(self, path, defaults=None):
        defaults = defaults or DEFAULTS
        super().__init__(defaults)
        self.defaults = defaults
        self.path = Path(path)
        stored = load_json(self.path, {})
        self.update({k: v for k, v in stored.items() if k in defaults})

    def save(self):
        """Записывает настройки в файл."""
        save_json(self.path, self)

    def masked(self):
        """Копия настроек, где секреты замаскированы.

        Пустое значение остаётся пустым: интерфейс должен видеть, задан ли
        секрет. Маска означает "оставить как есть" - так открытая и сохранённая
        форма настроек не затрёт настоящий токен.
        """
        out = dict(self)
        for key in SECRET_KEYS:
            if str(out.get(key) or ""):
                out[key] = MASK
        return out

    def apply(self, data):
        """Применяет правки из интерфейса: типы, диапазоны, запрещённые поля."""
        for key, default in self.defaults.items():
            if key not in data or key in READONLY:
                continue
            val = data[key]
            if key in SECRET_KEYS and val == MASK:
                continue          # маска: секрет не меняем
            try:
                if isinstance(default, bool):
                    val = bool(val)
                elif isinstance(default, int):
                    val = max(0, min(int(val), 2000))
                elif isinstance(default, float):
                    val = float(val)
                elif isinstance(default, list):
                    val = [str(x).strip() for x in val if str(x).strip()][:200]
                elif isinstance(default, dict):
                    if not isinstance(val, dict):
                        continue
                    val = {str(k)[:40]: str(v)[:40] for k, v in list(val.items())[:50]}
                else:
                    val = str(val)
            except (TypeError, ValueError):
                continue
            self[key] = val
        self["speed"] = max(0.7, min(self["speed"], 1.5))
        self["volume"] = max(0.0, min(self["volume"], 2.0))
        self["max_len"] = max(20, min(self["max_len"], 500))
        self["max_queue"] = max(1, min(self["max_queue"], 100))
        return self
