# -*- coding: utf-8 -*-
"""Золотой слепок настроек: как сервер принимает правки из интерфейса.

Каждая правка применяется к одному и тому же исходному состоянию, поэтому
в слепке видно именно правило приведения типов и диапазонов, а не остатки
предыдущего случая. В конце настройки возвращаются как были.

    python tools/golden_settings.py --base http://127.0.0.1:8769 --out tools/golden_settings.json
    python tools/golden_settings.py --base http://127.0.0.1:8769 --check tools/golden_settings.json
"""
import argparse
import json
import os
import sys
import urllib.request
from pathlib import Path

SKIP = ("twitch_token", "twitch_client_id", "applio_path", "cosyvoice_path",
        "channel", "disabled_voices")

CASES = [
    ("volume 0.5", {"volume": 0.5}),
    ("volume 5 -> верхняя граница", {"volume": 5}),
    ("volume -1 -> нижняя граница", {"volume": -1}),
    ("volume не число -> без изменений", {"volume": "не число"}),
    ("volume 200 процентов", {"volume": 2}),
    ("speed 0.1 -> минимум", {"speed": 0.1}),
    ("speed 9 -> максимум", {"speed": 9}),
    ("speed не число", {"speed": "быстро"}),
    ("max_len 5 -> минимум", {"max_len": 5}),
    ("max_len 9999 -> максимум", {"max_len": 9999}),
    ("max_queue 0 -> минимум", {"max_queue": 0}),
    ("max_queue 999 -> максимум", {"max_queue": 999}),
    ("muted 1 -> включён", {"muted": 1}),
    ("muted 0 -> выключен", {"muted": 0}),
    ("emotes строка -> истина", {"emotes": "да"}),
    ("language число -> строка", {"language": 5}),
    ("список с пустыми строками", {"ignore_users": ["a", "", "  b  "]}),
    ("список вместо списка", {"blocked_words": "не список"}),
    ("словарь правил обрезается", {"event_rules": {"raid": "announce", "k" * 60: "v" * 60}}),
    ("правила не словарь -> без изменений", {"event_rules": "нет"}),
    ("канал не меняется из интерфейса", {"channel": "другой_канал"}),
    ("отключённые голоса не меняются", {"disabled_voices": ["x"]}),
    ("префикс число -> строка", {"tts_prefix": 123}),
    ("неизвестный ключ игнорируется", {"неизвестный_ключ": 1}),
    ("brb_seconds огромный", {"brb_seconds": 100000}),
    ("follow_rule", {"follow_rule": "announce"}),
]


def api(base, path, payload=None):
    data = None
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(base.rstrip("/") + path, data=data,
                                 headers={"Content-Type": "application/json"},
                                 method="POST" if payload is not None else "GET")
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def clean(settings):
    return {k: v for k, v in sorted(settings.items()) if k not in SKIP}


def snapshot(base):
    """Что изменила каждая правка относительно исходного состояния.

    В слепок идут только отличия от исходных настроек: сами исходные
    настройки - это личные предпочтения пользователя, и слепок не должен
    ломаться от того, что он сменил громкость.
    """
    original = clean(api(base, "/api/state")["settings"])
    rows = []
    for name, payload in CASES:
        api(base, "/api/settings", original)
        api(base, "/api/settings", payload)
        now = clean(api(base, "/api/state")["settings"])
        rows.append({"case": name, "payload": payload,
                     "changed": {k: now[k] for k in sorted(now)
                                 if now.get(k) != original.get(k)}})
    api(base, "/api/settings", original)
    return {"cases": rows}


def diff(before, after, path=""):
    out = []
    if isinstance(before, dict) and isinstance(after, dict):
        for key in sorted(set(before) | set(after)):
            out += diff(before.get(key), after.get(key), "%s/%s" % (path, key))
    elif isinstance(before, list) and isinstance(after, list):
        if len(before) != len(after):
            out.append("%s: длина %d -> %d" % (path, len(before), len(after)))
        for i, (b, a) in enumerate(zip(before, after)):
            out += diff(b, a, "%s[%d]" % (path, i))
    elif before != after:
        out.append("%s: %r -> %r" % (path, before, after))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8769")
    ap.add_argument("--out")
    ap.add_argument("--check")
    args = ap.parse_args()

    snap = snapshot(args.base)
    if args.out:
        Path(args.out).write_text(json.dumps(snap, ensure_ascii=False, indent=2), encoding="utf-8")
        print("слепок настроек сохранён: %s (случаев %d)" % (args.out, len(snap["cases"])))
    if args.check:
        ref = json.loads(Path(args.check).read_text(encoding="utf-8"))
        problems = diff(ref, snap)
        if problems:
            print("РАСХОЖДЕНИЯ с эталоном настроек:")
            for p in problems[:40]:
                print("  -", p)
            return 1
        print("слепок настроек совпадает с эталоном (случаев %d)" % len(snap["cases"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
