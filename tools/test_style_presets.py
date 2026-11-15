
# -*- coding: utf-8 -*-
"""Проверка подбора стилей: метрики записи и выбор лучшего кандидата."""
import os
import sys

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))
sys.path.insert(0, os.path.join(ROOT, "app"))

import gen_style_presets as g   # noqa: E402

FAILED = []


def check(name, got, want):
    ok = got == want
    if not ok:
        FAILED.append(name)
    print("%-58s %s | %s" % (name, "OK" if ok else "FAIL", got if ok else "got=%r want=%r" % (got, want)))


sr = 24000
t = np.arange(sr) / sr
tone = (0.3 * np.sin(2 * np.pi * 200 * t)).astype(np.float32)
m = g.metrics(tone, sr)
check("метрики: тон 200 Гц", abs(m["f0_mean"] - 200) < 15, True)
check("метрики: громкость ~0.3/√2", abs(m["rms"] - 0.3 / np.sqrt(2)) < 0.02, True)
check("метрики: длительность 1 с", abs(m["dur"] - 1.0) < 0.05, True)
check("метрики: спектральный центр низкий для 200 Гц", m["centroid"] < 600, True)

# --- выбор кандидата под стиль ---
def cand(style, f0, f0s, rms, dur, centroid, zcr, **kw):
    p = {"style": style, "speed": 1.0, "pitch": 0.0, "temp": 0.7, "gain": 1.0, "lowpass": 0}
    p.update(kw)
    return p, {"f0_mean": f0, "f0_std": f0s, "rms": rms, "dur": dur,
                "centroid": centroid, "zcr": zcr}

happy = [cand("happy", 210, 40, 0.12, 2.0, 800, 0.05),
         cand("happy", 120, 10, 0.05, 2.4, 600, 0.05),
         cand("happy", 160, 25, 0.08, 2.2, 700, 0.05)]
best, score = g.score_candidates(happy)
check("стиль радостно: выбран высокий тон", best[1]["f0_mean"], 210)

angry = [cand("angry", 150, 30, 0.20, 1.6, 1800, 0.10),
         cand("angry", 150, 30, 0.05, 2.6, 700, 0.03)]
best, score = g.score_candidates(angry)
check("стиль зло: выбрана громкая и яркая", best[1]["rms"], 0.20)

whisper = [cand("whisper", 150, 20, 0.02, 2.4, 700, 0.20),
           cand("whisper", 150, 20, 0.15, 2.4, 700, 0.02)]
best, score = g.score_candidates(whisper)
check("стиль шёпот: выбрана тихая и шумная", best[1]["rms"], 0.02)

calm = [cand("calm", 150, 5, 0.09, 2.4, 700, 0.05),
        cand("calm", 150, 45, 0.09, 2.4, 700, 0.05)]
best, score = g.score_candidates(calm)
check("стиль спокойно: выбран ровный тон", best[1]["f0_std"], 5)

check("нет кандидатов - нет результата", g.score_candidates([])[0], None)

print()
print("Провалено: %d" % len(FAILED))
sys.exit(1 if FAILED else 0)
