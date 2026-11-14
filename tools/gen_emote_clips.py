# -*- coding: utf-8 -*-
"""Генерация эмоций голосами пользователя -> sounds/<эмоция>/<голос>/*.wav

Зачем это нужно
---------------
Приложение уже умеет озвучивать эмоцию вокализацией тем же голосом, которым
читается сообщение (app/main.py: emote_voice_audio). Но такой звук каждый раз
получается разный: то смех оканчивается на полуслове, то вздох звучит как
вздох. Здесь мы один раз генерируем набор клипов КАЖДЫМ голосом, отбираем
лучшие по акустическим признакам эмоции и кладём в sounds/ - приложение
подхватывает их сразу (перезапуск не нужен) и берёт случайный, чтобы не
повторяться.

Почему не чужие записи
-----------------------
Эмоциональные датасеты (RAVDESS - CC BY-NC-SA, CREMA-D, ESD) - это голоса
других людей под некоммерческой лицензией. Класть их в проект нельзя, и в
чужом тембре смех внутри клона звучит инородно. Поэтому все клипы здесь
рендерятся голосами самого пользователя.

Критерии отбора (акустика эмоций)
---------------------------------
* смех - несколько отдельных выдохов (burst'ы) с паузами < 10 мс между ними
  (The Phonetics of Laughter, Speech Prosody 2007);
* вздох - ровный гласный со спадом энергии к концу;
* дыхание - шум (низкий центр спектра, много нулевых пересечений);
* шипение - шум с высоким центром спектра;
* кашель, цоканье, чмок - короткая резкая атака;
* зевок - длинный (1-4 с) с падающим основным тоном;
* крик - высокий основной тон и большая энергия;
* плач - дрожащий основной тон.

Запуск
------
    python tools/gen_emote_clips.py                 # все голоса, все эмоции
    python tools/gen_emote_clips.py --voice дудя    # один голос
    python tools/gen_emote_clips.py --limit 2       # по 2 клипа на эмоцию
"""
import argparse
import json
import sys
import time
from pathlib import Path

APP = Path(__file__).resolve().parent.parent / "app"
sys.path.insert(0, str(APP))
ROOT = APP.parent

import numpy as np

import emotes as em
from engine import OUT_SR, XttsEngine, shift_pitch, write_wav
from voices import VoiceStore

SOUNDS = ROOT / "sounds"

# Варианты вокализации: (текст, температура, скорость, сдвиг тона в полутонах)
VARIANTS = [
    (None, 0.70, 1.00, 0.0),
    (None, 0.50, 0.95, 0.0),
    (None, 0.85, 1.05, 0.0),
    (None, 0.65, 1.00, 1.5),
    (None, 0.60, 1.20, 0.0),
    (None, 0.80, 0.90, -1.0),
    (None, 0.55, 1.10, 2.0),
]

# Правила отбора: dur - допустимая длительность, c - признаки
RULES = {
    "laugh":  {"dur": (0.35, 2.60), "bursts": 2},
    "sigh":   {"dur": (0.50, 2.60), "decay": True},
    "breath": {"dur": (0.25, 1.60), "noisy": True, "centroid_max": 1800},
    "cough":  {"dur": (0.20, 1.60), "attack": True},
    "hmm":    {"dur": (0.25, 1.80), "voiced": True, "f0_max": 280},
    "tsk":    {"dur": (0.08, 0.70), "attack": True},
    "smack":  {"dur": (0.08, 0.70), "attack": True},
    "hiss":   {"dur": (0.25, 1.80), "noisy": True, "centroid_min": 2800},
    "cry":    {"dur": (0.70, 3.20), "wobble": True},
    "scream": {"dur": (0.40, 2.60), "loud": True, "f0_min": 250},
    "yawn":   {"dur": (1.00, 4.00), "falling": True},
    "gasp":   {"dur": (0.15, 1.20), "attack": True, "loud": True},
}


# ---------------------------------------------------------------- акустика
def _frames(x, sr, hop):
    n = int(sr * hop)
    m = len(x) // n
    if m < 4:
        return None
    return x[:m * n].reshape(m, n)


def rms_db(x, sr, hop=0.02):
    fr = _frames(x, sr, hop)
    if fr is None:
        return None
    e = np.sqrt((fr ** 2).mean(axis=1) + 1e-12)
    return 20 * np.log10(e)


def zcr(x):
    if len(x) < 2:
        return 0.0
    return float(np.mean(np.abs(np.diff(np.sign(x))) > 0))


def centroid(x, sr):
    fr = _frames(x, sr, 0.02)
    if fr is None:
        return 0.0
    win = np.hanning(fr.shape[1])[None, :]
    spec = np.abs(np.fft.rfft(fr * win, axis=1)) ** 2
    freqs = np.fft.rfftfreq(fr.shape[1], 1.0 / sr)
    tot = spec.sum() + 1e-12
    return float((spec * freqs[None, :]).sum() / tot)


def f0_track(x, sr, hop=0.04, fmin=70, fmax=450):
    fr = _frames(x, sr, hop)
    if fr is None:
        return np.zeros(0)
    out = []
    lo = max(1, int(sr / fmax))
    hi = min(int(sr / fmin), fr.shape[1] - 2)
    for seg in fr:
        seg = seg - seg.mean()
        if len(seg) < 8:
            out.append(0.0)
            continue
        ac = np.correlate(seg, seg, "full")[len(seg) - 1:]
        if hi <= lo:
            out.append(0.0)
            continue
        lag = lo + int(np.argmax(ac[lo:hi]))
        if ac[lag] <= 1e-6:
            out.append(0.0)
            continue
        out.append(sr / float(lag))
    return np.asarray(out, dtype=np.float32)


def count_bursts(edb, min_prom=5.0):
    """Число отдельных выдохов: пики в огибающей с перепадом >= min_prom дБ."""
    if edb is None or len(edb) < 3:
        return 0
    peaks = 0
    for i in range(1, len(edb) - 1):
        if edb[i] >= edb[i - 1] and edb[i] > edb[i + 1]:
            lo = max(0, i - 8)
            hi = min(len(edb), i + 9)
            if edb[i] - edb[lo:hi].min() >= min_prom:
                peaks += 1
    return peaks


def score_emote(key, x, sr):
    """Оценка клипа: чем больше, тем лучше. None - клип непригоден."""
    rule = RULES.get(key, {})
    dur = len(x) / float(sr)
    if dur < 0.05:
        return None
    lo, hi = rule.get("dur", (0.05, 8.0))

    edb = rms_db(x, sr)
    if edb is None:
        return None
    peak = float(edb.max())
    if peak < -45:
        return None
    sc = 0.0
    mid = (lo + hi) / 2.0
    sc += 2.0 - 2.0 * min(1.0, abs(dur - mid) / max(0.01, (hi - lo) / 2.0))
    # сильное отклонение по длине - штраф, а не отказ: лучше слабый клип,
    # чем вовсе ничего (в приложении есть запасной вариант - генерация голосом)
    if dur < lo:
        sc -= 3.0 * min(1.0, (lo - dur) / max(0.05, lo))
    elif dur > hi:
        sc -= 3.0 * min(1.0, (dur - hi) / max(0.05, hi))

    z = zcr(x)
    cen = centroid(x, sr)
    f0 = f0_track(x, sr)
    voiced = f0[f0 > 0]
    f0_med = float(np.median(voiced)) if len(voiced) else 0.0
    bursts = count_bursts(edb)

    if rule.get("bursts"):
        sc += 2.0 if bursts >= rule["bursts"] else -3.0
    if rule.get("decay"):
        n = len(edb)
        a = float(edb[:max(1, n // 3)].mean())
        b = float(edb[-max(1, n // 3):].mean())
        sc += 1.5 if b < a - 3.0 else -1.0
    if rule.get("noisy"):
        sc += 1.5 if z > 0.08 else -1.5
    if rule.get("centroid_max"):
        sc += 1.0 if cen < rule["centroid_max"] else -1.0
    if rule.get("centroid_min"):
        sc += 1.0 if cen > rule["centroid_min"] else -1.0
    if rule.get("attack"):
        jump = float(np.max(np.diff(edb))) if len(edb) > 1 else 0.0
        sc += 1.5 if jump >= 8.0 else -1.0
    if rule.get("voiced"):
        sc += 1.5 if (f0_med > 0 and z < 0.09) else -1.5
    if rule.get("f0_max"):
        sc += 1.0 if 0 < f0_med < rule["f0_max"] else -1.0
    if rule.get("f0_min"):
        sc += 2.0 if f0_med >= rule["f0_min"] else -2.0
    if rule.get("wobble"):
        if len(voiced) >= 4:
            wob = float(np.std(voiced) / max(1.0, np.mean(voiced)))
            sc += 1.5 if wob >= 0.04 else -1.0
        else:
            sc -= 1.0
    if rule.get("falling"):
        if len(voiced) >= 4:
            sc += 1.5 if float(np.mean(voiced[-2:])) < float(np.mean(voiced[:2])) * 0.9 else -1.0
        else:
            sc -= 1.0
    if rule.get("loud"):
        sc += 1.0 if peak > -8.0 else -1.0
    return sc


# ------------------------------------------------------------- генерация
def make_variants(engine, latents, text, pitch, n):
    out = []
    for over, temp, speed, extra in VARIANTS[:max(1, n)]:
        t = text if over is None else over
        wav = engine._infer("ru", t, latents, speed, temp)
        if wav is None:
            continue
        wav = np.asarray(wav, dtype=np.float32).reshape(-1)
        if len(wav) < int(0.05 * OUT_SR):
            continue
        wav = shift_pitch(wav, OUT_SR, float(pitch or 0) + float(extra or 0))
        out.append(wav)
    return out


def save_clip(path, x, sr):
    peak = float(np.abs(x).max()) + 1e-9
    y = (x / peak * 0.9).astype(np.float32)
    write_wav(path, y, sr)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--voice", default="",
                    help="только эти голоса: имя или часть имени, можно через запятую")
    ap.add_argument("--limit", type=int, default=2, help="сколько клипов оставить на эмоцию")
    ap.add_argument("--variants", type=int, default=4, help="сколько вариантов генерировать")
    ap.add_argument("--min-score", type=float, default=0.0,
                    help="ниже этой оценки клип не сохраняется (останется генерация голосом)")
    ap.add_argument("--report", default=str(ROOT / "data" / "emote_clips_report.json"))
    args = ap.parse_args()

    settings = {}
    sp = ROOT / "data" / "settings.json"
    if sp.exists():
        try:
            settings = json.loads(sp.read_text(encoding="utf-8-sig"))
        except Exception:
            settings = {}

    print("загружаю XTTS...", flush=True)
    engine = XttsEngine(license_ok=(ROOT / "data" / "license_ok.txt").exists())
    engine.load()
    if engine.status != "ready":
        print("движок не готов:", engine.status)
        return 1
    store = VoiceStore(ROOT / "voices", engine, lambda: settings.get("disabled_voices", []))
    voices = [v for v in store.list() if v.get("enabled")]
    if args.voice:
        keys = [k.strip().lower() for k in args.voice.split(",") if k.strip()]
        voices = [v for v in voices if any(k in v["name"].lower() for k in keys)]
    if not voices:
        print("голоса не найдены")
        return 1

    report = []
    t0 = time.time()
    for v in voices:
        name = v["name"]
        engine_name = v.get("engine", "xtts")
        # у CosyVoice 3 свои теги эмоций: смех, вздох, кашель и т.п. модель
        # произносит сама. Клипы ему не нужны (и пока не подхватываются).
        if engine_name == "cosy":
            print("[%s] CosyVoice 3 - эмоции произносит сама, пропускаю" % name, flush=True)
            continue
        try:
            latents = store.latents(v["id"])
        except Exception as e:
            print("[%s] не удалось взять слепок: %s" % (name, e), flush=True)
            continue
        pitch = store.effective_pitch(v)
        for key in em.EMOTES:
            text = em.vocal_text(key, "ru")
            if not text:
                continue
            variants = make_variants(engine, latents, text, pitch, args.variants)
            scored = []
            for i, w in enumerate(variants):
                sc = score_emote(key, w, OUT_SR)
                if sc is None:
                    continue
                scored.append((sc, i, w))
            scored.sort(key=lambda t: -t[0])
            keep = scored[:max(1, args.limit)]
            if not keep:
                print("[%s] %-7s нет пригодных вариантов" % (name, key), flush=True)
                report.append({"voice": name, "emote": key, "ok": False})
                continue
            if keep[0][0] < args.min_score:
                # качество ниже порога - оставляем генерацию голосом в приложении
                print("[%s] %-7s пропущен (оценка %.2f < %.2f) - останется генерация голосом"
                      % (name, key, keep[0][0], args.min_score), flush=True)
                report.append({"voice": name, "emote": key, "ok": False,
                               "score": round(keep[0][0], 2), "reason": "ниже порога"})
                continue
            folder = SOUNDS / key / name
            folder.mkdir(parents=True, exist_ok=True)
            for old in folder.glob(key + "_*.wav"):
                old.unlink()
            saved = []
            for n, (sc, idx, w) in enumerate(keep, 1):
                path = folder / ("%s_%d.wav" % (key, n))
                save_clip(path, w, OUT_SR)
                saved.append({"file": path.name, "score": round(sc, 2),
                              "dur": round(len(w) / float(OUT_SR), 2)})
            print("[%s] %-7s -> %s (%s)" % (
                name, key, ", ".join(s["file"] for s in saved),
                ", ".join(str(s["dur"]) + "s/" + str(s["score"]) for s in saved)), flush=True)
            report.append({"voice": name, "emote": key, "ok": True,
                           "score": round(keep[0][0], 2), "files": saved})

    rp = Path(args.report)
    rp.parent.mkdir(parents=True, exist_ok=True)
    rp.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    ok = sum(1 for r in report if r.get("ok"))
    print("готово: %d/%d, времени %.1f с, отчёт %s" % (ok, len(report), time.time() - t0, rp))
    return 0


if __name__ == "__main__":
    sys.exit(main())
