# -*- coding: utf-8 -*-
"""CosyVoice 3: мост к отдельному процессу с моделью (по образцу моста к Applio).

Модель живёт в своём окружении Python, поэтому программа запускает воркер
app/cosyvoice_worker.py и общается с ним построчно через stdin/stdout.
"""
import json
import logging
import os
import queue
import subprocess
import threading
import time
from pathlib import Path

log = logging.getLogger("cosyvoice")

MODEL_NAME = "Fun-CosyVoice3-0.5B"
READY_TIMEOUT = 300
TAG = "@@CV@@"          # префикс ответов воркера


class CosyVoiceError(Exception):
    pass


def cosyvoice_python(cv_dir):
    d = Path(cv_dir)
    for rel in ("env/Scripts/python.exe", "env/bin/python", ".venv/Scripts/python.exe",
                ".venv/bin/python", "env/python.exe"):
        if (d / rel).exists():
            return d / rel
    return None


def check_cosyvoice(path):
    """Проверяет папку с CosyVoice. Возвращает (папка, python, исходники, модель)."""
    d = Path(str(path).strip().strip('"'))
    if not d.is_dir():
        raise CosyVoiceError("Папка не найдена: %s" % d)
    py = cosyvoice_python(d)
    if not py:
        raise CosyVoiceError("В папке нет окружения Python (env\\Scripts\\python.exe).")
    repo = None
    for cand in (d / "CosyVoice", d):
        if (cand / "cosyvoice" / "cli" / "cosyvoice.py").exists():
            repo = cand
            break
    if not repo:
        raise CosyVoiceError("Не найдены исходники CosyVoice (cosyvoice\\cli\\cosyvoice.py).")
    model = None
    for cand in (d / "pretrained_models" / MODEL_NAME, d / "pretrained_models", d):
        if (cand / "cosyvoice3.yaml").exists():
            model = cand
            break
    if not model:
        raise CosyVoiceError("Не найдена модель %s (в папке нет cosyvoice3.yaml)." % MODEL_NAME)
    return d, py, repo, model


def find_cosyvoice(root):
    """Ищет папку CosyVoice рядом с программой, выше по дереву и в корне диска."""
    root = Path(root)
    places = [root]
    for parent in list(root.parents)[:3]:
        places.append(parent)
    if root.anchor:
        places.append(Path(root.anchor))
    seen = set()
    for place in places:
        try:
            for d in sorted(place.glob("cosyvoice*")):
                if not d.is_dir() or str(d).lower() in seen:
                    continue
                seen.add(str(d).lower())
                try:
                    check_cosyvoice(d)
                    return str(d)
                except CosyVoiceError:
                    continue
        except OSError:
            continue
    return ""


class CosyVoiceBridge:
    """Держит запущенным воркер CosyVoice и отправляет ему запросы."""

    def __init__(self, worker_path, log_path, on_status):
        self.worker_path = str(Path(worker_path).resolve())
        self.log_path = str(log_path)
        self.on_status = on_status
        self.status = "off"      # off | starting | ready | error
        self.error = ""
        self.device = ""
        self.path = ""
        self.proc = None
        self._answers = queue.Queue()
        self._n = 0
        self._lock = threading.Lock()

    def info(self):
        return {"status": self.status, "error": self.error, "device": self.device, "path": self.path}

    def _set(self, status, error=""):
        self.status, self.error = status, error
        try:
            self.on_status()
        except Exception:
            pass

    def stop(self):
        proc, self.proc = self.proc, None
        if proc and proc.poll() is None:
            try:
                proc.kill()
            except OSError:
                pass
        self.status = "off"

    def start(self, cv_dir):
        self.stop()
        d, py, repo, model = check_cosyvoice(cv_dir)
        self.path = str(d)
        self._set("starting")
        env = dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
        env.pop("PYTHONHOME", None)
        env.pop("PYTHONPATH", None)
        logf = open(self.log_path, "ab")
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        proc = subprocess.Popen([str(py), self.worker_path, str(repo), str(model),
                                  str(d / "whisper")],
                                 cwd=str(repo), env=env, stdin=subprocess.PIPE,
                                 stdout=subprocess.PIPE, stderr=logf, creationflags=flags)
        self.proc = proc
        threading.Thread(target=self._reader, args=(proc,), daemon=True).start()

    def _reader(self, proc):
        for raw in proc.stdout:
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith(TAG):
                continue
            try:
                msg = json.loads(line[len(TAG):])
            except ValueError:
                continue
            if "ready" in msg:
                if msg["ready"]:
                    self.device = msg.get("device", "")
                    log.info("CosyVoice 3 готов (%s)", self.device)
                    self._set("ready")
                else:
                    log.error("CosyVoice 3 не запустился: %s", msg.get("error"))
                    self._set("error", str(msg.get("error") or "").strip().splitlines()[-1][:300])
            else:
                self._answers.put(msg)
        if self.proc is proc:
            self.proc = None
            if self.status != "error":
                self._set("error", "процесс CosyVoice завершился (см. data\\cosyvoice_log.txt)")
            self._answers.put({"id": None, "ok": False, "error": "процесс CosyVoice завершился"})

    def _request(self, req, timeout):
        with self._lock:
            if self.status == "starting":
                deadline = time.time() + READY_TIMEOUT
                while self.status == "starting" and time.time() < deadline:
                    time.sleep(0.25)
                if self.status == "error":
                    raise CosyVoiceError(self.error or "CosyVoice 3 не запустился")
            if self.status != "ready" or not self.proc:
                raise CosyVoiceError("CosyVoice 3 не подключён. Укажите папку с моделью "
                                     "на вкладке «Голоса».")
            self._n += 1
            req["id"] = self._n
            while not self._answers.empty():
                self._answers.get_nowait()
            try:
                self.proc.stdin.write((json.dumps(req) + "\n").encode("utf-8"))
                self.proc.stdin.flush()
            except (OSError, AttributeError):
                raise CosyVoiceError("Процесс CosyVoice не отвечает. Перезапустите программу.")
            while True:
                try:
                    ans = self._answers.get(timeout=timeout)
                except queue.Empty:
                    raise CosyVoiceError("CosyVoice 3 не ответил за %d секунд." % timeout)
                if ans.get("id") in (req["id"], None):
                    break
            if not ans.get("ok"):
                raise CosyVoiceError("Ошибка CosyVoice 3: %s" % ans.get("error"))
            return ans

    def synthesize(self, text, prompt_wav, prompt_text, out_path, speed=1.0, timeout=300, tags=False):
        """Озвучивает текст голосом образца. Возвращает длительность результата.

        tags=True - в тексте есть теги эмоций ([laughter], [sigh], ...): воркер следит,
        чтобы нормализация текста их не испортила.
        """
        ans = self._request({"cmd": "synthesize", "text": text, "prompt_wav": str(prompt_wav),
                             "prompt_text": prompt_text, "output": str(out_path),
                             "speed": float(speed), "tags": bool(tags)}, timeout)
        return float(ans.get("duration") or 0)

    def transcribe(self, wav_path, timeout=300):
        """Расшифровывает запись (Whisper). Возвращает текст."""
        ans = self._request({"cmd": "transcribe", "input": str(wav_path)}, timeout)
        return str(ans.get("text") or "").strip()
