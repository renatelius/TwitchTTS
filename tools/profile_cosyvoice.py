# -*- coding: utf-8 -*-
"""Профиль CosyVoice 3: где уходит время на фразу и что даёт кэш образца голоса.

Запускать в окружении CosyVoice (у него свой torch):

    B:\projects\TwitchTTS\cosyvoice\env\Scripts\python.exe tools\profile_cosyvoice.py

Что меряем:
  * загрузка модели;
  * обработка образца голоса во фронтенде (повторяется на каждой фразе);
  * синтез без кэша образца (как сейчас в приложении);
  * синтез с кэшем образца (add_zero_shot_spk + zero_shot_spk_id).

Код приложения не меняется: это отдельный измерительный скрипт.
"""
import argparse
import json
import statistics
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PREFIX = "You are a helpful assistant.<|endofprompt|>"

TEXTS = [
    ("обычная реплика", "Привет, чат! Сегодня отличная погода."),
    ("вопрос", "Кто-нибудь пробовал новую версию голоса?"),
    ("цифры", "Стоимость билета выросла до двух тысяч рублей."),
    ("латиница", "Проверка латиницы: streaming, overlay, websocket."),
    ("длинная фраза", "Когда программа работает давно, важно вовремя освобождать ресурсы, "
                      "иначе видеокарта постепенно забивается и всё начинает идти медленнее."),
]


def find_cosy(cv_dir):
    d = Path(cv_dir)
    repo = None
    for cand in (d / "CosyVoice", d):
        if (cand / "cosyvoice" / "cli" / "cosyvoice.py").exists():
            repo = cand
            break
    model = None
    for cand in (d / "pretrained_models" / "Fun-CosyVoice3-0.5B", d / "pretrained_models", d):
        if (cand / "cosyvoice3.yaml").exists():
            model = cand
            break
    return repo, model


def gpu():
    try:
        out = subprocess.run(["nvidia-smi", "--query-gpu=utilization.gpu,memory.used",
                              "--format=csv,noheader,nounits"],
                             capture_output=True, text=True, timeout=15).stdout.strip().splitlines()[0]
        u, m = [int(x.strip()) for x in out.split(",")]
        return {"util": u, "mem": m}
    except Exception:
        return {"util": 0, "mem": 0}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cv-dir", default=r"B:\projects\TwitchTTS\cosyvoice")
    ap.add_argument("--voice", default="v1794589589_bd031b")
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    repo, model_dir = find_cosy(args.cv_dir)
    if repo is None or model_dir is None:
        print("не нашёл исходники или модель CosyVoice")
        return 1
    sys.path.insert(0, str(Path(repo) / "third_party" / "Matcha-TTS"))
    sys.path.insert(0, str(repo))

    import torch
    from cosyvoice.cli.cosyvoice import AutoModel

    voice_dir = ROOT / "voices" / args.voice
    meta = json.loads((voice_dir / "voice.json").read_text(encoding="utf-8"))
    prompt_wav = str(voice_dir / meta["cv_prompt"])
    prompt_text = str(meta["cv_text"])
    print("образец: %s (%.1f с), текст образца: %d символов" % (
        prompt_wav,
        __import__("wave").open(prompt_wav, "rb").getnframes() / float(
            __import__("wave").open(prompt_wav, "rb").getframerate()),
        len(prompt_text)))

    t0 = time.perf_counter()
    model = AutoModel(model_dir=str(model_dir))
    load_s = time.perf_counter() - t0
    print("загрузка модели: %.1f с" % load_s)

    report = {"load_s": round(load_s, 2), "voice": args.voice, "rows": []}

    # 1) обработка образца во фронтенде (то, что повторяется на каждой фразе)
    t0 = time.perf_counter()
    model.frontend.frontend_zero_shot("", PREFIX + prompt_text, prompt_wav,
                                      model.sample_rate, "")
    frontend_s = time.perf_counter() - t0
    print("обработка образца во фронтенде: %.3f с" % frontend_s)
    report["frontend_s"] = round(frontend_s, 3)

    # 2) синтез без кэша образца (как в приложении сейчас)
    plain = []
    for name, text in TEXTS:
        t0 = time.perf_counter()
        chunks = []
        for res in model.inference_zero_shot(text, PREFIX + prompt_text, prompt_wav,
                                             stream=False, speed=1.0):
            chunks.append(res["tts_speech"])
        dt = time.perf_counter() - t0
        speech = torch.cat(chunks, dim=1)
        dur = speech.shape[1] / float(model.sample_rate)
        plain.append((name, dt, dur))
        print("  без кэша  %-16s %6.2f с  звук %5.2f с  RTF %.2f" % (name, dt, dur, dt / dur))

    # 3) кэш образца
    t0 = time.perf_counter()
    model.add_zero_shot_spk(PREFIX + prompt_text, prompt_wav, "cached_spk")
    add_s = time.perf_counter() - t0
    print("регистрация образца в кэше: %.3f с" % add_s)
    report["add_spk_s"] = round(add_s, 3)

    cached = []
    for name, text in TEXTS:
        t0 = time.perf_counter()
        chunks = []
        for res in model.inference_zero_shot(text, PREFIX + prompt_text, prompt_wav,
                                             zero_shot_spk_id="cached_spk",
                                             stream=False, speed=1.0):
            chunks.append(res["tts_speech"])
        dt = time.perf_counter() - t0
        speech = torch.cat(chunks, dim=1)
        dur = speech.shape[1] / float(model.sample_rate)
        cached.append((name, dt, dur))
        print("  с кэшем  %-16s %6.2f с  звук %5.2f с  RTF %.2f" % (name, dt, dur, dt / dur))

    p_plain = [d for _, d, _ in plain]
    p_cached = [d for _, d, _ in cached]
    mp, mc = statistics.mean(p_plain), statistics.mean(p_cached)
    print()
    print("без кэша: среднее %.3f с | с кэшем: среднее %.3f с | выигрыш %.1f%% (%.3f с на фразу)" % (
        mp, mc, (mp / max(mc, 0.001) - 1) * 100, mp - mc))
    report["plain_mean_s"] = round(mp, 3)
    report["cached_mean_s"] = round(mc, 3)
    report["gain_pct"] = round((mp / max(mc, 0.001) - 1) * 100, 1)
    report["gpu"] = gpu()
    for name, dt, dur in plain:
        report["rows"].append({"case": name, "plain_s": round(dt, 3), "audio_s": round(dur, 2)})
    for name, dt, dur in cached:
        for r in report["rows"]:
            if r["case"] == name:
                r["cached_s"] = round(dt, 3)

    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print("отчёт:", out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
