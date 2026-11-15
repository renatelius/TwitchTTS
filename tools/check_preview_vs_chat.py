# -*- coding: utf-8 -*-
"""Проверка: предпрослушивание не должно глушить озвучку чата.

Скрипт жмёт «Послушать» по нескольким голосам разом (как человек на странице
голосов) и в это же время отправляет реплику в чат. Меряет, через сколько
секунд реплика прозвучала.

    python tools/check_preview_vs_chat.py --base http://127.0.0.1:8765 --count 8
"""
import argparse
import json
import threading
import time
import urllib.request


def post(base, path, payload, timeout=600):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(base + path, data=data,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8") or "{}")


def get(base, path, timeout=60):
    with urllib.request.urlopen(base + path, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def speak_events(base, seen, stop):
    """Слушает SSE и запоминает момент события speak."""
    req = urllib.request.Request(base + "/events")
    with urllib.request.urlopen(req, timeout=120) as r:
        for raw in r:
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data:"):
                continue
            try:
                ev = json.loads(line[5:])
            except ValueError:
                continue
            if ev.get("type") == "speak":
                seen.append({"t": time.time(), "voice": ev.get("voice")})
            if stop.is_set():
                return


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8765")
    ap.add_argument("--count", type=int, default=8)
    ap.add_argument("--text", default="Проверка очереди: реплика должна прозвучать")
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    st = get(args.base, "/api/state")
    voices = [v for v in st["voices"] if v["kind"] != "standard" and v.get("enabled")]
    picked = voices[:args.count]

    seen = []
    stop = threading.Event()
    reader = threading.Thread(target=speak_events, args=(args.base, seen, stop), daemon=True)
    reader.start()
    time.sleep(1.0)

    t0 = time.time()
    results = []

    def preview(v):
        try:
            dur, body = None, post(args.base, "/api/preview", {"voice": v["id"]})
        except Exception as e:
            results.append({"voice": v["name"], "error": str(e)})

    def wave():
        # жмём кнопки подряд: запросы уходят пачкой
        ts = [threading.Thread(target=preview, args=(v,)) for v in picked]
        for t in ts:
            t.start()
        for t in ts:
            t.join()

    w = threading.Thread(target=wave)
    w.start()
    time.sleep(0.3)
    chat_at = time.time()
    post(args.base, "/api/say", {"text": args.text})
    w.join()

    speak = None
    deadline = time.time() + 120
    while time.time() < deadline:
        if seen:
            speak = seen[0]
            break
        time.sleep(0.05)

    stop.set()
    latency = (speak["t"] - chat_at) if speak else None
    print("предпрослушиваний: %d, отправлено подряд" % len(picked))
    if latency is None:
        print("РЕПЛИКА НЕ ПРОЗВУЧАЛА за 120 с")
    else:
        print("реплика чата прозвучала через %.2f с после отправки" % latency)
    print("всего предпрослушиваний заняло %.2f с" % (time.time() - t0))

    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump({"count": len(picked), "chat_latency_s": latency,
                       "previews_s": round(time.time() - t0, 2)}, f, ensure_ascii=False, indent=2)
        print("записано: %s" % args.out)
    return 0 if latency is not None else 1


if __name__ == "__main__":
    raise SystemExit(main())
