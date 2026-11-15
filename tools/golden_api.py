# -*- coding: utf-8 -*-
"""Золотой слепок HTTP-слоя: список маршрутов и ответы на типовые запросы.

Слепок снимается до и после переноса обработчиков в app/api.py. Если поведение
API изменилось незаметно - сверка это покажет.

    python tools/golden_api.py --base http://127.0.0.1:8769 --out tools/golden_api.json
    python tools/golden_api.py --base http://127.0.0.1:8769 --check tools/golden_api.json
"""
import argparse
import hashlib
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP = ROOT / "app"

# проверки не меняют состояние: только чтение и заведомо ошибочные запросы
CHECKS = [
    ("GET", "/", None),
    ("GET", "/poll", None),
    ("GET", "/chat", None),
    ("GET", "/voices", None),
    ("GET", "/overlay", None),
    ("GET", "/player", None),
    ("GET", "/brb", None),
    ("GET", "/static/style.css", None),
    ("GET", "/api/state", None),
    ("GET", "/api/emotes", None),
    ("GET", "/api/voices", None),
    ("GET", "/api/brb", None),
    ("GET", "/api/rvc/train/status", None),
    ("GET", "/audio/nope.wav", None),
    ("GET", "/api/nope", None),
    ("POST", "/api/say", {}),
    ("POST", "/api/preview", {"voice": "нет-такого"}),
    ("POST", "/api/announce", {"text": ""}),
    ("POST", "/api/control/unknown", {}),
    ("POST", "/api/settings", {"volume": 0.35, "speed": 1.0, "language": "auto"}),
    ("POST", "/api/emotes/reset", {}),
    ("POST", "/api/voice/engine", {"voice": "нет-такого", "engine": "xtts"}),
    ("POST", "/api/voice/transcript", {"voice": "нет-такого", "text": "x"}),
    ("POST", "/api/rvc/update", {"voice": "нет-такого"}),
    ("POST", "/api/voices/toggle", {"voice": "нет-такого", "enabled": True}),
]

VOLATILE_KEYS = ("duration", "elapsed", "job_id", "log", "result", "cv_text",
                  "transcript", "progress", "left", "until")


def norm(value):
    """Убирает изменчивые значения (пути, время, сгенерированные id)."""
    if isinstance(value, dict):
        out = {}
        for key, val in value.items():
            if key in VOLATILE_KEYS:
                continue
            if key in ("path", "folder"):
                out[key] = os.path.basename(str(val).rstrip("\\/"))
                continue
            if key == "voices" and isinstance(val, list):
                out[key] = [{"id": v.get("id"), "name": v.get("name"),
                             "kind": v.get("kind"), "engine": v.get("engine")}
                            for v in val if isinstance(v, dict)]
                continue
            if key == "settings" and isinstance(val, dict):
                out[key] = {k: val[k] for k in sorted(val)
                            if k not in ("twitch_token", "twitch_client_id",
                                         "applio_path", "cosyvoice_path")
                            and not isinstance(val[k], (list, dict))}
                continue
            if key == "status" and isinstance(val, dict):
                # статус движка зависит от фонового старта и не входит
                # в контракт HTTP; важна только ошибка
                out[key] = {k: ({"error": bool(v.get("error"))}
                                if isinstance(v, dict) else v)
                            for k, v in val.items()}
                continue
            out[key] = norm(val)
        return out
    if isinstance(value, list):
        return [norm(v) for v in value]
    if isinstance(value, float):
        return round(value, 2)
    return value


def routes_snapshot():
    """Список маршрутов приложения: путь, методы, имя обработчика."""
    os.environ.setdefault("TTS_MOCK", "1")
    # отладочный маршрут включается переменной окружения - убираем,
    # чтобы список маршрутов не зависел от того, как запущен процесс
    os.environ.pop("TTS_DEBUG_CHAT", None)
    if str(APP) not in sys.path:
        sys.path.insert(0, str(APP))
    import main as mainmod
    rows = []
    for route in mainmod.app.routes:
        endpoint = getattr(route, "endpoint", None)
        rows.append({
            "path": getattr(route, "path", None),
            "methods": sorted(getattr(route, "methods", None) or []),
            "handler": getattr(endpoint, "__name__", type(route).__name__),
        })
    return rows


def http_snapshot(base):
    """Ответы на типовые запросы: статус, тип, содержимое (нормализованное)."""
    rows = []
    for method, path, payload in CHECKS:
        url = base.rstrip("/") + path
        data = None
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(url, data=data, method=method,
                                     headers={"Content-Type": "application/json"})
        try:
            resp = urllib.request.urlopen(req, timeout=30)
            status, headers, raw = resp.status, resp.headers, resp.read()
        except urllib.error.HTTPError as e:
            status, headers, raw = e.code, e.headers, e.read()
        row = {"method": method, "path": path, "status": status,
               "content_type": (headers.get("Content-Type") or "").split(";")[0]}
        text = raw.decode("utf-8", "replace")
        try:
            row["json"] = norm(json.loads(text))
        except Exception:
            row["body_sha1"] = hashlib.sha1(raw).hexdigest()
            row["body_len"] = len(raw)
        rows.append(row)
    return rows


def build(base):
    snap = {"routes": routes_snapshot()}
    if base:
        snap["http"] = http_snapshot(base)
    return snap


def diff(before, after, path=""):
    """Расхождения между двумя слепками."""
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
    ap.add_argument("--base", help="адрес запущенного сервера, например http://127.0.0.1:8769")
    ap.add_argument("--out", help="куда сохранить слепок")
    ap.add_argument("--check", help="эталонный слепок для сверки")
    args = ap.parse_args()

    snap = build(args.base)
    if args.out:
        Path(args.out).write_text(json.dumps(snap, ensure_ascii=False, indent=2), encoding="utf-8")
        print("слепок сохранён: %s (маршрутов %d, проверок %d)"
              % (args.out, len(snap["routes"]), len(snap.get("http", []))))
    if args.check:
        ref = json.loads(Path(args.check).read_text(encoding="utf-8"))
        problems = diff(ref, snap)
        if problems:
            print("РАСХОЖДЕНИЯ с эталоном:")
            for p in problems[:40]:
                print("  -", p)
            return 1
        print("HTTP-слепок совпадает с эталоном (маршрутов %d, проверок %d)"
              % (len(snap["routes"]), len(snap.get("http", []))))
    return 0


if __name__ == "__main__":
    sys.exit(main())
