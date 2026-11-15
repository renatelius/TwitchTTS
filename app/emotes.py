# -*- coding: utf-8 -*-
"""Эмоции в сообщениях: (смех), *вздыхает*, [кашель] -> настоящий звук вместо чтения слова.

Как это работает:

* textproc.clean_text() находит ремарки в скобках/звёздочках и заменяет их
  метками вида {laugh};
* textproc.segments() превращает метки в куски ("emote", "laugh");
* дальше всё зависит от движка голоса:
    - CosyVoice 3 умеет смеяться и вздыхать сам: метка превращается в его
      тег ([laughter], [sigh], ...) и звучит голосом персонажа;
    - XTTS и RVC таких тегов не знают, поэтому звук эмоции либо берётся из
      папки sounds/<эмоция>/ (своя запись), либо генерируется тем же голосом:
      программа синтезирует короткую вокализацию («фх…», «ха-ха-ха…») отдельным
      куском и вклеивает её в речь. Слово («вдох», «смех») не читается никогда.

Своя запись из sounds/ всегда важнее сгенерированной: она звучит как настоящий
смех или вздох, а не как озвучка вокализации.
"""
import logging
import random
import threading
from pathlib import Path

import numpy as np

from emote_text import (  # noqa: E402  (разбор ремарок живёт в отдельном модуле,
    BRACKET_RE, EMOTES, MARK_RE, MAX_PER_MESSAGE, cosy_text, mark,
    match_emote, split_emotes, vocal_text)
from textkeys import norm_key  # noqa: E402

log = logging.getLogger("emotes")

CLIP_MAX_SEC = 6.0       # длиннее записи из sounds/ обрезаются
CLIP_PEAK = 0.7          # громкость вклеенной записи (пик)
AUDIO_EXT = (".wav", ".mp3", ".ogg", ".flac", ".m4a", ".opus", ".webm", ".mp4")

class EmoteSounds:
    """Записи эмоций из папки sounds/<эмоция>/ - для движков без собственных тегов."""

    def __init__(self, root, sample_rate, decode):
        self.root = Path(root)
        self.sr = int(sample_rate)
        self.decode = decode          # функция: путь -> (моно float32, частота)
        self._cache = {}              # путь -> (mtime, samples)
        self._last = {}               # ключ -> последняя сыгранная запись
        self._lock = threading.Lock()

    def prepare_folders(self):
        """Создаёт папки под записи и подсказку, что в них класть."""
        try:
            for key in EMOTES:
                (self.root / key).mkdir(parents=True, exist_ok=True)
            note = self.root / "ПРОЧТИ.txt"
            if not note.exists():
                lines = ["Записи эмоций для озвучки чата.", "",
                         "Положите в папку эмоции один или несколько коротких файлов (до %d секунд):" % CLIP_MAX_SEC,
                         "wav, mp3, ogg, flac, m4a. Если файлов несколько - каждый раз берётся случайный.",
                         "Новые файлы подхватываются сразу, перезапуск не нужен.", "",
                         "Если файла нет - звук эмоции генерируется тем же голосом, которым читается",
                         "сообщение (короткая вокализация: «фх…», «ха-ха-ха…»). Своя запись всегда",
                         "звучит натуральнее, поэтому если нужен настоящий смех или вздох -",
                         "положите файл сюда.", "",
                         "Папка -> что пишет зритель:"]
                for key, spec in EMOTES.items():
                    lines.append("  %-7s -> (%s)" % (key, spec["title"]))
                lines += ["",
                          "Свой звук для конкретного голоса: вложенная папка с именем голоса,",
                          "например sounds/laugh/Геральт/смех1.wav.", "",
                          "Голоса на CosyVoice 3 смеются и вздыхают сами, своим тембром - записи",
                          "им нужны только для эмоций, которых модель не умеет (cry, scream, yawn, gasp)."]
                note.write_text("\n".join(lines) + "\n", encoding="utf-8")
        except OSError as e:
            log.warning("не удалось подготовить папку sounds: %s", e)

    def _files(self, folder):
        try:
            return sorted(p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in AUDIO_EXT)
        except OSError:
            return []

    def files(self, key, voice=None):
        """Записи эмоции: сначала из папки голоса, потом общие."""
        base = self.root / key
        if voice:
            names = {norm_key(n) for n in [voice.get("name", "")] + list(voice.get("aliases") or [])}
            names.discard("")
            try:
                for d in sorted(p for p in base.iterdir() if p.is_dir()):
                    if norm_key(d.name) in names:
                        found = self._files(d)
                        if found:
                            return found
            except OSError:
                pass
        return self._files(base)

    def summary(self):
        """Сколько записей лежит в каждой папке - для интерфейса."""
        out = {}
        for key, spec in EMOTES.items():
            n = len(self._files(self.root / key))
            try:
                n += sum(len(self._files(d)) for d in (self.root / key).iterdir() if d.is_dir())
            except OSError:
                pass
            out[key] = {"title": spec["title"], "files": n, "cosy": bool(spec["cosy"])}
        return out

    def _load(self, path):
        mtime = path.stat().st_mtime
        with self._lock:
            hit = self._cache.get(str(path))
            if hit and hit[0] == mtime:
                return hit[1]
        samples, sr = self.decode(path)
        x = np.asarray(samples, dtype=np.float32).reshape(-1)
        if sr != self.sr and len(x) > 1:
            n = max(1, int(round(len(x) * self.sr / float(sr))))
            x = np.interp(np.linspace(0, len(x) - 1, n), np.arange(len(x)), x).astype(np.float32)
        x = _trim_silence(x, self.sr)[:int(CLIP_MAX_SEC * self.sr)]
        if len(x) < int(0.05 * self.sr):
            raise ValueError("запись пустая")
        peak = float(np.abs(x).max()) + 1e-9
        x = x * (CLIP_PEAK / peak)
        fade = min(int(0.01 * self.sr), len(x) // 2)
        if fade > 1:
            x[:fade] *= np.linspace(0, 1, fade, dtype=np.float32)
            x[-fade:] *= np.linspace(1, 0, fade, dtype=np.float32)
        with self._lock:
            self._cache[str(path)] = (mtime, x)
        return x

    def clip(self, key, voice=None):
        """Случайная запись эмоции (не та же, что в прошлый раз) или None."""
        files = self.files(key, voice)
        if len(files) > 1:
            files = [f for f in files if str(f) != self._last.get(key)] or files
        random.shuffle(files)
        for path in files:
            try:
                samples = self._load(path)
            except Exception as e:
                log.warning("запись %s не читается: %s", path, e)
                continue
            self._last[key] = str(path)
            return samples
        return None

    def resolve(self, segs, voice=None, mode="auto"):
        """Заменяет куски ("emote", ключ) на ("audio", запись), если запись есть.

        Если записи нет - кусок остаётся ("emote", ключ): звук тогда генерирует
        голос (см. main.emote_voice_audio). Результат понимает engine.synthesize()."""
        out = []
        for kind, val in segs:
            if kind != "emote":
                out.append((kind, val))
                continue
            samples = self.clip(val, voice)
            if samples is not None:
                out.append(("audio", samples))
            else:
                out.append(("emote", val))     # звук сгенерирует голос
        return out


def _trim_silence(x, sr, floor=0.02):
    """Срезает тишину по краям записи."""
    if not len(x):
        return x
    level = np.abs(x)
    loud = np.nonzero(level > max(floor * float(level.max()), 1e-4))[0]
    if not len(loud):
        return x[:0]
    pad = int(0.03 * sr)
    return x[max(0, loud[0] - pad):loud[-1] + pad + 1]
