# -*- coding: utf-8 -*-
"""Baseline производительности конвейера озвучки - без изменения поведения.

Меряет всё через публичный HTTP-интерфейс (ни строчки кода приложения не
трогаем):
  * оверхед HTTP: GET /api/state;
  * время синтеза по движкам: POST /api/announce (мимо очереди) - XTTS и
    CosyVoice 3 отдельно, по контрольным текстам;
  * отдача готового звука: GET /audio/<файл>;
  * загрузку GPU и видеопамяти во время замеров.

Результат: JSON с цифрами и сводка в консоли.

    python tools/profile_pipeline.py --out data/bench/perf-baseline.json
"""
import argparse
import json
import os
import statistics
import subprocess
import threading
import time
import urllib.request
import wave
from pathlib import Path

BASE = os.environ.get("TTS_BASE", "http://127.0.0.1:8765")

TEXTS = [
    ("обычная реплика", "Привет, чат! Сегодня отличная погода."),
    ("вопрос", "Кто-нибудь пробовал новую версию голоса?"),
    ("восклицание", "Это невероятно красиво звучит!"),
    ("цифры", "Стоимость билета выросла до двух тысяч рублей."),
    ("латиница", "Проверка латиницы: streaming, overlay, websocket."),
    ("длинная фраза", "Когда программа работает давно, важно вовремя освобождать ресурсы, "
                      "иначе видеокарта постепенно забивается и всё начинает идти медленнее."),
    ("эмоция", "(смех) Вот это поворот!"),
    ("тихий стиль", "(шёпотом) тихо не спорить, а то услышат."),
]


def api(path, payload=None, timeout=600):
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(BASE.rstrip("/") + path, data=data,
                                 headers={"Content-Type": "application/json"},
                                 method="POST" if payload is not None else "GET")
    t0 = time.perf_counter()
    with urllib.request.urlopen(req, timeout=timeout) as r:
        body = r.read()
    dt = time.perf_counter() - t0
    return dt, (json.loads(body.decode("utf-8")) if body else {})


def wav_duration(path):
    with wave.open(str(path), "rb") as w:
        return w.getnframes() / float(w.getframerate())


def gpu_sample():
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=utilization.gpu,memory.used,memory.total",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=10).stdout.strip().splitlines()[0]
        util, used, total = [int(x.strip()) for x in out.split(",")]
        return {"util": util, "mem_used": used, "mem_total": total}
    except Exception:
        return None


class GpuWatch:
    """Фоновая выборка загрузки GPU во время замеров."""

    def __init__(self, interval=0.5):
        self.interval = interval
        self.samples = []
        self._stop = threading.Event()
        self._thread = None

    def __enter__(self):
        def loop():
            while not self._stop.is_set():
                s = gpu_sample()
                if s:
                    self.samples.append(s)
                self._stop.wait(self.interval)
        self._thread = threading.Thread(target=loop, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *a):
        self._stop.set()
        self._thread.join(timeout=5)


def pct(values, p):
    if not values:
        return None
    vals = sorted(values)
    k = max(0, min(len(vals) - 1, int(round((p / 100.0) * (len(vals) - 1)))))
    return round(vals[k], 3)


def stat(values):
    if not values:
        return None
    return {"n": len(values), "min": round(min(values), 3),
            "p50": pct(values, 50), "p95": pct(values, 95),
            "max": round(max(values), 3),
            "mean": round(statistics.mean(values), 3)}


def main():
    global BASE
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default=BASE)
    ap.add_argument("--out", default="")
    ap.add_argument("--repeats", type=int, default=2, help="повторов на текст и голос")
    ap.add_argument("--voices", default="", help="через запятую id голосов; пусто - по движкам")
    args = ap.parse_args()
    BASE = args.base

    report = {"base": BASE, "started": time.strftime("%Y-%m-%d %H:%M:%S"), "http": {}, "synthesis": [], "gpu": {}}

    # 1) оверхед HTTP
    lat = []
    for _ in range(20):
        dt, _ = api("/api/state")
        lat.append(dt)
    report["http"]["state"] = stat(lat)

    # 2) голоса
    _, voices_res = api("/api/voices")
    voices = voices_res.get("voices") or []
    by_engine = {}
    for v in voices:
        if not v.get("enabled", True):
            continue
        by_engine.setdefault(v.get("engine") or "xtts", v)
    chosen = []
    if args.voices:
        wanted = [s.strip() for s in args.voices.split(",") if s.strip()]
        chosen = [v for v in voices if v["id"] in wanted]
    else:
        for eng in ("xtts", "cosy"):
            if eng in by_engine:
                chosen.append(by_engine[eng])
    report["voices"] = [{"id": v["id"], "name": v.get("name"), "engine": v.get("engine")} for v in chosen]

    # 3) время синтеза
    with GpuWatch() as watch:
        for v in chosen:
            for name, text in TEXTS:
                for rep in range(args.repeats):
                    try:
                        dt, res = api("/api/announce", {"text": text, "voice": v["id"]})
                    except Exception as e:
                        report["synthesis"].append({"voice": v["id"], "engine": v.get("engine"),
                                                    "case": name, "error": str(e)})
                        continue
                    if not res.get("ok"):
                        report["synthesis"].append({"voice": v["id"], "engine": v.get("engine"),
                                                    "case": name, "error": res.get("error")})
                        continue
                    dur = res.get("duration") or 0
                    fetch_dt = None
                    size = None
                    try:
                        t0 = time.perf_counter()
                        with urllib.request.urlopen(BASE.rstrip("/") + res["url"], timeout=120) as r:
                            blob = r.read()
                        fetch_dt = round(time.perf_counter() - t0, 3)
                        size = len(blob)
                    except Exception:
                        pass
                    report["synthesis"].append({
                        "voice": v["id"], "voice_name": v.get("name"), "engine": v.get("engine"),
                        "case": name, "rep": rep, "synth_s": round(dt, 3),
                        "audio_s": round(float(dur), 3),
                        "rtf": round(dt / float(dur), 3) if dur else None,
                        "fetch_s": fetch_dt, "bytes": size,
                    })
                    print("  %-9s %-16s synth %6.2f с  audio %5.2f с  RTF %.2f" % (
                        v.get("engine"), name, dt, dur, (dt / dur) if dur else 0))
    report["gpu"]["samples"] = watch.samples
    if watch.samples:
        report["gpu"]["util_max"] = max(s["util"] for s in watch.samples)
        report["gpu"]["util_mean"] = round(statistics.mean(s["util"] for s in watch.samples), 1)
        report["gpu"]["mem_used_max"] = max(s["mem_used"] for s in watch.samples)
        report["gpu"]["mem_used_mean"] = round(statistics.mean(s["mem_used"] for s in watch.samples), 1)

    # 4) свод по движкам
    summary = {}
    for eng in sorted({r.get("engine") for r in report["synthesis"] if r.get("engine")}):
        rows = [r for r in report["synthesis"] if r.get("engine") == eng and r.get("synth_s")]
        summary[eng] = {
            "synth_s": stat([r["synth_s"] for r in rows]),
            "rtf": stat([r["rtf"] for r in rows if r.get("rtf")]),
            "fetch_s": stat([r["fetch_s"] for r in rows if r.get("fetch_s")]),
            "samples": len(rows),
        }
    report["summary"] = summary
    report["http"]["state_note"] = "GET /api/state, 20 запросов"

    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print("отчёт:", out)
    print()
    print("=== сводка ===")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print("GPU:", json.dumps(report["gpu"], ensure_ascii=False))
    print("HTTP /api/state:", json.dumps(report["http"]["state"], ensure_ascii=False))


if __name__ == "__main__":
    main()
