
# -*- coding: utf-8 -*-
"""Подбор стилей речи для каждого XTTS-голоса.

Для каждого голоса и каждого стиля генерируются варианты с разными
параметрами синтеза (температура, скорость, тон). Каждый вариант
оценивается по акустическим признакам стиля - тон, энергия, разброс
интонации, тембровая яркость, - и лучший сохраняется в
data/style_presets.json.

Приложение берёт эти параметры, когда зритель пишет (радостно) или
в настройках выбран стиль по умолчанию: у каждого голоса своя
«радостность» и свой «шёпот».

Запуск: python tools/gen_style_presets.py [--voices 12] [--variants 10]
"""
import argparse
import json
import os
import random
import sys
import wave

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "app"))

import styles as stylelib          # noqa: E402
from engine import OUT_SR, XttsEngine  # noqa: E402
from voices import VoiceStore      # noqa: E402

PHRASES = ["Привет, чат, как у вас дела?",
           "Сегодня отличная погода, друзья.",
           "Я рад видеть всех в чате сегодня."]


# ------------------------------------------------------------- метрики
def f0_track(x, sr):
    """Тон по кадрам: средний и разброс."""
    win = 2048
    hop = 1024
    vals = []
    for i in range(0, max(1, len(x) - win), hop):
        seg = x[i:i + win]
        if len(seg) < win or float(np.abs(seg).max()) < 0.02:
            continue
        seg = seg - seg.mean()
        s = seg * np.hanning(win)
        r = np.correlate(s, s, "full")[win - 1:]
        lo, hi = int(sr / 350), int(sr / 60)
        if hi >= len(r):
            continue
        band = r[lo:hi]
        k = int(np.argmax(band))
        if band[k] > 0.3 * band[0]:
            vals.append(sr / (lo + k))
    if not vals:
        return 0.0, 0.0
    a = np.asarray(vals)
    return float(a.mean()), float(a.std())


def metrics(x, sr):
    """Признаки записи для оценки эмоциональности."""
    x = np.asarray(x, dtype=np.float32).reshape(-1)
    dur = len(x) / float(sr)
    rms = float(np.sqrt((x * x).mean()) + 1e-9)
    f0m, f0s = f0_track(x, sr)
    n = 1024
    cents = []
    zcrs = []
    for i in range(0, max(1, len(x) - n), 2048):
        seg = x[i:i + n]
        if len(seg) < n or float(np.abs(seg).max()) < 0.02:
            continue
        w = seg * np.hanning(n)
        sp = np.abs(np.fft.rfft(w)) ** 2
        fr = np.fft.rfftfreq(n, 1 / sr)
        cents.append(float((sp * fr).sum() / (sp.sum() + 1e-9)))
        zcrs.append(float(np.mean(np.abs(np.diff(np.sign(seg))) > 0)))
    centroid = float(np.median(cents)) if cents else 0.0
    zcr = float(np.median(zcrs)) if zcrs else 0.0
    return {"dur": dur, "rms": rms, "f0_mean": f0m, "f0_std": f0s,
            "centroid": centroid, "zcr": zcr}


# ------------------------------------------------- оценка под стиль
WEIGHTS = {
    # стиль: веса по признакам (нормируются внутри набора кандидатов)
    "happy":   {"f0_mean": 0.45, "f0_std": 0.30, "rms": 0.25},
    "sad":     {"f0_mean": -0.40, "rms": -0.25, "dur": -0.20, "f0_std": -0.15},
    "angry":   {"rms": 0.35, "centroid": 0.30, "f0_std": 0.20, "dur": -0.15},
    "whisper": {"rms": -0.35, "zcr": 0.35, "f0_std": -0.15, "centroid": -0.15},
    "calm":    {"f0_std": -0.45, "dur": 0.20, "rms": 0.20, "f0_mean": 0.15},
}


def score_candidates(cands):
    """Выбрать лучший вариант под стиль: кандидаты - [(params, metrics)]."""
    if not cands:
        return None, None
    keys = set()
    for _, m in cands:
        keys |= set(m)
    stats = {}
    for k in keys:
        vals = np.array([m.get(k, 0.0) for _, m in cands], dtype=np.float64)
        mean, std = float(vals.mean()), float(vals.std())
        stats[k] = (mean, std if std > 1e-9 else 1.0)
    best, best_score = None, -1e9
    for p, m in cands:
        total = 0.0
        for k, w in WEIGHTS[p["style"]].items():
            mean, std = stats[k]
            total += w * ((m.get(k, 0.0) - mean) / std)
        if total > best_score:
            best_score, best = total, (p, m)
    return best, best_score


def variants_for(style, n, rng):
    """Кандидаты: базовые параметры стиля плюс случайные отклонения."""
    base = stylelib.params(style)
    out = []
    for _ in range(n):
        temp = max(0.35, min(1.0, base["temp"] + rng.uniform(-0.12, 0.12)))
        speed = max(0.7, min(1.4, base["speed"] * rng.uniform(0.94, 1.06)))
        pitch = base["pitch"] + rng.uniform(-1.2, 1.2)
        out.append({"style": style, "speed": speed, "pitch": pitch, "temp": temp,
                    "gain": base["gain"], "lowpass": base["lowpass"]})
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--voices", type=int, default=0, help="ограничить число голосов (0 - все)")
    ap.add_argument("--variants", type=int, default=10, help="кандидатов на стиль")
    ap.add_argument("--out", default=os.path.join(ROOT, "data", "style_presets.json"))
    args = ap.parse_args()

    engine = XttsEngine(license_ok=os.path.exists(os.path.join(ROOT, "data", "license_ok.txt")))
    engine.load()
    if engine.status != "ready":
        print("движок не готов:", engine.status)
        return 1
    store = VoiceStore(os.path.join(ROOT, "voices"), engine, lambda: [])
    voices = [v for v in store.list() if v.get("enabled") and v.get("engine") != "cosy"]
    if args.voices:
        voices = voices[:args.voices]
    print("голосов:", len(voices), flush=True)

    presets = {}
    if os.path.exists(args.out):
        try:
            presets = json.load(open(args.out, encoding="utf-8"))
        except Exception:
            presets = {}
    for v in voices:
        vid, name = v["id"], v["name"]
        try:
            latents = store.latents(vid)
        except Exception as e:
            print("[%s] нет слепка: %s" % (name, e), flush=True)
            continue
        presets.setdefault(vid, {})
        for style in stylelib.STYLES:
            rng = random.Random("%s|%s" % (vid, style))
            cands = []
            for p in variants_for(style, args.variants, rng):
                text = rng.choice(PHRASES)
                tmp = os.path.join(ROOT, "data", "cache", "sp_%s_%s.wav" % (vid.replace(":", "_"), style))
                try:
                    engine.synthesize([("ru", text)], latents, tmp,
                                       p["speed"], p["pitch"],
                                       style={"temp": p["temp"], "gain": 1.0, "lowpass": 0,
                                              "speed": 1.0, "pitch": 0.0})
                    with wave.open(tmp, "rb") as w:
                        sr = w.getframerate()
                        x = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32) / 32768.0
                    m = metrics(x, sr)
                except Exception as e:
                    print("  [сбой]", e, flush=True)
                    continue
                finally:
                    try:
                        os.remove(tmp)
                    except OSError:
                        pass
                cands.append((p, m))
            best, sc = score_candidates(cands)
            if not best:
                continue
            p, m = best
            presets[vid][style] = {"speed": round(p["speed"], 3), "pitch": round(p["pitch"], 2),
                                   "temp": round(p["temp"], 3), "gain": p["gain"], "lowpass": p["lowpass"]}
            print("[%s] %-8s speed=%.2f pitch=%+.2f temp=%.2f  f0=%.0f rms=%.3f" % (
                name, style, p["speed"], p["pitch"], p["temp"], m["f0_mean"], m["rms"]), flush=True)
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(presets, f, ensure_ascii=False, indent=1)
    print("сохранено:", args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
