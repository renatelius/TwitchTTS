
# -*- coding: utf-8 -*-
"""Проверка стилей речи: разбор ремарки, параметры, обработка звука."""
import os
import sys

APP = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app")
sys.path.insert(0, APP)

import numpy as np  # noqa: E402
import styles         # noqa: E402

FAILED = []


def check(name, got, want):
    ok = got == want
    if not ok:
        FAILED.append(name)
    print("%-58s %s | %s" % (name, "OK" if ok else "FAIL", got if ok else "got=%r want=%r" % (got, want)))


# --- разбор ремарки стиля ---
check("стиль: (радостно) привет", styles.match_style("(радостно) привет чат"), ("happy", "привет чат"))
check("стиль: [зло] хватит", styles.match_style("[зло] хватит"), ("angry", "хватит"))
check("стиль: (шёпотом) тише", styles.match_style("(шёпотом) тише"), ("whisper", "тише"))
check("стиль: (грустно) ...", styles.match_style("(грустно) ..."), ("sad", "..."))
check("стиль: (спокойно) ок", styles.match_style("(спокойно) ок"), ("calm", "ок"))
check("стиль: чужая ремарка не стиль", styles.match_style("(смех) привет"), ("", "(смех) привет"))
check("стиль: без ремарки", styles.match_style("привет"), ("", "привет"))
check("стиль: ремарка не в начале", styles.match_style("привет (радостно)"), ("", "привет (радостно)"))

# --- параметры ---
p = styles.params("happy")
check("параметры: радостно - быстрее", p["speed"] > 1.0, True)
check("параметры: радостно - выше тон", p["pitch"] > 0, True)
check("параметры: грустно - ниже тон", styles.params("sad")["pitch"] < 0, True)
check("параметры: шёпот - есть срез", styles.params("whisper")["lowpass"] > 0, True)
check("параметры: нейтрально - базовые", styles.params(""),
      {"speed": 1.0, "pitch": 0.0, "temp": 0.7, "lowpass": 0, "gain": 1.0})

# --- обработка звука ---
sr = 24000
t = np.arange(sr) / sr
tone = np.sin(2 * np.pi * 1000 * t).astype(np.float32)
hi = np.sin(2 * np.pi * 8000 * t).astype(np.float32)
check("фильтр: высокие частоты режутся",
      float(np.abs(styles.lowpass(hi, sr, 2600)).max()) < float(np.abs(hi).max()) * 0.5, True)
check("фильтр: низкие частоты остаются",
      float(np.abs(styles.lowpass(tone, sr, 2600)).max()) > float(np.abs(tone).max()) * 0.7, True)
g = styles.post(tone, sr, styles.params("angry"))
check("громкость: зло громче", float(np.abs(g).max()) > float(np.abs(tone).max()) * 1.1, True)

print()
print("Провалено: %d" % len(FAILED))
sys.exit(1 if FAILED else 0)
