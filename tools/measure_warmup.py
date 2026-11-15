# -*- coding: utf-8 -*-
"""Замер раннего старта CosyVoice 3: время до готовности модели и видеопамять.

Инструмент сам поднимает сервер (как при обычном запуске), снимает таймлайн
готовности и опрашивает nvidia-smi. Код приложения не меняется.

    python tools/measure_warmup.py --port 8766 --out data/bench/warmup.json
"""
import argparse
import json
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def state(base):
    try:
        with urllib.request.urlopen(base + "/api/state", timeout=5) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception:
        return None


def vram_mb():
    """Занятая видеопамять, МБ (None, если nvidia-smi недоступен)."""
    try:
        out = subprocess.run(["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
                             capture_output=True, text=True, timeout=10).stdout.strip()
        return int(out.splitlines()[0].strip())
    except Exception:
        return None


class VramSampler(threading.Thread):
    """Пишет замеры видеопамяти с отметками времени."""

    def __init__(self, t0):
        super().__init__(daemon=True)
        self.t0 = t0
        self.samples = []
        self.stop_flag = False

    def run(self):
        while not self.stop_flag:
            v = vram_mb()
            if v is not None:
                self.samples.append([round(time.time() - self.t0, 2), v])
            time.sleep(0.5)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8766)
    ap.add_argument("--timeout", type=float, default=300.0)
    ap.add_argument("--out", default="")
    ap.add_argument("--keep", action="store_true", help="не останавливать сервер после замера")
    args = ap.parse_args()

    env = dict(os.environ)
    env["TTS_PORT"] = str(args.port)
    env.pop("TTS_MOCK", None)
    env.pop("TTS_DEBUG_CHAT", None)
    env["PYTHONUTF8"] = "1"
    base = "http://127.0.0.1:%d" % args.port

    before = vram_mb()
    t0 = time.time()
    proc = subprocess.Popen([sys.executable, "-u", str(ROOT / "app" / "main.py"), "--no-browser"],
                            cwd=str(ROOT), env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    sampler = VramSampler(t0)
    sampler.start()

    marks = {}
    engine_ready = cosy_ready = None
    deadline = t0 + args.timeout
    try:
        while time.time() < deadline:
            st = state(base)
            if st:
                now = round(time.time() - t0, 2)
                marks.setdefault("http_ready", now)
                eng = (st.get("status") or {}).get("engine") or {}
                cv = (st.get("status") or {}).get("cosyvoice") or {}
                if engine_ready is None and eng.get("status") == "ready":
                    engine_ready = now
                if cosy_ready is None and cv.get("status") == "ready":
                    cosy_ready = now
                if cosy_ready is not None:
                    break
            time.sleep(0.25)
        # ровная память после прогрева: ждём ещё 10 секунд
        steady_samples = []
        if cosy_ready is not None:
            t_steady = time.time()
            while time.time() - t_steady < 10:
                v = vram_mb()
                if v is not None:
                    steady_samples.append(v)
                time.sleep(1.0)
    finally:
        sampler.stop_flag = True
        if not args.keep:
            proc.terminate()
            try:
                proc.wait(timeout=20)
            except subprocess.TimeoutExpired:
                proc.kill()

    vals = [v for _, v in sampler.samples]
    peak = max(vals) if vals else None
    report = {
        "port": args.port,
        "vram_before_mb": before,
        "http_ready_s": marks.get("http_ready"),
        "engine_ready_s": engine_ready,
        "cosyvoice_ready_s": cosy_ready,
        "vram_peak_mb": peak,
        "vram_after_ready_mb": steady_samples[-1] if steady_samples else None,
        "vram_steady_avg_mb": (round(sum(steady_samples) / len(steady_samples), 1)
                              if steady_samples else None),
        "samples": sampler.samples,
    }
    print(json.dumps({k: v for k, v in report.items() if k != "samples"},
                     ensure_ascii=False, indent=2))
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print("сохранено: %s" % args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
