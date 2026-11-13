# -*- coding: utf-8 -*-
"""База голосов и подготовка новых голосов из произвольных аудио/видео файлов."""
import json
import logging
import os
import shutil
import subprocess
import time
import uuid
from pathlib import Path

import numpy as np

from engine import REF_SR, write_wav
from textproc import norm_key

log = logging.getLogger("voices")

MAX_CLIPS = 12
MIN_SPEECH_SEC = 3.0


class VoiceError(Exception):
    pass


def find_ffmpeg():
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        pass
    exe = shutil.which("ffmpeg")
    if not exe:
        raise VoiceError("Не найден ffmpeg. Запустите install.bat ещё раз.")
    return exe


def decode(path):
    """Любой аудио/видео файл -> моно float32 с частотой REF_SR."""
    cmd = [find_ffmpeg(), "-v", "error", "-nostdin", "-t", "900", "-i", str(path), "-vn",
           "-ac", "1", "-ar", str(REF_SR), "-af", "highpass=f=60", "-f", "f32le", "-"]
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    p = subprocess.run(cmd, capture_output=True, creationflags=flags)
    if p.returncode != 0 or len(p.stdout) < 4:
        raise VoiceError("не удалось прочитать как аудио")
    return np.frombuffer(p.stdout[:len(p.stdout) // 4 * 4], dtype=np.float32).copy()


GAP_MIN = 0.35      # пауза длиннее этого считается границей между фразами
PIECE_MAX = 11.0    # максимальная длина куска, секунды
FADE = 0.008        # мягкий срез на границах кусков, секунды


def _frames_db(x):
    """Громкость кадрами по 20 мс в дБ."""
    frame = int(0.02 * REF_SR)
    n = len(x) // frame
    if n < 10:
        return np.zeros(0, dtype=np.float32), frame
    fr = x[:n * frame].reshape(n, frame)
    db = 20 * np.log10(np.sqrt((fr ** 2).mean(axis=1) + 1e-12))
    return db, frame


def _quiet_spans(db, min_len):
    """Тихие участки длиной не меньше min_len секунд: [(начало, конец)] в кадрах."""
    if len(db) == 0:
        return []
    thr = max(np.percentile(db, 95) - 30, -50)
    spans, i, n = [], 0, len(db)
    while i < n:
        if db[i] <= thr:
            j = i
            while j < n and db[j] <= thr:
                j += 1
            if (j - i) * 0.02 >= min_len:
                spans.append((i, j))
            i = j
        else:
            i += 1
    return spans


def _utterances(x):
    """Делит запись на фразы по длинным паузам. Короткие паузы внутри фразы остаются."""
    db, frame = _frames_db(x)
    if len(db) == 0:
        return []
    out, start = [], 0
    for a, b in _quiet_spans(db, GAP_MIN):
        seg = x[start:a * frame]
        if len(seg) >= int(0.4 * REF_SR):
            out.append(seg)
        start = b * frame
    seg = x[start:]
    if len(seg) >= int(0.4 * REF_SR):
        out.append(seg)
    return out


def _trim_edges(seg):
    """Убирает только тишину по краям фразы, внутри ничего не трогает."""
    db, frame = _frames_db(seg)
    if len(db) == 0:
        return seg
    thr = max(np.percentile(db, 95) - 30, -50)
    loud = np.where(db > thr)[0]
    if len(loud) == 0:
        return seg
    a = max(0, int(loud[0]) - 2) * frame
    b = min(len(seg), (int(loud[-1]) + 3) * frame)
    return seg[a:b]


def _split_long(seg, max_len=PIECE_MAX):
    """Режет длинную фразу по паузам, чтобы не разрывать слова."""
    limit = int(max_len * REF_SR)
    if len(seg) <= limit:
        return [seg]
    db, frame = _frames_db(seg)
    cuts = [((a + b) // 2) * frame for a, b in _quiet_spans(db, 0.12)]
    pieces, rest, offset = [], seg, 0
    while len(rest) > limit:
        cands = [c - offset for c in cuts if limit * 0.3 < (c - offset) < limit * 0.95]
        cut = min(cands, key=lambda p: abs(p - limit)) if cands else limit
        pieces.append(rest[:cut])
        rest = rest[cut:]
        offset += cut
    if len(rest) > int(0.2 * REF_SR):
        pieces.append(rest)
    return pieces


def _norm_gain(seg):
    """Одно усиление на всю фразу: перепады громкости внутри неё сохраняются."""
    rms = float(np.sqrt((seg ** 2).mean()) + 1e-9)
    peak = float(np.abs(seg).max()) + 1e-9
    return min(0.1 / rms, 0.95 / peak)


def _fade(piece, at_start=True, at_end=True):
    """Короткий срез, чтобы куски не щёлкали при склейке."""
    n = int(FADE * REF_SR)
    if n < 2 or len(piece) < 2 * n:
        return piece
    piece = piece.copy()
    if at_start:
        piece[:n] *= np.linspace(0, 1, n)
    if at_end:
        piece[-n:] *= np.linspace(1, 0, n)
    return piece


def cut_clips(x):
    """Готовит куски образца голоса.

    Короткие паузы и перепады громкости внутри фразы сохраняются: в них живёт
    интонация. Раньше паузы вырезались, а куски разных предложений склеивались
    и выравнивались по отдельности - из-за этого голос получался ровным и пустым.
    """
    clips = []
    for utt in _utterances(x):
        seg = _trim_edges(utt)
        if len(seg) < int(0.4 * REF_SR):
            continue
        pieces = _split_long(seg)
        gain = _norm_gain(seg)
        for k, piece in enumerate(pieces):
            piece = (piece * gain).astype(np.float32)
            piece = _fade(piece, at_start=(k > 0), at_end=(k < len(pieces) - 1))
            clips.append(piece)
    return clips


def prepare_clips(src_paths, out_dir, names=None):
    """Готовит образцы голоса из исходных файлов. Возвращает (пути, отчёт)."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    per_file, failed = [], []
    names = names or {}
    for src in src_paths:
        label = names.get(str(src), Path(src).name)
        try:
            clips = cut_clips(decode(src))
            if clips:
                per_file.append(clips)
            else:
                failed.append("%s: не нашлось речи" % label)
        except VoiceError as e:
            failed.append("%s: %s" % (label, e))
    # берём куски по порядку: сначала первый файл, потом второй - так сохраняется
    # естественный ход речи, а не перемешанные куски
    chosen = []
    for clips in per_file:
        for clip in clips:
            if len(chosen) >= MAX_CLIPS:
                break
            chosen.append(clip)
        if len(chosen) >= MAX_CLIPS:
            break
    total = sum(len(c) for c in chosen) / REF_SR
    if total < MIN_SPEECH_SEC:
        msg = "Слишком мало речи (%.1f с). Нужно хотя бы 6-10 секунд чистого голоса." % total
        if failed:
            msg += " Проблемные файлы: " + "; ".join(failed)
        raise VoiceError(msg)
    paths = []
    for n, clip in enumerate(chosen, 1):
        p = out_dir / ("clip_%02d.wav" % n)
        write_wav(p, clip, REF_SR)
        paths.append(p)
    warnings = list(failed)
    if total < 12:
        warnings.append("Речи всего %.0f с - голос может получиться неточным. Лучше 20-60 секунд." % total)
    return paths, {"seconds": round(total, 1), "clips": len(paths), "warnings": warnings}


def measure_f0(path):
    """Медианный основной тон записи в Гц. 0, если измерить не удалось."""
    try:
        import librosa
        y, _ = librosa.load(str(path), sr=REF_SR)
        if len(y) < REF_SR // 2:
            return 0.0
        f0 = librosa.yin(y, fmin=60.0, fmax=500.0, sr=REF_SR, frame_length=1024, hop_length=256)
        rms = librosa.feature.rms(y=y, hop_length=256)[0]
        n = min(len(f0), len(rms))
        f0, rms = f0[:n], rms[:n]
        thr = max(float(np.percentile(rms, 95)) * 0.15, 1e-4)
        voiced = f0[rms > thr]
        voiced = voiced[np.isfinite(voiced)]
        if len(voiced) < 10:
            return 0.0
        return float(np.median(voiced))
    except Exception:
        log.exception("не удалось измерить основной тон: %s", path)
        return 0.0



def clips_f0(clips_dir):
    """Медианный тон по готовым кускам образца - для голосов, импортированных ранее."""
    d = Path(clips_dir)
    if not d.exists():
        return 0.0
    f0s = [measure_f0(p) for p in sorted(d.glob("clip_*.wav"))]
    f0s = [f for f in f0s if f > 0]
    return round(float(np.median(f0s)), 1) if f0s else 0.0


class VoiceStore:
    """Голоса: свои (папка voices/) и стандартные (из комплекта модели)."""

    def __init__(self, root, engine, get_disabled):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.engine = engine
        self.get_disabled = get_disabled
        self._latents = {}

    def _custom(self):
        out = []
        for d in sorted(self.root.iterdir()):
            meta = d / "voice.json"
            if not (d.is_dir() and meta.exists()) or d.name.startswith("_"):
                continue
            try:
                info = json.loads(meta.read_text(encoding="utf-8-sig"))
            except Exception:
                log.exception("битый голос в папке %s", d)
                continue
            if info.get("type") == "rvc" and (d / "model.pth").exists():
                out.append({"id": "c:" + d.name, "name": info["name"], "aliases": info.get("aliases", []),
                            "kind": "rvc", "engine": "rvc", "base": info.get("base", ""), "pitch": info.get("pitch", 0),
                            "index_rate": info.get("index_rate", 0.5), "has_index": (d / "model.index").exists(),
                            "created": info.get("created", 0)})
            elif (d / "latents.pth").exists():
                try:
                    if not info.get("ref_f0"):
                        # голос импортирован до появления функции: считаем тон эталона один раз
                        info["ref_f0"] = clips_f0(d / "clips")
                        if info["ref_f0"]:
                            meta.write_text(json.dumps(info, ensure_ascii=False, indent=2),
                                            encoding="utf-8")
                    out.append({"id": "c:" + d.name, "name": info["name"],
                                "aliases": info.get("aliases", []), "kind": "custom",
                                "engine": info.get("engine", "xtts"),
                                "cv_ready": bool(info.get("cv_text")),
                                "cv_seconds": info.get("cv_seconds", 0),
                                "cv_text": info.get("cv_text", ""),
                                "seconds": info.get("seconds", 0), "created": info.get("created", 0),
                                "pitch": info.get("pitch", 0), "ref_f0": info.get("ref_f0", 0)})
                except Exception:
                    log.exception("битый голос в папке %s", d)
        out.sort(key=lambda v: v["name"].lower())
        return out

    def list(self):
        """Свои голоса и обученные модели RVC.

        Стандартные голоса из комплекта XTTS не показываются: работаем только
        с голосами, которые записаны пользователем или обучены на его записях.
        """
        disabled = set(self.get_disabled())
        voices = self._custom()
        for v in voices:
            v["enabled"] = v["id"] not in disabled
        return voices

    def enabled(self):
        return [v for v in self.list() if v["enabled"]]

    def get(self, voice_id):
        for v in self.list():
            if v["id"] == voice_id:
                return v
        return None

    def resolve(self, query):
        """Ищет включённый голос по имени из чата: точное совпадение, затем по началу имени."""
        key = norm_key(query)
        if not key:
            return None
        voices = self.enabled()
        keys = [(v, [norm_key(v["name"])] + [norm_key(a) for a in v["aliases"]]) for v in voices]
        for v, ks in keys:
            if key in ks:
                return v
        for v, ks in keys:
            if any(k.startswith(key) for k in ks):
                return v
        return None

    def base_of(self, voice):
        """Базовый (обычный) голос, которым читается текст перед переозвучкой RVC."""
        plain = [v for v in self.list() if v["kind"] != "rvc"]
        for v in plain:
            if v["id"] == voice.get("base"):
                return v
        return plain[0] if plain else None

    def model_files(self, voice_id):
        d = self.root / voice_id.partition(":")[2]
        idx = d / "model.index"
        return d / "model.pth", (idx if idx.exists() else None)

    def latents(self, voice_id):
        voice = self.get(voice_id)
        if voice and voice["kind"] == "rvc":
            base = self.base_of(voice)
            if not base:
                raise VoiceError("Нет базового голоса для RVC.")
            voice_id = base["id"]
        if voice_id not in self._latents:
            kind, _, name = voice_id.partition(":")
            if kind == "s":
                self._latents[voice_id] = self.engine.standard_latents(name)
            else:
                self._latents[voice_id] = self.engine.load_latents(self.root / name / "latents.pth")
        return self._latents[voice_id]

    def check_name(self, name, aliases):
        if not norm_key(name):
            raise VoiceError("Введите имя голоса (буквы или цифры).")
        new_keys = {norm_key(name)} | {norm_key(a) for a in aliases if norm_key(a)}
        for v in self.list():
            old = {norm_key(v["name"])} | {norm_key(a) for a in v["aliases"]}
            if new_keys & old:
                raise VoiceError("Имя уже занято голосом «%s»." % v["name"])

    def create(self, name, aliases, src_paths, names=None):
        """Полный цикл: файлы -> чистка и нарезка -> слепок голоса -> запись в базу."""
        name = name.strip()[:40]
        aliases = [a.strip()[:40] for a in aliases if a.strip()][:5]
        self.check_name(name, aliases)
        if self.engine.status != "ready":
            raise VoiceError("Модель озвучки ещё не загружена, подождите.")
        folder = "v%d_%s" % (int(time.time()), uuid.uuid4().hex[:6])
        tmp = self.root / ("_tmp_" + folder)
        try:
            clips, report = prepare_clips(src_paths, tmp / "clips", names)
            f0s = [measure_f0(p) for p in clips]
            f0s = [f for f in f0s if f > 0]
            ref_f0 = round(float(np.median(f0s)), 1) if f0s else 0.0
            report["ref_f0"] = ref_f0
            latents = self.engine.make_latents(clips)
            self.engine.save_latents(latents, tmp / "latents.pth")
            (tmp / "voice.json").write_text(json.dumps({
                "name": name, "aliases": aliases, "seconds": report["seconds"],
                "pitch": 0, "ref_f0": ref_f0,
                "created": int(time.time())}, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, self.root / folder)
        except Exception:
            shutil.rmtree(tmp, ignore_errors=True)
            raise
        voice = self.get("c:" + folder)
        return voice, report

    def create_rvc(self, name, aliases, pth, index, base="", pitch=0):
        """Добавляет RVC-модель: копирует .pth и .index в базу голосов."""
        name = name.strip()[:40]
        aliases = [a.strip()[:40] for a in aliases if a.strip()][:5]
        self.check_name(name, aliases)
        folder = "r%d_%s" % (int(time.time()), uuid.uuid4().hex[:6])
        tmp = self.root / ("_tmp_" + folder)
        try:
            tmp.mkdir(parents=True)
            shutil.copyfile(pth, tmp / "model.pth")
            if index:
                shutil.copyfile(index, tmp / "model.index")
            (tmp / "voice.json").write_text(json.dumps({
                "type": "rvc", "name": name, "aliases": aliases, "base": base,
                "pitch": max(-24, min(int(pitch), 24)), "index_rate": 0.5,
                "created": int(time.time())}, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, self.root / folder)
        except Exception:
            shutil.rmtree(tmp, ignore_errors=True)
            raise
        return self.get("c:" + folder)

    def update_pitch(self, voice_id, pitch):
        """Сдвиг тона (полутона) для своего голоса."""
        voice = self.get(voice_id)
        if not voice or voice["kind"] != "custom":
            raise VoiceError("Сдвиг тона доступен только для своих голосов.")
        meta = self.root / voice_id.partition(":")[2] / "voice.json"
        info = json.loads(meta.read_text(encoding="utf-8-sig"))
        try:
            steps = int(round(float(pitch)))
        except (TypeError, ValueError):
            steps = 0
        info["pitch"] = max(-24, min(steps, 24))
        meta.write_text(json.dumps(info, ensure_ascii=False, indent=2), encoding="utf-8")
        return self.get(voice_id)


    # ---------- CosyVoice 3 ----------
    def _folder(self, voice_id):
        return self.root / voice_id.partition(":")[2]

    def set_engine(self, voice_id, engine):
        """Переключает движок озвучки своего голоса: xtts или cosy."""
        voice = self.get(voice_id)
        if not voice or voice["kind"] != "custom":
            raise VoiceError("Движок переключается только у своих голосов.")
        engine = str(engine)
        if engine not in ("xtts", "cosy"):
            raise VoiceError("Неизвестный движок: %s" % engine)
        meta = self._folder(voice_id) / "voice.json"
        info = json.loads(meta.read_text(encoding="utf-8-sig"))
        info["engine"] = engine
        meta.write_text(json.dumps(info, ensure_ascii=False, indent=2), encoding="utf-8")
        return self.get(voice_id)

    def prepare_cosyvoice(self, voice_id):
        """Собирает образец для CosyVoice 3 из готовых кусков записи.

        CosyVoice работает в режиме in-context: нужен один непрерывный кусок
        речи и точный текст этого куска.
        """
        from engine import read_wav, write_wav
        voice = self.get(voice_id)
        if not voice or voice["kind"] != "custom":
            raise VoiceError("Образец CosyVoice 3 готовят только для своих голосов.")
        d = self._folder(voice_id)
        clips = sorted((d / "clips").glob("clip_*.wav"))
        if not clips:
            raise VoiceError("У голоса нет готовых образцов.")
        chunks, total = [], 0.0
        for p in clips:
            x = read_wav(p)
            chunks.append(x)
            total += len(x) / float(REF_SR)
            if total >= 12.0:
                break
        if total < 3.0:
            raise VoiceError("Слишком мало речи для образца (%.1f с). Нужно хотя бы 3 секунды." % total)
        gap = np.zeros(int(0.12 * REF_SR), dtype=np.float32)
        parts = []
        for i, c in enumerate(chunks):
            if i:
                parts.append(gap)
            parts.append(c)
        x = np.concatenate(parts).astype(np.float32)
        n = int(0.02 * REF_SR)
        if len(x) > 2 * n:
            x[:n] *= np.linspace(0, 1, n)
            x[-n:] *= np.linspace(1, 0, n)
        out = d / "cv_prompt.wav"
        write_wav(out, x, REF_SR)
        meta = d / "voice.json"
        info = json.loads(meta.read_text(encoding="utf-8-sig"))
        info["cv_prompt"] = "cv_prompt.wav"
        info["cv_seconds"] = round(len(x) / float(REF_SR), 1)
        meta.write_text(json.dumps(info, ensure_ascii=False, indent=2), encoding="utf-8")
        return str(out), info.get("cv_text", "")

    def set_transcript(self, voice_id, text):
        """Сохраняет текст образца для CosyVoice 3."""
        voice = self.get(voice_id)
        if not voice or voice["kind"] != "custom":
            raise VoiceError("Текст образца задаётся только для своих голосов.")
        meta = self._folder(voice_id) / "voice.json"
        info = json.loads(meta.read_text(encoding="utf-8-sig"))
        info["cv_text"] = str(text).strip()[:600]
        meta.write_text(json.dumps(info, ensure_ascii=False, indent=2), encoding="utf-8")
        return self.get(voice_id)

    def cosyvoice_prompt(self, voice_id):
        """Путь к образцу и его текст для синтеза CosyVoice 3."""
        voice = self.get(voice_id)
        if not voice or voice["kind"] != "custom":
            raise VoiceError("CosyVoice 3 работает только со своими голосами.")
        meta = self._folder(voice_id) / "voice.json"
        info = json.loads(meta.read_text(encoding="utf-8-sig"))
        prompt = info.get("cv_prompt") or ""
        text = (info.get("cv_text") or "").strip()
        path = self._folder(voice_id) / prompt if prompt else None
        if not prompt or not path.exists():
            raise VoiceError("Для этого голоса не подготовлен образец CosyVoice 3.")
        if not text:
            raise VoiceError("Для этого голоса не указан текст образца.")
        return str(path), text

    def effective_pitch(self, voice):
        """Итоговый сдвиг тона, который нужно применить при синтезе."""
        if not voice:
            return 0.0
        if voice["kind"] == "rvc":
            base = self.base_of(voice)
            if base and base["kind"] == "custom":
                return float(base.get("pitch", 0) or 0)
            return 0.0
        return float(voice.get("pitch", 0) or 0)

    def update_rvc(self, voice_id, base=None, pitch=None, index_rate=None):
        voice = self.get(voice_id)
        if not voice or voice["kind"] != "rvc":
            raise VoiceError("RVC-голос не найден.")
        meta = self.root / voice_id.partition(":")[2] / "voice.json"
        info = json.loads(meta.read_text(encoding="utf-8-sig"))
        if base is not None:
            info["base"] = str(base)
        if pitch is not None:
            info["pitch"] = max(-24, min(int(pitch), 24))
        if index_rate is not None:
            info["index_rate"] = max(0.0, min(float(index_rate), 1.0))
        meta.write_text(json.dumps(info, ensure_ascii=False, indent=2), encoding="utf-8")
        return self.get(voice_id)

    def delete(self, voice_id):
        kind, _, name = voice_id.partition(":")
        if kind != "c" or not name or "/" in name or "\\" in name or name.startswith("."):
            raise VoiceError("Удалять можно только свои голоса.")
        path = self.root / name
        if not (path / "voice.json").exists():
            raise VoiceError("Голос не найден.")
        shutil.rmtree(path)
        self._latents.pop(voice_id, None)
