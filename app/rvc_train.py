# -*- coding: utf-8 -*-
"""Обучение RVC-модели из своих записей.

Пайплайн тот же, что у Applio, но запускается из приложения:
нарезка -> извлечение тона и признаков -> обучение -> сборка индекса.
Все файлы кладём в папку приложения (data/rvc_models), Applio об этом не знает.
"""
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

log = logging.getLogger("rvctrain")

MAX_UPLOAD = 500 * 1024 * 1024
EPOCH_RE = re.compile(r"epoch=(\d+)")
STEP_RE = re.compile(r"step=(\d+)")
BATCH_RE = re.compile(r"batch=(\d+)/(\d+)")
TIME_RE = re.compile(r"time=([0-9:]+)")
SPEED_RE = re.compile(r"training_speed=([0-9:]+)")
PTH_RE = re.compile(r"_(\d+)e_\d+s\.pth$")

STAGES = {
    "queued": "В очереди",
    "preparing": "Подготовка записей",
    "preprocessing": "Нарезка и нормализация",
    "extracting": "Извлечение тона и признаков",
    "training": "Обучение модели",
    "indexing": "Сборка индекса",
    "done": "Готово",
    "error": "Ошибка",
    "cancelled": "Отменено",
}


class TrainError(Exception):
    pass


def _hms(text):
    """'0:01:29' -> секунды."""
    try:
        parts = [int(x) for x in text.split(":")]
    except ValueError:
        return 0
    out = 0
    for p in parts:
        out = out * 60 + p
    return out


class RvcTrainer:
    """Готовит и обучает RVC-модель, ведёт прогресс, умеет отменяться."""

    def __init__(self, applio_dir, work_dir, log_path, on_status=None):
        self.applio_dir = Path(applio_dir) if applio_dir else None
        self.work_dir = Path(work_dir)
        self.log_path = Path(log_path)
        self.on_status = on_status
        self.lock = threading.RLock()
        self.state = "idle"
        self.stage = ""
        self.progress = 0.0
        self.detail = ""
        self.error = ""
        self.job_id = ""
        self.model_name = ""
        self.voice_name = ""
        self.aliases = []
        self.epochs_total = 0
        self.epoch = 0
        self.step = 0
        self.batch_total = 0
        self.batch = 0
        self.elapsed = 0.0
        self.log_lines = []
        self.result = {}
        self.proc = None
        self.thread = None
        self.canceled = False
        self.dataset_seconds = 0.0
        self.opts = {}

    # ---------- состояние ----------
    def _touch(self):
        try:
            if self.on_status:
                self.on_status()
        except Exception:
            pass

    def busy(self):
        with self.lock:
            return self.state in ("queued", "preparing", "preprocessing",
                                  "extracting", "training", "indexing")

    def status(self):
        with self.lock:
            return {
                "state": self.state,
                "stage": self.stage,
                "stage_text": STAGES.get(self.stage, self.stage),
                "progress": round(self.progress, 3),
                "detail": self.detail,
                "error": self.error,
                "job_id": self.job_id,
                "voice_name": self.voice_name,
                "epochs_total": self.epochs_total,
                "epoch": self.epoch,
                "step": self.step,
                "batch": self.batch,
                "batch_total": self.batch_total,
                "elapsed": int(self.elapsed),
                "dataset_seconds": round(self.dataset_seconds, 1),
                "aliases": list(self.aliases),
                "log": self.log_lines[-40:],
                "result": dict(self.result),
            }

    def _set(self, state, stage="", progress=None, detail="", error=""):
        with self.lock:
            self.state = state
            if stage:
                self.stage = stage
            if progress is not None:
                self.progress = progress
            self.detail = detail
            if error:
                self.error = error
        self._touch()

    # ---------- проверки ----------
    def check(self):
        """Готова ли машина к обучению. Возвращает (ok, сообщение)."""
        if not self.applio_dir or not self.applio_dir.is_dir():
            return False, "Applio не подключён. Укажите его папку на вкладке «Голоса»."
        py = None
        for rel in ("env/python.exe", "env/Scripts/python.exe", ".venv/Scripts/python.exe"):
            if (self.applio_dir / rel).exists():
                py = self.applio_dir / rel
                break
        if not py:
            return False, "В папке Applio нет окружения Python. Запустите в ней run-install.bat."
        need = [self.applio_dir / "rvc" / "train" / "preprocess" / "preprocess.py",
                self.applio_dir / "rvc" / "train" / "extract" / "extract.py",
                self.applio_dir / "rvc" / "train" / "train.py",
                self.applio_dir / "rvc" / "train" / "process" / "extract_index.py"]
        missing = [p.name for p in need if not p.exists()]
        if missing:
            return False, "В папке Applio нет нужных файлов: %s" % ", ".join(missing)
        self.python = py
        return True, ""

    def _pretrained(self, sample_rate, vocoder="HiFi-GAN"):
        folder = "hifi-gan" if vocoder != "RefineGAN" else "refinegan"
        tag = {32000: "32k", 40000: "40k", 48000: "48k"}.get(int(sample_rate))
        if not tag:
            raise TrainError("Частота %d Гц не поддерживается (нужно 32000, 40000 или 48000)."
                             % sample_rate)
        base = self.applio_dir / "rvc" / "models" / "pretraineds" / folder
        g = base / ("f0G%s.pth" % tag)
        d = base / ("f0D%s.pth" % tag)
        if not g.exists() or not d.exists():
            raise TrainError("Нет заготовленных весов %s. Запустите в Applio обновление "
                             "(run-updater.bat) или скачайте их через Applio." % tag)
        return str(g), str(d)

    # ---------- запуск ----------
    def start(self, voice_name, aliases, files, opts):
        ok, msg = self.check()
        if not ok:
            raise TrainError(msg)
        if self.busy():
            raise TrainError("Обучение уже идёт. Дождитесь окончания или нажмите «Отменить».")
        voice_name = (voice_name or "").strip()[:40]
        if not voice_name:
            raise TrainError("Введите имя голоса.")
        if not files:
            raise TrainError("Добавьте записи голоса.")
        epochs = max(10, min(2000, int(opts.get("epochs") or 200)))
        batch = max(1, min(32, int(opts.get("batch") or 4)))
        sample_rate = int(opts.get("sample_rate") or 40000)
        self._pretrained(sample_rate)  # проверяем, что веса есть, до старта
        job_id = uuid.uuid4().hex[:10]
        job_dir = self.work_dir / job_id
        dataset = job_dir / "dataset"
        dataset.mkdir(parents=True, exist_ok=True)
        with self.lock:
            self.job_id = job_id
            self.model_name = "twitchtts_%s" % job_id
            self.voice_name = voice_name
            self.aliases = list(aliases or [])
            self.epochs_total = epochs
            self.epoch = 0
            self.step = 0
            self.batch = 0
            self.batch_total = 0
            self.elapsed = 0.0
            self.error = ""
            self.detail = ""
            self.result = {}
            self.log_lines = []
            self.canceled = False
            self.dataset_seconds = 0.0
        self._set("queued", "preparing", 0.0)
        self.thread = threading.Thread(target=self._worker, args=(job_dir, dataset, files, opts),
                                       daemon=True)
        self.thread.start()
        return self.status()

    def set_error(self, message):
        """Пометить задание как ошибочное (например, не удалось добавить голос)."""
        self._set("error", "error", 1.0, "", message)

    def cancel(self):
        with self.lock:
            if not self.busy():
                return False
            self.canceled = True
        self._kill()
        self._set("cancelled", "cancelled", 0.0, "Обучение отменено.")
        return True

    def _kill(self):
        proc = self.proc
        if not proc or proc.poll() is not None:
            return
        try:
            if os.name == "nt":
                subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                               capture_output=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            else:
                proc.terminate()
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass

    # ---------- внутреннее ----------
    def _log(self, line):
        line = line.rstrip()
        if not line:
            return
        with self.lock:
            self.log_lines.append(line)
            if len(self.log_lines) > 400:
                self.log_lines = self.log_lines[-300:]

    def _convert(self, src, dst, sample_rate):
        """Любой файл -> моно wav нужной частоты (для набора данных)."""
        import imageio_ffmpeg
        exe = imageio_ffmpeg.get_ffmpeg_exe()
        cmd = [exe, "-v", "error", "-nostdin", "-i", str(src), "-vn", "-ac", "1",
               "-ar", str(sample_rate), "-c:a", "pcm_s16le", str(dst)]
        p = subprocess.run(cmd, capture_output=True,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if p.returncode != 0 or not dst.exists() or dst.stat().st_size < 1000:
            raise TrainError("Не удалось прочитать файл %s" % Path(src).name)

    def _duration(self, path):
        try:
            import wave
            with wave.open(str(path), "rb") as w:
                return w.getnframes() / float(w.getframerate())
        except Exception:
            return 0.0

    def _env(self):
        env = dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8",
                   APPLIO_LOGS_DIR=str(self.work_dir))
        env.pop("PYTHONHOME", None)
        env.pop("PYTHONPATH", None)
        return env

    def _stage(self, script, args, stage, progress_from=0.0, progress_to=1.0):
        """Запускает один шаг Applio, разбирает его вывод."""
        cmd = [str(self.python), str(script)] + [str(a) for a in args]
        log.info("обучение: %s", " ".join(cmd[1:]))
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        self.proc = subprocess.Popen(cmd, cwd=str(self.applio_dir), env=self._env(),
                                     stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                     creationflags=flags)
        started = time.time()
        try:
            for raw in self.proc.stdout:
                if self.canceled:
                    break
                line = raw.decode("utf-8", "replace").replace("\r", "\n")
                for part in line.split("\n"):
                    part = part.strip()
                    if not part:
                        continue
                    self._log(part)
                    self._parse(part, stage, progress_from, progress_to)
        finally:
            try:
                self.proc.stdout.close()
            except Exception:
                pass
            code = self.proc.wait()
            self.proc = None
        with self.lock:
            self.elapsed = time.time() - started
        return code

    def _parse(self, line, stage, p_from, p_to):
        """Достаёт из строки лога номер эпохи/шага и двигает прогресс."""
        if stage != "training":
            return
        m = EPOCH_RE.search(line)
        if not m:
            return
        epoch = int(m.group(1))
        step = int(STEP_RE.search(line).group(1)) if STEP_RE.search(line) else 0
        bm = BATCH_RE.search(line)
        batch = int(bm.group(1)) if bm else 0
        batch_total = int(bm.group(2)) if bm else 0
        with self.lock:
            self.epoch = epoch
            self.step = step
            self.batch = batch
            self.batch_total = batch_total
            total = max(1, self.epochs_total)
            frac = min(1.0, (epoch - 1 + (batch / float(batch_total) if batch_total else 0.0)) / total)
            self.progress = p_from + (p_to - p_from) * frac
            speed = SPEED_RE.search(line)
            self.detail = "эпоха %d из %d%s%s" % (
                epoch, self.epochs_total,
                (", шаг %d" % step) if step else "",
                (" · %s" % speed.group(1)) if speed else "")
        self._touch()

    def _worker(self, job_dir, dataset, files, opts):
        try:
            sample_rate = int(opts.get("sample_rate") or 40000)
            cores = max(1, min(16, (os.cpu_count() or 4)))
            model_name = self.model_name
            exp_dir = self.work_dir / model_name
            exp_dir.mkdir(parents=True, exist_ok=True)

            # 1. подготовка записей
            self._set("preparing", "preparing", 0.02, "Готовлю %d файлов…" % len(files))
            total = 0.0
            for n, src in enumerate(files, 1):
                if self.canceled:
                    raise TrainError("Отменено")
                ext = os.path.splitext(src)[1][:8] or ".wav"
                dst = dataset / ("%02d%s" % (n, ext))
                self._convert(src, dst, sample_rate)
                total += self._duration(dst)
                self._set("preparing", "preparing", 0.02 + 0.06 * n / max(1, len(files)),
                          "Готовлю %d из %d" % (n, len(files)))
            with self.lock:
                self.dataset_seconds = total
            # исходные файлы больше не нужны - чистим временную папку
            try:
                shutil.rmtree(Path(files[0]).parent, ignore_errors=True)
            except Exception:
                pass
            if total < 30:
                raise TrainError("Слишком мало речи (%.0f с). Для обучения нужно хотя бы "
                                 "минута, лучше 5-10 минут." % total)

            # 2. нарезка и нормализация
            self._set("preprocessing", "preprocessing", 0.08,
                      "Режу записи на куски и нормализую")
            code = self._stage(self.applio_dir / "rvc" / "train" / "preprocess" / "preprocess.py",
                               [exp_dir, dataset, sample_rate, cores, "Automatic", "False",
                                "0.3", "0.3", "none"], "preprocessing")
            if self.canceled:
                raise TrainError("Отменено")
            if code != 0:
                raise TrainError("Не удалось подготовить записи (см. лог).")

            # 3. извлечение тона и признаков
            self._set("extracting", "extracting", 0.12,
                      "Считаю тон (RMVPE) и признаки речи")
            code = self._stage(self.applio_dir / "rvc" / "train" / "extract" / "extract.py",
                               [exp_dir, "rmvpe", cores, "0", sample_rate, "contentvec", "", "2"],
                               "extracting")
            if self.canceled:
                raise TrainError("Отменено")
            if code != 0:
                raise TrainError("Не удалось извлечь признаки (см. лог).")

            # 4. обучение
            pg, pd = self._pretrained(sample_rate)
            self._set("training", "training", 0.15,
                      "Обучение %d эпох" % self.epochs_total)
            code = self._stage(self.applio_dir / "rvc" / "train" / "train.py",
                               [model_name, 25, self.epochs_total, pg, pd, "0", opts.get("batch") or 4,
                                sample_rate, "True", "False", "False", "True", "HiFi-GAN", "False"],
                               "training")
            if self.canceled:
                raise TrainError("Отменено")
            if code != 0:
                raise TrainError("Обучение прервано с ошибкой (см. лог).")

            # 5. индекс
            self._set("indexing", "indexing", 0.92, "Собираю индекс")
            code = self._stage(self.applio_dir / "rvc" / "train" / "process" / "extract_index.py",
                               [exp_dir, "Auto"], "indexing")
            if self.canceled:
                raise TrainError("Отменено")
            if code != 0:
                raise TrainError("Не удалось собрать индекс (см. лог).")

            pth = self._best_model(exp_dir)
            index = exp_dir / ("%s.index" % model_name)
            if not pth:
                raise TrainError("Модель не найдена после обучения (см. лог).")
            if not index.exists():
                raise TrainError("Индекс не найден после обучения (см. лог).")
            with self.lock:
                self.result = {"pth": str(pth), "index": str(index),
                               "dataset_seconds": round(self.dataset_seconds, 1),
                               "epochs": self.epochs_total}
            self._set("done", "done", 1.0, "Модель готова")
        except TrainError as e:
            if self.canceled:
                self._set("cancelled", "cancelled", 0.0, "Обучение отменено.")
            else:
                self._set("error", "error", 0.0, "", str(e))
        except Exception as e:
            log.exception("ошибка обучения")
            self._set("error", "error", 0.0, "", "%s: %s" % (type(e).__name__, e))

    def _best_model(self, exp_dir):
        """Последняя сохранённая модель (имя вроде twitchtts_xx_120e_900s.pth)."""
        best, best_epoch = None, -1
        for p in exp_dir.glob("%s_*e_*s.pth" % self.model_name):
            m = PTH_RE.search(p.name)
            if not m:
                continue
            epoch = int(m.group(1))
            if epoch > best_epoch:
                best, best_epoch = p, epoch
        if best is None:
            files = sorted(exp_dir.glob("*.pth"))
            files = [f for f in files if not f.name.startswith(("G_", "D_"))]
            best = files[-1] if files else None
        return best
