# -*- coding: utf-8 -*-
"""Сравнение первой реплики с ранним стартом CosyVoice 3 и без него.

Оба прогона делаются одинаково: сервер поднимается с нуля, программа какое-то
время работает без реплик, затем приходит первая реплика. Разница - только в
настройке cosyvoice_warmup. Настройки пользователя после прогона возвращаются.

    python tools/bench_warmup.py --port 8766 --count 5
"""
import argparse
import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SETTINGS = ROOT / "data" / "settings.json"


def state(base):
    try:
        with urllib.request.urlopen(base + "/api/state", timeout=10) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception:
        return None


def wait_ready(base, timeout=180):
    """Ждёт готовности XTTS и (если включён ранний старт) CosyVoice."""
    deadline = time.time() + timeout
    engine = cosy = None
    while time.time() < deadline:
        st = state(base)
        if st:
            s = st.get("status") or {}
            engine = (s.get("engine") or {}).get("status")
            cosy = (s.get("cosyvoice") or {}).get("status")
            if engine == "ready" and cosy in ("ready", "off", "error"):
                return engine, cosy
        time.sleep(0.25)
    return engine, cosy


def start_server(port):
    env = dict(os.environ)
    env["TTS_PORT"] = str(port)
    env.pop("TTS_MOCK", None)
    env.pop("TTS_DEBUG_CHAT", None)
    env["PYTHONUTF8"] = "1"
    return subprocess.Popen([sys.executable, "-u", str(ROOT / "app" / "main.py"), "--no-browser"],
                            cwd=str(ROOT), env=env, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def stop_server(proc):
    proc.terminate()
    try:
        proc.wait(timeout=20)
    except subprocess.TimeoutExpired:
        proc.kill()


def run_once(base, port, count, warmup, idle, out_dir):
    print("\n=== прогон: ранний старт %s ===" % ("вкл" if warmup else "выкл"))
    proc = start_server(port)
    try:
        t0 = time.time()
        engine, cosy = wait_ready(base)
        t_ready = round(time.time() - t0, 2)
        st = state(base) or {}
        voice = (st.get("settings") or {}).get("default_voice", "")
        print("XTTS: %s, CosyVoice: %s (через %s с)" % (engine, cosy, t_ready))
        # одинаковое время простоя перед первой репликой
        while time.time() - t0 < idle:
            time.sleep(0.5)
        env = dict(os.environ, TTS_BASE=base)
        out = Path(out_dir) / ("first-reply-warmup-%s.json" % ("on" if warmup else "off"))
        subprocess.run([sys.executable, str(ROOT / "tools" / "bench_queue.py"),
                        "--count", str(count), "--out", str(out)],
                       cwd=str(ROOT), env=env)
        res = json.loads(out.read_text(encoding="utf-8"))
        res["warmup"] = warmup
        res["engine_ready_s"] = t_ready
        res["cosyvoice_status_at_first_reply"] = cosy
        res["default_voice"] = voice
        res["idle_before_first_reply_s"] = idle
        out.write_text(json.dumps(res, ensure_ascii=False, indent=2), encoding="utf-8")
        return res
    finally:
        stop_server(proc)
        time.sleep(3)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8766)
    ap.add_argument("--count", type=int, default=5)
    ap.add_argument("--idle", type=float, default=25.0, help="сколько ждать до первой реплики")
    ap.add_argument("--out-dir", default=str(ROOT / "data" / "bench"))
    args = ap.parse_args()

    saved = SETTINGS.read_bytes()
    base = "http://127.0.0.1:%d" % args.port
    results = []
    try:
        for warmup in (True, False):
            d = json.loads(saved.decode("utf-8"))
            d["cosyvoice_warmup"] = warmup
            SETTINGS.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
            results.append(run_once(base, args.port, args.count, warmup, args.idle, args.out_dir))
    finally:
        SETTINGS.write_bytes(saved)
        print("\nнастройки возвращены")

    print("\n=== итог ===")
    for r in results:
        print("ранний старт %-3s: первая реплика %s с, цикл %s с" % (
            "вкл" if r["warmup"] else "выкл", r.get("first_speak_sec"), r.get("cycle_avg_sec")))
    a, b = results
    if a.get("first_speak_sec") and b.get("first_speak_sec"):
        print("выигрыш первой реплики: %.2f с" % (b["first_speak_sec"] - a["first_speak_sec"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
