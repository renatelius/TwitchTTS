# -*- coding: utf-8 -*-
"""Юнит-тесты модуля настроек app/settings.py."""
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP = ROOT / "app"
if str(APP) not in sys.path:
    sys.path.insert(0, str(APP))

from settings import DEFAULTS, READONLY, Settings  # noqa: E402

FAILED = []


def check(name, ok, detail=""):
    if not ok:
        FAILED.append(name)
    print("%-58s %s%s" % (name, "OK" if ok else "FAIL", (" | " + str(detail)) if detail else ""))


tmp = Path(tempfile.mkdtemp(prefix="tts_settings_"))
try:
    path = tmp / "settings.json"
    s = Settings(path)

    check("Settings - словарь", isinstance(s, dict))
    check("значения по умолчанию на месте", s["volume"] == DEFAULTS["volume"], s["volume"])
    check("файла нет - настройки из defaults", not path.exists())

    # значения по умолчанию не делятся между экземплярами
    s["volume"] = 1.5
    check("экземпляры не делят значения", Settings(tmp / "other.json")["volume"] == DEFAULTS["volume"])

    # загрузка из файла: только известные ключи
    path.write_text(json.dumps({"volume": 0.25, "неизвестный": 1}), encoding="utf-8")
    s = Settings(path)
    check("значение из файла прочитано", s["volume"] == 0.25, s["volume"])
    check("неизвестный ключ отброшен", "неизвестный" not in s)

    # нормализация
    s = Settings(tmp / "n.json")
    s.apply({"volume": 5, "speed": 9, "max_len": 5, "max_queue": 0, "brb_seconds": 100000})
    check("volume ограничен сверху", s["volume"] == 2.0, s["volume"])
    check("speed ограничен сверху", s["speed"] == 1.5, s["speed"])
    check("max_len ограничен снизу", s["max_len"] == 20, s["max_len"])
    check("max_queue ограничен снизу", s["max_queue"] == 1, s["max_queue"])
    check("целое ограничено сверху", s["brb_seconds"] == 2000, s["brb_seconds"])

    s = Settings(tmp / "n2.json")
    s.apply({"volume": -1, "speed": 0.1})
    check("volume ограничен снизу", s["volume"] == 0.0, s["volume"])
    check("speed ограничен снизу", s["speed"] == 0.7, s["speed"])

    # типы приводятся
    s = Settings(tmp / "t.json")
    s.apply({"muted": 1, "emotes": "", "language": 5, "ignore_users": ["a", "", " b "],
             "event_rules": {"raid": "announce"}})
    check("bool приводится к True", s["muted"] is True)
    check("пустая строка - это False", s["emotes"] is False)
    check("число становится строкой", s["language"] == "5", s["language"])
    check("список чистится", s["ignore_users"] == ["a", "b"], s["ignore_users"])
    check("словарь правил сохранён", s["event_rules"] == {"raid": "announce"})

    # неверное значение - прежнее остаётся
    s = Settings(tmp / "b.json")
    s.apply({"volume": "не число", "speed": "быстро"})
    check("некорректный volume игнорируется", s["volume"] == DEFAULTS["volume"], s["volume"])
    check("некорректный speed игнорируется", s["speed"] == DEFAULTS["speed"], s["speed"])

    # поля, которые нельзя менять из интерфейса
    s = Settings(tmp / "r.json")
    s.apply({"channel": "другой", "disabled_voices": ["x"],
             "applio_path": "C:/x", "cosyvoice_path": "C:/y"})
    check("канал не меняется", s["channel"] == DEFAULTS["channel"], s["channel"])
    check("отключённые голоса не меняются", s["disabled_voices"] == DEFAULTS["disabled_voices"])
    check("путь Applio не меняется", s["applio_path"] == DEFAULTS["applio_path"])
    check("путь CosyVoice не меняется", s["cosyvoice_path"] == DEFAULTS["cosyvoice_path"])
    check("запрещённые поля перечислены", set(READONLY) ==
          {"channel", "disabled_voices", "applio_path", "cosyvoice_path"})

    # неизвестный ключ игнорируется
    s = Settings(tmp / "u.json")
    s.apply({"неизвестный_ключ": 1})
    check("неизвестный ключ не добавлен", "неизвестный_ключ" not in s)

    # сохранение и чтение обратно
    save_path = tmp / "save.json"
    s = Settings(save_path)
    s.apply({"volume": 0.42, "language": "en", "muted": True})
    s.save()
    check("файл сохранён", save_path.exists())
    stored = json.loads(save_path.read_text(encoding="utf-8"))
    check("в файле нужное значение", stored.get("volume") == 0.42, stored.get("volume"))
    again = Settings(save_path)
    check("настройки читаются обратно", again["volume"] == 0.42 and again["language"] == "en"
          and again["muted"] is True)
    check("все ключи на месте", set(again) == set(DEFAULTS), len(again))
finally:
    shutil.rmtree(tmp, ignore_errors=True)

print("")
print("Провалено: %d" % len(FAILED))
sys.exit(1 if FAILED else 0)
