# -*- coding: utf-8 -*-
"""Нагрузка на очередь озвучки: имитация живого чата разными голосами.

Отправляет сообщения через POST /api/say (тот же путь, что и сообщение из
чата) и слушает SSE /events, чтобы восстановить таймлайн: когда сообщение
встало в очередь, когда начало звучать и когда отыграло.

    python tools/chat_spam.py --count 20 --mode burst
    python tools/chat_spam.py --count 20 --mode chat --interval 0.7
    python tools/chat_spam.py --count 20 --voices cosy --out data/bench/spam-cosy.json

Что смотрим: порядок сохраняется ли, сколько ждёт первая реплика, сколько
накапливается очередь, успевает ли предгенерация, не было ли ошибок.
"""
import argparse
import json
import os
import statistics
import subprocess
import threading
import time
import urllib.request
from pathlib import Path

BASE = os.environ.get("TTS_BASE", "http://127.0.0.1:8765")

PHRASES = [
    "Привет, чат",
    "Как дела у всех",
    "Это тест нагрузки",
    "Озвучка работает",
    "Проверка очереди",
    "Кто ещё онлайн",
    "Всем привет",
    "Скоро уже выйду",
    "Такой вот тест",
    "Работаем дальше",
    "Озвучка не тормозит",
    "Проверяем скорость",
    "Слышно меня",
    "Давайте ещё раз",
    "Тест номер один",
    "Очередь растёт",
    "Сообщение в чат",
    "Ещё одна фраза",
    "Финальный тест",
    "Пора подводить итоги",
]


def api(path, payload=None, timeout=60):
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(BASE.rstrip("/") + path, data=data,
                                 headers={"Content-Type": "application/json"},
                                 method="POST" if payload is not None else "GET")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        body = r.read()
    return json.loads(body.decode("utf-8")) if body else {}


class Events(threading.Thread):
    """Читает SSE и складывает события с отметками времени."""

    def __init__(self):
        super().__init__(daemon=True)
        self.events = []
        self.lock = threading.Lock()
        self.stop_flag = threading.Event()

    def run(self):
        try:
            req = urllib.request.Request(BASE.rstrip("/") + "/events")
            with urllib.request.urlopen(req, timeout=600) as r:
                buf = ""
                for raw in r:
                    if self.stop_flag.is_set():
                        break
                    line = raw.decode("utf-8", "replace")
                    if not line.startswith("data: "):
                        continue
                    try:
                        ev = json.loads(line[6:])
                    except ValueError:
                        continue
                    with self.lock:
                        self.events.append((time.perf_counter(), ev))
        except Exception:
            pass

    def take(self):
        with self.lock:
            out = list(self.events)
            self.events.clear()
        return out


def gpu_sample():
    try:
        out = subprocess.run(["nvidia-smi", "--query-gpu=utilization.gpu,memory.used",
                              "--format=csv,noheader,nounits"],
                             capture_output=True, text=True, timeout=15).stdout.strip().splitlines()[0]
        u, m = [int(x.strip()) for x in out.split(",")]
        return {"util": u, "mem": m}
    except Exception:
        return {"util": 0, "mem": 0}


def wav_duration(blob):
    """Длительность wav по заголовку RIFF: частота и число байт данных.

    Формула по размеру файла не годится: XTTS и CosyVoice пишут 24 кГц,
    а RVC - 40 кГц, и одинаковая формула даёт для RVC треть меньше.
    """
    if len(blob) < 44 or blob[:4] != b"RIFF" or blob[8:12] != b"WAVE":
        return 0.0
    pos = 12
    sr = None
    bits = None
    ch = 1
    data = 0
    while pos + 8 <= len(blob):
        cid = blob[pos:pos + 4]
        size = int.from_bytes(blob[pos + 4:pos + 8], "little")
        body = blob[pos + 8:pos + 8 + size]
        if cid == b"fmt ":
            # fmt: audioFormat[0:2], channels[2:4], sampleRate[4:8], bits[14:16]
            ch = int.from_bytes(body[2:4], "little") or 1
            sr = int.from_bytes(body[4:8], "little")
            bits = int.from_bytes(body[14:16], "little")
        elif cid == b"data":
            data = min(size, len(body))
        pos += 8 + size + (size & 1)
    if not sr or not bits:
        return 0.0
    return data / float(sr * (bits // 8) * ch)


def main():
    global BASE
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default=BASE)
    ap.add_argument("--count", type=int, default=20)
    ap.add_argument("--mode", choices=["burst", "chat"], default="burst")
    ap.add_argument("--interval", type=float, default=0.7)
    ap.add_argument("--voices", default="mixed", help="mixed | cosy | xtts | rvc | id голоса")
    ap.add_argument("--out", default="")
    args = ap.parse_args()
    BASE = args.base

    voices = api("/api/voices").get("voices", [])
    by_engine = {}
    for v in voices:
        if v.get("enabled", True):
            by_engine.setdefault(v.get("engine"), []).append(v)
    if args.voices == "mixed":
        pool = []
        for eng in ("cosy", "xtts", "rvc"):
            pool.extend(by_engine.get(eng, []))
    elif args.voices in by_engine:
        pool = list(by_engine[args.voices])
    else:
        pool = [v for v in voices if v["id"] == args.voices]
    if not pool:
        print("нет подходящих голосов")
        return 1
    print("голосов в пуле: %d (%s)" % (len(pool), ", ".join(
        sorted({v.get("engine") for v in pool}))))
    for v in voices:
        if v.get("engine") == "rvc" and v.get("enabled"):
            break

    ev = Events()
    ev.start()
    time.sleep(1.0)

    t0 = time.perf_counter()
    enqueued = []
    for i in range(args.count):
        v = pool[i % len(pool)]
        text = "%s, сообщение %d" % (PHRASES[i % len(PHRASES)], i + 1)
        if args.mode == "chat" and enqueued:
            time.sleep(args.interval)
        try:
            res = api("/api/say", {"text": text, "voice": v["id"]})
        except Exception as exc:
            print("ошибка отправки:", exc)
            continue
        enqueued.append({"text": text, "voice_id": v["id"], "voice": v.get("name"),
                         "engine": v.get("engine"), "at": time.perf_counter() - t0,
                         "ok": res.get("ok")})
    print("сообщений отправлено: %d" % len(enqueued))

    # ждём, пока все отыграют
    deadline = time.perf_counter() + 600
    done_ids = set()
    speak = []
    errors = []
    gpu = []
    chat_map = {}
    queue_depth = []
    next_gpu = time.perf_counter()
    while time.perf_counter() < deadline:
        for ts, e in ev.take():
            t = e.get("type")
            if t == "speak":
                real = None
                url = e.get("url")
                if url:
                    try:
                        with urllib.request.urlopen(BASE.rstrip("/") + url, timeout=60) as rr:
                            blob = rr.read()
                        real = round(wav_duration(blob), 3)
                    except Exception:
                        real = None
                speak.append({"id": e.get("id"), "voice": e.get("voice"),
                              "voice_id": e.get("voice_id"), "duration": e.get("duration"),
                              "real_duration": real, "at": ts - t0})
            elif t == "done":
                done_ids.add(e.get("id"))
            elif t == "tts_error":
                errors.append({"id": e.get("id"), "error": e.get("error"), "at": ts - t0})
            elif t == "chat":
                chat_map[e.get("text")] = e.get("id")
            elif t == "status":
                q = e.get("queue")
                if isinstance(q, int):
                    queue_depth.append({"at": round(ts - t0, 2), "queue": q})
        if time.perf_counter() >= next_gpu:
            gpu.append(gpu_sample())
            next_gpu = time.perf_counter() + 0.5
        if len(speak) >= len(enqueued) and len(errors) == 0:
            break
        time.sleep(0.05)
    ev.stop_flag.set()

    # связываем сообщения с событиями: id приходит в событии chat
    for ts, e in list(ev.events):
        if e.get("type") == "chat":
            chat_map[e.get("text")] = e.get("id")
    speak_by_id = {s["id"]: s for s in speak}

    rows = []
    for item in enqueued:
        mid = chat_map.get(item["text"])
        s = speak_by_id.get(mid, {})
        rows.append({
            "text": item["text"], "voice": item["voice"], "engine": item["engine"],
            "enqueued_at": round(item["at"], 2),
            "speak_at": round(s.get("at", -1), 2) if s else None,
            "latency_s": round(s["at"] - item["at"], 2) if s else None,
            "audio_s": s.get("duration"),
            "real_audio_s": s.get("real_duration"),
            "delta_s": (round(s.get("real_duration") - s.get("duration"), 3)
                        if s.get("real_duration") is not None and s.get("duration") is not None else None),
            "spoke": bool(s),
        })

    lat = [r["latency_s"] for r in rows if r["latency_s"] is not None]
    gaps = []
    times = [r["speak_at"] for r in rows if r["speak_at"] is not None]
    for a, b in zip(times, times[1:]):
        gaps.append(round(b - a, 2))
    order_ok = times == sorted(times)
    overlaps = []
    for i in range(len(rows) - 1):
        a, b = rows[i], rows[i + 1]
        if a.get("real_audio_s") is None or b.get("speak_at") is None:
            continue
        end = a["speak_at"] + a["real_audio_s"]
        if b["speak_at"] < end - 0.05:
            overlaps.append({"n": i + 1, "overlap_s": round(end - b["speak_at"], 3),
                             "voice": a.get("voice"), "next": b.get("voice")})

    print()
    print("%-3s %-34s %-9s %8s %8s %8s %8s" % ("#", "текст", "движок", "в очередь", "звучит", "ждала", "длина"))
    for i, r in enumerate(rows, 1):
        print("%-3d %-34s %-9s %8.2f %8s %8s %8s" % (
            i, r["text"][:34], r["engine"], r["enqueued_at"],
            ("%.2f" % r["speak_at"]) if r["speak_at"] is not None else "-",
            ("%.2f" % r["latency_s"]) if r["latency_s"] is not None else "-",
            ("%.2f/%.2f" % (r["audio_s"], r["real_audio_s"])) if r.get("real_audio_s") is not None else "-"))
    print()
    print("озвучено: %d из %d | ошибок: %d | порядок сохранён: %s" % (
        len(speak), len(enqueued), len(errors), "да" if order_ok else "нет"))
    if lat:
        print("ожидание звука: p50 %.2f с, максимум %.2f с, среднее %.2f с" % (
            statistics.median(lat), max(lat), statistics.mean(lat)))
    if gaps:
        print("паузы между репликами: медиана %.2f с, максимум %.2f с" % (
            statistics.median(gaps), max(gaps)))
    if times:
        print("время от первой отправки до последней реплики: %.2f с" % (max(times)))
    if queue_depth:
        print("глубина очереди: максимум %d, в конце %d" % (
            max(q["queue"] for q in queue_depth), queue_depth[-1]["queue"]))
    if overlaps:
        print("НАЛОЖЕНИЯ (следующая реплика началась до конца предыдущей): %d" % len(overlaps))
        for o in overlaps:
            print("   #%d наложение %.2f с: %s -> %s" % (o["n"], o["overlap_s"], o["voice"], o["next"]))
    else:
        print("наложений нет")
    if gpu:
        print("GPU: util средняя %.0f %%, максимум %d %%, VRAM до %d МБ" % (
            statistics.mean(g["util"] for g in gpu),
            max(g["util"] for g in gpu),
            max(g["mem"] for g in gpu)))
    for e in errors:
        print("ОШИБКА:", e)

    report = {"base": BASE, "mode": args.mode, "interval": args.interval,
              "count": len(enqueued), "rows": rows, "errors": errors,
              "order_ok": order_ok, "latency": lat, "gaps": gaps, "queue_depth": queue_depth,
              "gpu": gpu}
    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print("отчёт:", out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
