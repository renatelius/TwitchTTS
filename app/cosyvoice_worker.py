# -*- coding: utf-8 -*-
"""Воркер CosyVoice 3.

Работает внутри отдельного окружения CosyVoice (со своим torch), общается с
программой построчно через stdin/stdout в формате JSON. Ответы помечены
префиксом @@CV@@, остальные строки (логи модели) игнорируются.
"""
import json
import os
import re
import sys

# CosyVoice 3 требует маркер <|endofprompt|> в тексте образца
# (см. cosyvoice/llm/llm.py:479). Без него модель не работает.
PREFIX = "You are a helpful assistant.<|endofprompt|>"
TAG = "@@CV@@"


def send(obj):
    sys.stdout.write(TAG + json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


# Теги эмоций CosyVoice 3 (cosyvoice/tokenizer/tokenizer.py, CosyVoice3Tokenizer)
EMOTE_TAG_RE = re.compile(r"\[(?:laughter|sigh|breath|quick_breath|cough|mn|clucking|lipsmack|hissing)\]")


def tags_survive(model, text):
    """Проверяет, что нормализация текста не ломает теги эмоций.

    Нормализатор (wetext) может выкинуть или «прочитать» квадратные скобки - тогда
    вместо смеха прозвучит слово. В этом случае нормализацию для фразы отключаем:
    CosyVoice 3 читает числа и символы и без неё.
    """
    tags = EMOTE_TAG_RE.findall(text)
    if not tags:
        return True
    try:
        norm = model.frontend.text_normalize(text, split=False, text_frontend=True)
    except Exception:
        return False
    if not isinstance(norm, str):
        return False
    return EMOTE_TAG_RE.findall(norm) == tags


def load_audio(path, target_sr=16000):
    """Читает wav и приводит к моно нужной частоты (без внешнего ffmpeg)."""
    import numpy as np
    import soundfile as sf
    import torch
    import torchaudio
    data, sr = sf.read(str(path), dtype="float32", always_2d=True)
    speech = torch.from_numpy(np.asarray(data, dtype=np.float32)).T.contiguous()
    speech = speech.mean(dim=0, keepdim=True)
    if sr != target_sr:
        speech = torchaudio.transforms.Resample(orig_freq=sr, new_freq=target_sr)(speech)
    return speech


def main():
    if len(sys.argv) < 4:
        send({"ready": False, "error": "ожидались: папка CosyVoice, папка модели, папка кэша whisper"})
        return 2
    repo, model_dir, whisper_dir = sys.argv[1], sys.argv[2], sys.argv[3]
    sys.path.insert(0, os.path.join(repo, "third_party", "Matcha-TTS"))
    sys.path.insert(0, repo)
    try:
        import torch
        import soundfile as sf
        from cosyvoice.cli.cosyvoice import AutoModel
    except Exception as exc:
        send({"ready": False, "error": "%s: %s" % (type(exc).__name__, exc)})
        return 1

    device = ("Видеокарта: " + torch.cuda.get_device_name(0)) if torch.cuda.is_available() else "Процессор"
    state = {"model": None, "whisper": None}
    # Кэш образцов голоса: обработка образца (токены речи, фичи, эмбеддинг
    # говорящего) занимает около секунды, а образец для голоса один и тот же.
    # Ключ: путь и текст образца, время файла и режим нормализации текста.
    spk_cache = {}

    def get_model():
        if state["model"] is None:
            state["model"] = AutoModel(model_dir=model_dir)
        return state["model"]

    def get_whisper():
        if state["whisper"] is None:
            import whisper
            state["whisper"] = whisper.load_model("large-v3-turbo", download_root=whisper_dir)
        return state["whisper"]

    try:
        get_model()
    except Exception as exc:
        send({"ready": False, "error": "%s: %s" % (type(exc).__name__, exc)})
        return 1
    send({"ready": True, "device": device})

    for line in sys.stdin:
        line = line.strip().lstrip("\ufeff")
        if not line:
            continue
        try:
            req = json.loads(line)
        except ValueError:
            continue
        rid = req.get("id")
        cmd = req.get("cmd")
        try:
            if cmd == "synthesize":
                text = str(req.get("text") or "").strip()
                prompt_text = str(req.get("prompt_text") or "").strip()
                prompt_wav = str(req.get("prompt_wav") or "")
                out = str(req.get("output") or "")
                speed = float(req.get("speed") or 1.0)
                if not text:
                    raise ValueError("пустой текст")
                if not os.path.exists(prompt_wav):
                    raise ValueError("нет образца голоса")
                if not prompt_text:
                    raise ValueError("нет текста образца")
                m = get_model()
                extra = {}
                if req.get("tags") and not tags_survive(m, text):
                    extra["text_frontend"] = False
                # Образец готовим один раз на голос и переиспользуем: иначе
                # каждая фраза заново разбирает запись голоса (~1 с).
                try:
                    mtime = os.path.getmtime(prompt_wav)
                except OSError:
                    mtime = 0.0
                normalize = bool(extra.get("text_frontend", True))
                key = (prompt_wav, prompt_text, mtime, normalize)
                spk_id = spk_cache.get(key)
                if spk_id is None:
                    spk_id = "spk_%d" % (len(spk_cache) + 1)
                    prompt_for_cache = PREFIX + prompt_text
                    if normalize:
                        try:
                            # тот же нормализованный текст, что и при синтезе без кэша
                            prompt_for_cache = m.frontend.text_normalize(
                                prompt_for_cache, split=False, text_frontend=True)
                        except Exception:
                            pass
                    m.add_zero_shot_spk(prompt_for_cache, prompt_wav, spk_id)
                    spk_cache[key] = spk_id
                chunks = []
                for res in m.inference_zero_shot(text, PREFIX + prompt_text, prompt_wav,
                                                 zero_shot_spk_id=spk_id,
                                                 stream=False, speed=speed, **extra):
                    chunks.append(res["tts_speech"])
                speech = torch.cat(chunks, dim=1)
                data = speech.squeeze(0).cpu().numpy().astype("float32")
                sf.write(out, data, m.sample_rate)
                send({"id": rid, "ok": True, "duration": float(speech.shape[1]) / float(m.sample_rate)})
            elif cmd == "transcribe":
                path = str(req.get("input") or "")
                if not os.path.exists(path):
                    raise ValueError("нет файла для расшифровки")
                w = get_whisper()
                speech = load_audio(path, 16000).squeeze(0).cpu().numpy()
                res = w.transcribe(speech, language="ru", fp16=False, verbose=False)
                send({"id": rid, "ok": True, "text": str(res.get("text") or "").strip()})
            else:
                send({"id": rid, "ok": False, "error": "неизвестная команда: %s" % cmd})
        except Exception as exc:
            send({"id": rid, "ok": False, "error": "%s: %s" % (type(exc).__name__, exc)})
    return 0


if __name__ == "__main__":
    sys.exit(main())
