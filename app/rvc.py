# -*- coding: utf-8 -*-
"""RVC: связь с Applio (отдельный процесс) и загрузка моделей из файлов/HuggingFace."""
import json
import logging
import os
import queue
import shutil
import subprocess
import sys
import threading
from pathlib import Path

log = logging.getLogger("rvc")



class RvcError(Exception):
    pass


def applio_python(applio_dir):
    d = Path(applio_dir)
    for rel in ("env/python.exe", "env/Scripts/python.exe", ".venv/Scripts/python.exe",
                "env/bin/python", ".venv/bin/python"):
        if (d / rel).exists():
            return d / rel
    return None


def check_applio(path):
    """Проверяет, что в папке лежит установленный Applio. Возвращает (папка, python)."""
    d = Path(str(path).strip().strip('"'))
    if not d.is_dir():
        raise RvcError("Папка не найдена: %s" % d)
    if not (d / "rvc" / "infer" / "infer.py").exists():
        raise RvcError("В этой папке нет Applio (не найден файл rvc\\infer\\infer.py).")
    py = applio_python(d)
    if not py:
        raise RvcError("Applio найден, но не установлен: запустите в его папке run-install.bat.")
    return d, py


def find_applio(root):
    """Ищет Applio рядом с программой и в корне диска."""
    root = Path(root)
    places = [root.parent, Path(root.anchor)] if root.anchor else [root.parent]
    for place in places:
        try:
            for top in sorted(place.glob("Applio*")):
                # сам Applio может лежать и во вложенной папке (так бывает после распаковки архива)
                for d in [top] + sorted(x for x in top.glob("*") if x.is_dir()):
                    try:
                        check_applio(d)
                        return str(d)
                    except RvcError:
                        continue
        except OSError:
            pass
    return ""


class RvcBridge:
    """Держит запущенным rvc_worker.py внутри окружения Applio."""

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
            proc.kill()
        self.status = "off"

    def start(self, applio_dir):
        self.stop()
        d, py = check_applio(applio_dir)
        self.path = str(d)
        self._set("starting")
        env = dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
        env.pop("PYTHONHOME", None)
        env.pop("PYTHONPATH", None)
        logf = open(self.log_path, "ab")
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        proc = subprocess.Popen([str(py), self.worker_path, str(d)], cwd=str(d), env=env,
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=logf,
                                creationflags=flags)
        self.proc = proc
        threading.Thread(target=self._reader, args=(proc,), daemon=True).start()

    def _reader(self, proc):
        for raw in proc.stdout:
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("@@RVC@@"):
                continue
            try:
                msg = json.loads(line[7:])
            except ValueError:
                continue
            if "ready" in msg:
                if msg["ready"]:
                    self.device = msg.get("device", "")
                    log.info("RVC готов (%s)", self.device)
                    self._set("ready")
                else:
                    log.error("RVC не запустился: %s", msg.get("error"))
                    self._set("error", (msg.get("error") or "").strip().splitlines()[-1][:300])
            else:
                self._answers.put(msg)
        if self.proc is proc:
            self.proc = None
            if self.status != "error":
                self._set("error", "процесс Applio завершился (см. data\\rvc_log.txt)")
            self._answers.put({"id": None, "ok": False, "error": "процесс Applio завершился"})

    def convert(self, src, dst, model, index, pitch=0, index_rate=0.5, timeout=180):
        """Переозвучивает wav. Возвращает длительность результата."""
        with self._lock:
            if self.status == "starting":
                raise RvcError("RVC ещё запускается, подождите минуту.")
            if self.status != "ready" or not self.proc:
                raise RvcError("RVC не подключён. Укажите папку Applio на вкладке «Голоса».")
            self._n += 1
            req = {"id": self._n, "input": str(src), "output": str(dst), "model": str(model),
                   "index": str(index) if index else "", "pitch": pitch, "index_rate": index_rate}
            while not self._answers.empty():
                self._answers.get_nowait()
            try:
                self.proc.stdin.write((json.dumps(req) + "\n").encode("utf-8"))
                self.proc.stdin.flush()
            except (OSError, AttributeError):
                raise RvcError("Процесс RVC не отвечает. Перезапустите программу.")
            while True:
                try:
                    ans = self._answers.get(timeout=timeout)
                except queue.Empty:
                    raise RvcError("RVC не ответил за %d секунд." % timeout)
                if ans.get("id") in (req["id"], None):
                    break
            if not ans.get("ok"):
                raise RvcError("Ошибка RVC: %s" % ans.get("error"))
            return float(ans.get("duration") or 0)


class MockBridge(RvcBridge):
    """Заглушка для проверки интерфейса: просто копирует файл."""

    def start(self, applio_dir):
        self.path = str(applio_dir)
        self.device = "заглушка"
        self._set("ready")

    def convert(self, src, dst, model, index, pitch=0, index_rate=0.5, timeout=180):
        import wave
        shutil.copyfile(src, dst)
        with wave.open(str(dst), "rb") as w:
            return w.getnframes() / float(w.getframerate())
