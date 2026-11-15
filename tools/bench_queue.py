# -*- coding: utf-8 -*-
"""Замер скорости очереди озвучки: сколько проходит между репликами.

Меряется цикл очереди - от начала озвучки одной реплики до начала следующей.
До предгенерации цикл равен "синтез + звучание", после - примерно "звучанию":
пока играет текущая реплика, следующая уже синтезируется.

    python tools/bench_queue.py --count 4
    python tools/bench_queue.py --count 4 --out data/bench/before-H.json
"""
import argparse
import json
import os
import threading
import time
import urllib.request
from pathlib import Path

BASE = os.environ.get("TTS_BASE", "http://127.0.0.1:8765")
DEFAULT_TEXT = "Озвучка идёт ровно так, как это задумано"


def post(path, payload=None):
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(BASE.rstrip("/") + path, data=data,
                                 headers={"Content-Type": "application/json"},
                                 method="POST" if payload is not None else "GET")
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read().decode("utf-8"))


def state():
    try:
        with urllib.request.urlopen(BASE.rstrip("/") + "/api/state", timeout=30) as r:
            return json.loads(r.read().decode("utf-8"))["status"]
    except Exception:
        return {}


def wait_engine(timeout=180):
    """Ждёт, пока модель озвучки загрузится."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if (state().get("engine") or {}).get("status") == "ready":
            return True
        time.sleep(1.0)
    return False


def wait_idle(timeout=120):
    deadline = time.time() + timeout
    while time.time() < deadline:
        st = state()
        if not st.get("queue"):
            return True
        time.sleep(0.2)
    return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--count", type=int, default=4, help="сколько реплик")
    ap.add_argument("--text", default=DEFAULT_TEXT)
    ap.add_argument("--out", help="куда сохранить результат")
    args = ap.parse_args()

    speaks = []
    stop = False

    def reader():
        try:
            resp = urllib.request.urlopen(BASE.rstrip("/") + "/events", timeout=180)
            for raw in resp:
                line = raw.decode("utf-8", "replace").strip()
                if line.startswith("data: "):
                    try:
                        ev = json.loads(line[6:])
                    except Exception:
                        continue
                    if ev.get("type") == "speak":
                        speaks.append(time.time())
        except Exception:
            pass

    threading.Thread(target=reader, daemon=True).start()
    time.sleep(1.0)
    wait_engine()
    wait_idle()
    # очередь очищаем, чтобы замер шёл ровно по нашим репликам
    post("/api/control/clear", {})
    time.sleep(0.5)
    del speaks[:]

    t0 = time.time()
    for i in range(args.count):
        post("/api/say", {"text": args.text})
        time.sleep(0.05)

    deadline = time.time() + 300
    while time.time() < deadline and len(speaks) < args.count:
        time.sleep(0.05)
    total = time.time() - t0

    gaps = [round(speaks[i + 1] - speaks[i], 2) for i in range(len(speaks) - 1)]
    first = round(speaks[0] - t0, 2) if speaks else None
    result = {
        "base": BASE,
        "count": args.count,
        "text": args.text,
        "first_speak_sec": first,
        "gaps_sec": gaps,
        "cycle_avg_sec": round(sum(gaps) / len(gaps), 2) if gaps else None,
        "total_sec": round(total, 2),
    }
    print("первая реплика озвучена через %s с" % first)
    print("паузы между репликами: %s" % (gaps or "-"))
    print("средний цикл: %s с" % result["cycle_avg_sec"])
    print("всего: %s с" % result["total_sec"])
    if args.out:
        p = Path(args.out)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print("результат: %s" % p)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
