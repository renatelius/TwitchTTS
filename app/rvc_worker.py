# -*- coding: utf-8 -*-
"""Запускается Python-ом из папки Applio (не из нашего venv!) и переозвучивает wav RVC-моделью.
Общение с основной программой: строки JSON через stdin/stdout."""
import inspect
import json
import os
import sys
import traceback

MARK = "@@RVC@@"

# Наши ответы идут в настоящий stdout, а всё, что печатает Applio, уводим в stderr (в журнал)
_real = os.fdopen(os.dup(1), "w", encoding="utf-8")
os.dup2(2, 1)
sys.stdout = sys.stderr


def send(obj):
    _real.write(MARK + json.dumps(obj) + "\n")
    _real.flush()


def duration_of(path):
    try:
        import soundfile
        info = soundfile.info(path)
        return info.frames / float(info.samplerate)
    except Exception:
        import wave
        with wave.open(path, "rb") as w:
            return w.getnframes() / float(w.getframerate())


def main():
    applio = sys.argv[1]
    sys.argv = sys.argv[:1]
    os.chdir(applio)
    sys.path.insert(0, applio)
    try:
        # Докачиваем служебные модели Applio (если их ещё нет) - то же, что он делает при первом запуске
        from rvc.lib.tools import prerequisites_download as pd
        fn = getattr(pd, "prequisites_download_pipeline", None) or getattr(pd, "prerequisites_download_pipeline")
        args = {n: ("pretrained" not in n) for n in inspect.signature(fn).parameters}
        fn(**args)
    except Exception:
        traceback.print_exc()
    try:
        from rvc.infer.infer import VoiceConverter
        vc = VoiceConverter()
        sig = inspect.signature(vc.convert_audio).parameters
        any_kw = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.values())
        device = ""
        try:
            import torch
            device = torch.cuda.get_device_name(0) if torch.cuda.is_available() else "процессор"
        except Exception:
            pass
        send({"ready": True, "device": device})
    except Exception:
        send({"ready": False, "error": traceback.format_exc()[-1500:]})
        return 1

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        req = json.loads(line)
        try:
            if os.path.exists(req["output"]):
                os.remove(req["output"])
            kw = {
                "audio_input_path": req["input"],
                "audio_output_path": req["output"],
                "model_path": req["model"],
                "index_path": req.get("index") or "",
                "pitch": int(req.get("pitch", 0)),
                "f0_method": "rmvpe",
                "index_rate": float(req.get("index_rate", 0.5)),
                "volume_envelope": 1.0,
                "rms_mix_rate": 1.0,
                "protect": 0.33,
                "hop_length": 128,
                "split_audio": False,
                "f0_autotune": False,
                "f0_autotune_strength": 1.0,
                "filter_radius": 3,
                "embedder_model": "contentvec",
                "embedder_model_custom": None,
                "clean_audio": False,
                "clean_strength": 0.5,
                "export_format": "WAV",
                "post_process": False,
                "resample_sr": 0,
                "sid": 0,
            }
            if not any_kw:
                kw = {k: v for k, v in kw.items() if k in sig}
            vc.convert_audio(**kw)
            if not os.path.exists(req["output"]) or os.path.getsize(req["output"]) < 100:
                raise RuntimeError("Applio не создал выходной файл (подробности в data/rvc_log.txt)")
            send({"id": req["id"], "ok": True, "duration": duration_of(req["output"])})
        except Exception as e:
            traceback.print_exc()
            send({"id": req.get("id"), "ok": False, "error": "%s: %s" % (type(e).__name__, e)})
    return 0


if __name__ == "__main__":
    sys.exit(main())
