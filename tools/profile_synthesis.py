# -*- coding: utf-8 -*-
"""Профилирование синтеза XTTS по фазам - без изменения кода приложения.

Загружает тот же движок (app/engine.py) и тот же голос, что и сервер, но в
отдельном процессе: так можно замерить каждую фазу и снять профиль Python,
не трогая работающее приложение.

Фазы, которые мы оборачиваем:
  tokenizer.encode   - разметка текста в токены (CPU)
  gpt.generate       - авторегрессивный GPT (главная последовательная работа на GPU)
  gpt (forward)      - проекция GPT-латентов (быстро)
  hifigan_decoder    - вокодер (параллельная работа на GPU)
  shift_pitch        - сдвиг тона librosa (CPU, только если тон сдвинут)
  styles.post        - финальная обработка (фильтр, громкость)
  write_wav          - запись файла

    python tools/profile_synthesis.py --out data/bench/profile-xtts.json
"""
import argparse
import cProfile
import io
import json
import os
import pstats
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "app"
for p in (str(APP),):
    if p not in sys.path:
        sys.path.insert(0, p)

import numpy as np  # noqa: E402
import engine as enginelib  # noqa: E402
import styles as stylelib  # noqa: E402

TEXTS = [
    ("обычная реплика", "Привет, чат! Сегодня отличная погода."),
    ("вопрос", "Кто-нибудь пробовал новую версию голоса?"),
    ("цифры", "Стоимость билета выросла до двух тысяч рублей."),
    ("латиница", "Проверка латиницы: streaming, overlay, websocket."),
    ("длинная фраза", "Когда программа работает давно, важно вовремя освобождать ресурсы, "
                      "иначе видеокарта постепенно забивается и всё начинает идти медленнее."),
    ("эмоция", "(смех) Вот это поворот!"),
]

TIMES = {}
LOCK = threading.Lock()


def timed(name, fn):
    def wrapper(*a, **kw):
        t0 = time.perf_counter()
        try:
            return fn(*a, **kw)
        finally:
            dt = time.perf_counter() - t0
            with LOCK:
                TIMES.setdefault(name, []).append(dt)
    return wrapper


def install_hooks(model):
    """Оборачиваем методы модели, чтобы узнать, где уходит время."""
    model.tokenizer.encode = timed("tokenizer.encode", model.tokenizer.encode)
    model.gpt.generate = timed("gpt.generate", model.gpt.generate)
    model.gpt.forward = timed("gpt.forward", model.gpt.forward)
    model.hifigan_decoder.forward = timed("hifigan_decoder.forward", model.hifigan_decoder.forward)


def gpu_sample():
    try:
        import subprocess
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=utilization.gpu,memory.used",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=10).stdout.strip().splitlines()[0]
        util, mem = [int(x.strip()) for x in out.split(",")]
        return {"util": util, "mem": mem}
    except Exception:
        return None


class Watch:
    def __init__(self, interval=0.25):
        self.interval = interval
        self.samples = []
        self._stop = threading.Event()

    def __enter__(self):
        def loop():
            while not self._stop.is_set():
                s = gpu_sample()
                if s:
                    self.samples.append(s)
                self._stop.wait(self.interval)
        t = threading.Thread(target=loop, daemon=True)
        t.start()
        self._t = t
        return self

    def __exit__(self, *a):
        self._stop.set()
        self._t.join(timeout=5)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="")
    ap.add_argument("--voice", default="v1794519480_527499", help="папка своего голоса")
    ap.add_argument("--repeats", type=int, default=2)
    args = ap.parse_args()

    t_load0 = time.perf_counter()
    e = enginelib.XttsEngine(license_ok=(ROOT / "data" / "license_ok.txt").exists())
    e.load()
    load_s = time.perf_counter() - t_load0
    if e.status != "ready":
        print("движок не готов:", e.error)
        return 1
    install_hooks(e.model)

    voice_dir = ROOT / "voices" / args.voice
    latents = e.load_latents(voice_dir / "latents.pth")

    rows = []
    prof = cProfile.Profile()
    with Watch() as watch:
        for rep in range(args.repeats):
            for name, text in TEXTS:
                for k in list(TIMES.keys()):
                    TIMES[k].clear()
                segs = [("ru", text)]
                out = ROOT / "data" / "cache" / ("profile_%d_%s.wav" % (rep, name.replace(" ", "_")))
                t0 = time.perf_counter()
                if rep == args.repeats - 1:
                    prof.enable()
                dur = e.synthesize(segs, latents, out, speed=1.0, pitch=0.0)
                if rep == args.repeats - 1:
                    prof.disable()
                total = time.perf_counter() - t0
                row = {"rep": rep, "case": name, "total_s": round(total, 3),
                       "audio_s": round(dur, 3), "rtf": round(total / dur, 3) if dur else None}
                for k, v in TIMES.items():
                    row[k] = round(sum(v), 3)
                rows.append(row)
                print("  rep%d %-16s всего %6.2f с  звук %5.2f с  gpt %5.2f  hifi %5.2f  токен %5.3f" % (
                    rep, name, total, dur, row.get("gpt.generate", 0),
                    row.get("hifigan_decoder.forward", 0), row.get("tokenizer.encode", 0)))
                try:
                    out.unlink()
                except OSError:
                    pass

    buf = io.StringIO()
    st = pstats.Stats(prof, stream=buf)
    st.sort_stats("tottime")
    top = st.strip_dirs().sort_stats("cumulative")
    text_top = buf.getvalue()[:6000]

    report = {"load_s": round(load_s, 2), "rows": rows, "gpu": watch.samples,
              "phases": {k: round(sum(v), 3) for k, v in TIMES.items()},
              "profile_top": text_top}
    if args.out:
        outp = Path(args.out)
        outp.parent.mkdir(parents=True, exist_ok=True)
        outp.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print("отчёт:", outp)
    print()
    print("=== загрузка модели: %.1f с ===" % load_s)
    warm = [r for r in rows if r["rep"] == args.repeats - 1]
    for field, label in (("gpt.generate", "GPT (генерация кодов)"),
                         ("gpt.forward", "GPT (проекция латентов)"),
                         ("hifigan_decoder.forward", "HiFiGAN (вокодер)"),
                         ("tokenizer.encode", "токенизация")):
        vals = [r.get(field, 0) for r in warm]
        if vals:
            print("%-26s сумма %6.2f с  в среднем %5.2f с" % (label, sum(vals), sum(vals) / len(vals)))
    tot = sum(r["total_s"] for r in warm)
    print("%-26s сумма %6.2f с" % ("всего синтез", tot))
    if watch.samples:
        print("GPU: util max %d%%, средний %.0f%%, VRAM до %d МБ" % (
            max(s["util"] for s in watch.samples),
            sum(s["util"] for s in watch.samples) / len(watch.samples),
            max(s["mem"] for s in watch.samples)))
    print()
    print("=== профиль Python (tottime, 25 строк) ===")
    print(text_top.splitlines()[0] if text_top else "")
    for line in text_top.splitlines()[4:29]:
        print(line)


if __name__ == "__main__":
    sys.exit(main())
