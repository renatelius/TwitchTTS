# -*- coding: utf-8 -*-
"""Очередь озвучки: порядок сообщений, пауза, очистка, пропуск.

Модуль ничего не знает про модели и про HTTP. Синтез приходит готовой
функцией, события - колбэком, поэтому очередь можно проверять отдельно
(tools/test_queue.py) и менять без риска сломать озвучку.

Зависимости (передаются при создании):
    synthesize(job)  -> (path, dur)   асинхронная функция синтеза реплики
    broadcast(event)                  отправить событие подписчикам (SSE)
    is_ready()      -> bool           готов ли движок синтеза
    on_state()                       состояние изменилось (статус в интерфейсе)
    on_finished()                    задание обработано (чистка кэша)

Пока играет текущая реплика, следующая уже синтезируется: без этого модель
простаивает половину времени (озвучка длится примерно столько же, сколько
заняла подготовка).

Модуль назван speak_queue, а не queue: каталог приложения стоит первым в
sys.path, и файл queue.py перекрыл бы стандартный модуль queue, который
нужен concurrent.futures и logging.handlers.
"""
import asyncio
import logging
from collections import deque

log = logging.getLogger("queue")


class SpeakQueue:
    """Очередь реплик: одна озвучивается, остальные ждут."""

    def __init__(self, synthesize, broadcast, is_ready, on_state=None, on_finished=None):
        self.synthesize = synthesize
        self.broadcast = broadcast
        self.is_ready = is_ready
        self.on_state = on_state or (lambda: None)
        self.on_finished = on_finished or (lambda: None)
        self.pending = deque()
        self.gen = 0          # номер поколения: сброс очереди увеличивает его
        self.now = None       # задание, которое сейчас звучит
        self.paused = False
        self.wake = None      # событие "пора взять задание"
        self.skip_event = None  # событие "прервать текущее задание"
        self._prefetch_task = None   # синтез следующего задания (идёт параллельно)
        self._ready = None           # следующее задание уже синтезировано

    # ---------- состояние ----------
    def _waiting(self):
        """Сколько реплик ждут своей очереди.

        Считается и реплика, которую воркер готовит заранее: она тоже ещё
        не прозвучала.
        """
        n = len(self.pending)
        if self._prefetch_task is not None or self._ready is not None:
            n += 1
        return n

    def state(self):
        """Состояние очереди для интерфейса."""
        now = self.now
        return {
            "queue": self._waiting() + (1 if now else 0),
            "paused": self.paused,
            "now": now and {"id": now["id"], "user": now["user"], "voice": now["voice_name"]},
        }

    def pending_len(self):
        """Сколько реплик ждут своей очереди."""
        return self._waiting()

    # ---------- команды ----------
    def enqueue(self, job):
        """Кладёт задание в очередь и будит воркер."""
        job = dict(job)
        job["gen"] = self.gen
        self.pending.append(job)
        self.wake_up()
        self.on_state()
        self._maybe_prefetch()

    def skip(self):
        """Прерывает текущую реплику (озвучка замолкает)."""
        if self.skip_event is not None:
            self.skip_event.set()

    def clear(self):
        """Убирает все ожидающие задания и прерывает текущее."""
        self.gen += 1
        for job in self.pending:
            self.broadcast({"type": "done", "id": job["id"]})
        self.pending.clear()
        self.skip()
        self.on_state()

    def pause(self):
        """Ставит очередь на паузу: новые реплики копятся."""
        self.paused = True

    def resume(self):
        """Снимает паузу и будит воркер."""
        self.paused = False
        self.wake_up()

    def wake_up(self):
        """Просит воркер проверить очередь."""
        if self.wake is not None:
            self.wake.set()

    # ---------- работа ----------
    def start(self, loop=None):
        """Готовит очередь к работе (нужен запущенный цикл событий)."""
        self.loop = loop or asyncio.get_running_loop()
        self.wake = asyncio.Event()
        self.skip_event = asyncio.Event()

    async def worker(self):
        """Берёт сообщения по одному: озвучивает, отправляет в браузер, ждёт конца звучания.

        Пока звучит текущая реплика, синтезируется следующая - так модель не
        простаивает.
        """
        while True:
            got = await self._acquire()
            if got is None:
                continue
            job, path, dur = got
            self.skip_event.clear()
            if job["gen"] != self.gen:
                # очередь очистили, пока задание готовилось
                self._finish(job)
                continue
            self.now = job
            self.on_state()
            self.broadcast({"type": "speak", "id": job["id"], "url": "/audio/" + path.name,
                            "user": job["user"], "voice": job["voice_name"],
                            "voice_id": job.get("voice_id", ""),
                            "vol": job.get("vol", 1.0), "duration": dur})
            self._maybe_prefetch()
            try:
                await asyncio.wait_for(self.skip_event.wait(), timeout=dur + 0.35)
                self.broadcast({"type": "stop"})
            except asyncio.TimeoutError:
                pass
            self._finish(job)

    async def _acquire(self):
        """Берёт задание к озвучке: уже готовое или новое.

        Возвращает (job, path, dur). Пока очередь пуста или на паузе - ждёт.
        """
        while True:
            if self._ready is not None:
                if self.paused:
                    await self._wait()
                    continue
                ready, self._ready = self._ready, None
                return ready
            if self._prefetch_task is not None:
                task, self._prefetch_task = self._prefetch_task, None
                self._ready = await task
                continue
            if self.pending and not self.paused and self.is_ready():
                job = self.pending.popleft()
                path, dur = await self.synthesize(job)
                return (job, path, dur)
            await self._wait()

    async def _wait(self):
        """Ждёт новых заданий или снятия паузы."""
        self.wake.clear()
        try:
            await asyncio.wait_for(self.wake.wait(), timeout=1.0)
        except asyncio.TimeoutError:
            pass

    def _maybe_prefetch(self):
        """Готовит следующую реплику, пока звучит текущая.

        Вызывается и когда реплика встаёт в очередь, и когда текущая
        начинает звучать: сообщения приходят по одному, и ждать, пока
        следующее уже окажется в очереди, нельзя.
        """
        if self.now is None:
            return                      # никто не звучит - готовить нечего
        if self._prefetch_task is not None or self._ready is not None:
            return                      # следующая уже готовится
        if not self.pending:
            return
        job = self.pending.popleft()
        loop = self.loop
        if loop is None:
            self.pending.appendleft(job)
            return
        loop.call_soon_threadsafe(self._launch_prefetch, job)

    def _launch_prefetch(self, job):
        """Создаёт задачу предгенерации (выполняется в цикле событий)."""
        if self._prefetch_task is not None or self._ready is not None:
            self.pending.appendleft(job)
            return
        self._prefetch_task = asyncio.ensure_future(self._prefetch(job))

    async def _prefetch(self, job):
        """Готовит задание заранее: синтез идёт, пока звучит предыдущее."""
        try:
            path, dur = await self.synthesize(job)
        except Exception as e:
            log.exception("ошибка озвучки")
            self.broadcast({"type": "tts_error", "id": job["id"], "error": str(e)})
            self.broadcast({"type": "done", "id": job["id"]})
            return None
        return (job, path, dur)

    def _finish(self, job):
        """Задание обработано: сообщаем и чистим кэш."""
        self.broadcast({"type": "done", "id": job["id"]})
        self.now = None
        self.on_state()
        self.on_finished()
