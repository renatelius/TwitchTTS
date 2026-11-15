# -*- coding: utf-8 -*-
"""Контрольный набор аудио: одинаковые тексты, голос и настройки.

Нужен, чтобы после любой оптимизации можно было сравнить звук «до» и «после»:
сэмплы XTTS стохастичны, побайтовое сравнение бессмысленно - сравнивают
на слух (слепой A/B) и по автоматическим метрикам из манифеста.

    python tools/quality_baseline.py --out data/quality/baseline
    python tools/quality_baseline.py --out data/quality/after-fp16
"""
import argparse
import json
import os
import struct
import sys
import urllib.request
import wave
from pathlib import Path

BASE = os.environ.get("TTS_BASE", "http://127.0.0.1:8765")

# набор покрывает то, что ломается чаще всего: цифры, латиница, длинная фраза,
# вопрос, восклицание, эмоция и стиль речи
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


def api(path, payload=None):
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(BASE.rstrip("/") + path, data=data,
                                 headers={"Content-Type": "application/json"},
                                 method="POST" if payload is not None else "GET")
    with urllib.request.urlopen(req, timeout=600) as r:
        return json.loads(r.read().decode("utf-8"))


def fetch(url):
    with urllib.request.urlopen(BASE.rstrip("/") + url, timeout=600) as r:
        return r.read()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True, help="папка для записи (например data/quality/baseline)")
    ap.add_argument("--voice", default="", help="id голоса (пусто - голос по умолчанию)")
    args = ap.parse_args()

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    state = api("/api/state")
    settings = state.get("settings") or {}
    manifest = {
        "base": BASE,
        "voice": args.voice,
        "settings": {k: settings.get(k) for k in
                     ("speed", "volume", "language", "style", "emotes", "default_voice")},
        "samples": [],
    }

    for name, text in TEXTS:
        payload = {"text": text}
        if args.voice:
            payload["voice"] = args.voice
        res = api("/api/announce", payload)
        if not res.get("ok"):
            print("  %-16s ОШИБКА: %s" % (name, res.get("error")))
            continue
        wav = fetch(res["url"])
        path = out / ("%02d.wav" % (len(manifest["samples"]) + 1))
        path.write_bytes(wav)
        with wave.open(str(path), "rb") as w:
            dur = w.getnframes() / float(w.getframerate())
        manifest["samples"].append({
            "file": path.name, "case": name, "text": text,
            "duration": round(dur, 2), "voice": res.get("voice", ""),
        })
        print("  %-16s %5.2f с  %s" % (name, dur, path.name))

    (out / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print("контрольный набор: %s (%d образцов)" % (out, len(manifest["samples"])))
    return 0


if __name__ == "__main__":
    sys.exit(main())
