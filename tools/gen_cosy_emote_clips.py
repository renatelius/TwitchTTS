
"""Клипы эмоций для голосов CosyVoice 3.

CosyVoice 3 умеет смеяться, вздыхать, кашлять и т.п. своими тегами
([laughter], [sigh], ...), поэтому для этих эмоций записи не нужны.
Но у модели нет тегов для плача, крика, зевка и аха - там нужен файл.

Клип делается тем же голосом: сервер синтезирует вокализацию
(«у-у-у-у…») голосом CosyVoice, а мы сохраняем результат в
sounds/<эмоция>/<голос>/. Дальше приложение берёт файл вместо
повторной генерации.

Запуск: python tools/gen_cosy_emote_clips.py [--base http://127.0.0.1:8765]
"""
import argparse
import io
import json
import os
import sys
import urllib.request
import wave

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "app"))

from emotes import EMOTES  # noqa: E402

# эмоции, которых CosyVoice 3 не умеет (в EMOTES у них пустой тег cosy)
TAGLESS = [k for k, spec in EMOTES.items() if not spec.get("cosy")]


def post(base, path, obj, timeout=300):
    req = urllib.request.Request(base + path, data=json.dumps(obj).encode("utf-8"),
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def get(base, path, timeout=300):
    with urllib.request.urlopen(base + path, timeout=timeout) as r:
        return r.read()


def backup_own_tagged(root, name):
    """Клипы XTTS для эмоций, которые CosyVoice 3 произносит сам, - в архив.

    Если голос перевели на CosyVoice, старые XTTS-клипы звучат чужим тембром.
    Такие файлы убираем в data/emote_backup/<голос>/<эмоция>/, чтобы модель
    читала эти эмоции своими тегами ([laughter], [sigh]...).
    """
    moved = 0
    for key, spec in EMOTES.items():
        if not spec.get("cosy"):
            continue
        folder = os.path.join(root, "sounds", key, name)
        if not os.path.isdir(folder):
            continue
        dest = os.path.join(root, "data", "emote_backup", name, key)
        os.makedirs(dest, exist_ok=True)
        for f in os.listdir(folder):
            src = os.path.join(folder, f)
            if not os.path.isfile(src):
                continue
            os.replace(src, os.path.join(dest, f))
            moved += 1
        if not os.listdir(folder):
            os.rmdir(folder)
    return moved


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8765")
    args = ap.parse_args()
    base = args.base.rstrip("/")

    voices = json.loads(get(base, "/api/voices").decode("utf-8"))["voices"]
    cosy = [v for v in voices if v.get("engine") == "cosy" and v.get("enabled")]
    if not cosy:
        print("CosyVoice-голосов нет")
        return 1

    for v in cosy:
        name = v["name"]
        moved = backup_own_tagged(ROOT, name)
        if moved:
            print("[%s] XTTS-клипы в архив: %d файлов" % (name, moved), flush=True)
        for key in TAGLESS:
            title = EMOTES[key]["title"]
            try:
                res = post(base, "/api/preview", {"text": "(%s)" % title, "voice": v["id"]})
            except Exception as e:
                print("[%s] %s ошибка запроса: %s" % (name, key, e))
                continue
            if not res.get("ok"):
                print("[%s] %s ошибка: %s" % (name, key, res))
                continue
            try:
                data = get(base, res["url"])
            except Exception as e:
                print("[%s] %s ошибка загрузки: %s" % (name, key, e))
                continue
            with wave.open(io.BytesIO(data), "rb") as w:
                sr, ch, n = w.getframerate(), w.getnchannels(), w.getnframes()
                dur = n / float(sr)
            if dur < 0.25:
                print("[%s] %s пропущен (слишком короткий: %.2fs)" % (name, key, dur))
                continue
            folder = os.path.join(ROOT, "sounds", key, name)
            os.makedirs(folder, exist_ok=True)
            out = os.path.join(folder, "%s_1.wav" % key)
            with open(out, "wb") as f:
                f.write(data)
            print("[%s] %s -> %s (%.2fs, %d Гц, %d кан.)" % (name, key, out, dur, sr, ch))
    return 0


if __name__ == "__main__":
    sys.exit(main())
