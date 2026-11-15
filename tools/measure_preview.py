# -*- coding: utf-8 -*-
"""Замер страницы голосов: сколько стоит предпрослушивание и как ведёт себя очередь.

Скрипт жмёт «Послушать» по нескольким голосам одновременно - как это делает
человек - и смотрит, когда приходят ответы и сколько они ждали.
"""
import argparse
import json
import time
import urllib.request


def post(base, path, payload, timeout=600):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(base + path, data=data,
                                 headers={"Content-Type": "application/json"})
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=timeout) as r:
        body = json.loads(r.read().decode("utf-8") or "{}")
    return time.time() - t0, body


def get(base, path, timeout=60):
    with urllib.request.urlopen(base + path, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8765")
    ap.add_argument("--count", type=int, default=8, help="сколько голосов нажать одновременно")
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    st = get(args.base, "/api/state")
    voices = [v for v in st["voices"] if v["kind"] != "standard" and v.get("enabled")]
    picked = voices[:args.count]
    print("нажимаю: %s" % ", ".join(v["name"] for v in picked))

    import threading
    results = []
    lock = threading.Lock()
    t_start = time.time()

    def worker(v):
        try:
            dur, body = post(args.base, "/api/preview", {"voice": v["id"]})
            rec = {"voice": v["name"], "engine": v.get("engine"), "ok": bool(body.get("ok")),
                   "wait_s": round(dur, 2), "at_s": round(time.time() - t_start, 2)}
        except Exception as e:
            rec = {"voice": v["name"], "engine": v.get("engine"), "ok": False,
                   "error": str(e), "at_s": round(time.time() - t_start, 2)}
        with lock:
            results.append(rec)

    threads = [threading.Thread(target=worker, args=(v,)) for v in picked]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    total = time.time() - t_start
    results.sort(key=lambda r: r["at_s"])
    for r in results:
        print("%-14s %-6s ждал %6.2f с  ответ на %6.2f с  %s" %
              (r["voice"][:14], r.get("engine"), r.get("wait_s", -1), r["at_s"],
               "ok" if r.get("ok") else r.get("error", "ошибка")))
    print("всего: %.2f с на %d голосов" % (total, len(picked)))

    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump({"base": args.base, "total_s": round(total, 2), "results": results},
                      f, ensure_ascii=False, indent=2)
        print("записано: %s" % args.out)


if __name__ == "__main__":
    main()
