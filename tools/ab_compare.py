# -*- coding: utf-8 -*-
"""Слепое A/B-сравнение наборов качества: before / after / control.

Что делает:
  1. берёт наборы качества из data/quality (каждый - manifest.json + wav);
  2. считает объективные метрики каждого файла (длительность, RMS, пик, f0,
     спектральный центроид, sha256);
  3. собирает слепой комплект: файлы переименованы в A/B/C и перемешаны
     детерминированно, ключ пишется отдельно;
  4. пишет metrics.json, blind-key.json и объективную часть отчёта.

Ключ слепого комплекта лежит рядом и не отдаётся слушателю до прослушивания.

    python tools/ab_compare.py --sets before-G after-G control-G --out data/quality/AB-2026-11-15
"""
import argparse
import hashlib
import json
import random
import shutil
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
QUALITY = ROOT / "data" / "quality"


def read_wav(path):
    """Возвращает (samples float32 numpy, sample_rate)."""
    import numpy as np
    with wave.open(str(path), "rb") as w:
        n = w.getnframes()
        sr = w.getframerate()
        ch = w.getnchannels()
        sw = w.getsampwidth()
        raw = w.readframes(n)
    if sw == 2:
        data = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    elif sw == 4:
        data = np.frombuffer(raw, dtype="<i4").astype(np.float32) / 2147483648.0
    else:
        raise ValueError("unsupported sample width %s in %s" % (sw, path))
    if ch > 1:
        data = data.reshape(-1, ch).mean(axis=1)
    return data, sr


def f0_median(y, sr, fmin=70.0, fmax=400.0):
    """Медианный основной тон по вокализованным кадрам (автокорреляция)."""
    import numpy as np
    frame = int(0.04 * sr)
    hop = int(0.01 * sr)
    if len(y) < frame:
        return None
    voiced = []
    for start in range(0, len(y) - frame, hop):
        chunk = y[start:start + frame]
        rms = float(np.sqrt(np.mean(chunk ** 2)))
        if rms < 0.01:
            continue
        chunk = chunk - chunk.mean()
        corr = np.correlate(chunk, chunk, mode="full")[frame - 1:]
        lo = int(sr / fmax)
        hi = min(int(sr / fmin), len(corr) - 1)
        if hi <= lo:
            continue
        seg = corr[lo:hi]
        k = int(np.argmax(seg))
        if seg[k] / (seg[0] + 1e-9) > 0.35:
            voiced.append(sr / (lo + k))
    if not voiced:
        return None
    return float(np.median(voiced))


def centroid(y, sr):
    import numpy as np
    frame = int(0.04 * sr)
    hop = int(0.02 * sr)
    vals = []
    for start in range(0, len(y) - frame, hop):
        chunk = y[start:start + frame]
        if float(np.sqrt(np.mean(chunk ** 2))) < 0.01:
            continue
        spec = np.abs(np.fft.rfft(chunk * np.hanning(frame)))
        freqs = np.fft.rfftfreq(frame, 1.0 / sr)
        vals.append(float((spec * freqs).sum() / (spec.sum() + 1e-9)))
    if not vals:
        return None
    return float(np.mean(vals))


def metrics(path):
    import numpy as np
    y, sr = read_wav(path)
    dur = len(y) / float(sr)
    peak = float(np.max(np.abs(y))) if len(y) else 0.0
    rms = float(np.sqrt(np.mean(y ** 2))) if len(y) else 0.0
    return {
        "file": path.name,
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "sample_rate": sr,
        "duration": round(dur, 3),
        "peak_dbfs": round(20 * np.log10(peak + 1e-9), 2),
        "rms_dbfs": round(20 * np.log10(rms + 1e-9), 2),
        "f0_median": round(f0_median(y, sr), 1) if f0_median(y, sr) else None,
        "centroid_hz": round(centroid(y, sr), 1) if centroid(y, sr) else None,
    }


def db(v):
    return "—" if v is None else ("%+.2f" % v)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sets", nargs="+", default=["before-G", "after-G", "control-G"])
    ap.add_argument("--root", default=str(QUALITY))
    ap.add_argument("--out", default=str(QUALITY / "AB"))
    ap.add_argument("--seed", type=int, default=20261115)
    args = ap.parse_args()

    root = Path(args.root)
    out = Path(args.out)
    blind = out / "blind"
    if blind.exists():
        shutil.rmtree(blind)
    blind.mkdir(parents=True, exist_ok=True)

    sets = args.sets
    manifests = {}
    for s in sets:
        mf = root / s / "manifest.json"
        if not mf.exists():
            raise SystemExit("no manifest for set %s" % s)
        manifests[s] = json.loads(mf.read_text(encoding="utf-8"))

    cases = [x["case"] for x in manifests[sets[0]]["samples"]]
    for s in sets:
        got = [x["case"] for x in manifests[s]["samples"]]
        if got != cases:
            raise SystemExit("sets disagree on cases: %s vs %s" % (sets[0], s))

    rng = random.Random(args.seed)
    all_metrics = {}
    key = {}
    for s in sets:
        for smp in manifests[s]["samples"]:
            p = root / s / smp["file"]
            m = metrics(p)
            m["set"] = s
            m["case"] = smp["case"]
            m["text"] = smp.get("text", "")
            m["manifest_duration"] = smp.get("duration")
            all_metrics["%s/%s" % (s, smp["file"])] = m

    for idx, case in enumerate(cases, start=1):
        order = list(sets)
        rng.shuffle(order)
        letters = ["A", "B", "C"]
        cdir = blind / ("%02d" % idx)
        cdir.mkdir(parents=True, exist_ok=True)
        mapping = {}
        for letter, s in zip(letters, order):
            src = root / s / manifests[s]["samples"][idx - 1]["file"]
            shutil.copyfile(src, cdir / ("%s.wav" % letter))
            mapping[letter] = s
        key["%02d" % idx] = {"case": case, "text": manifests[sets[0]]["samples"][idx - 1].get("text", ""), "map": mapping}

    (out / "blind-key.json").write_text(json.dumps(key, ensure_ascii=False, indent=2), encoding="utf-8")
    (out / "metrics.json").write_text(json.dumps(all_metrics, ensure_ascii=False, indent=2), encoding="utf-8")

    lines = []
    lines.append("# Слепое A/B: объективная часть")
    lines.append("")
    lines.append("Наборы: %s. Ключ: blind-key.json." % ", ".join(sets))
    lines.append("")
    for idx, case in enumerate(cases, start=1):
        lines.append("## %02d. %s" % (idx, case))
        lines.append("")
        lines.append("| набор | файл | хэш | длина, с | RMS, dBFS | f0, Гц | центр, Гц |")
        lines.append("|---|---|---|---|---|---|---|")
        for s in sets:
            m = all_metrics["%s/%s" % (s, manifests[s]["samples"][idx - 1]["file"])]
            lines.append("| %s | %s | %s | %.2f | %s | %s | %s |" % (
                s, m["file"], m["sha256"][:8], m["duration"], db(m["rms_dbfs"]),
                "—" if m["f0_median"] is None else "%.0f" % m["f0_median"],
                "—" if m["centroid_hz"] is None else "%.0f" % m["centroid_hz"]))
        hashes = {s: all_metrics["%s/%s" % (s, manifests[s]["samples"][idx - 1]["file"])]["sha256"] for s in sets}
        same = [a for a in hashes if hashes[a] == hashes[sets[0]]]
        if len(same) == len(sets):
            lines.append("")
            lines.append("**Все три файла побайтово идентичны.**")
        elif len(same) > 1:
            lines.append("")
            lines.append("**Побайтово идентичны:** %s." % ", ".join(same))
        lines.append("")
    (out / "report-objective.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    print("blind pack: %s" % blind)
    print("cases: %d, files: %d" % (len(cases), len(cases) * len(sets)))
    for idx, case in enumerate(cases, start=1):
        hashes = {}
        for s in sets:
            hashes[s] = all_metrics["%s/%s" % (s, manifests[s]["samples"][idx - 1]["file"])]["sha256"]
        uniq = len(set(hashes.values()))
        print("case %02d %-28s unique=%d  %s" % (idx, case[:28], uniq, "IDENTICAL" if uniq == 1 else ""))


if __name__ == "__main__":
    main()
