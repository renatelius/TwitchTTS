# -*- coding: utf-8 -*-
"""Тесты очереди озвучки (app/speak_queue.py) без сервера и моделей.

Проверяются: порядок реплик, пауза, очистка, пропуск и «поколение» заданий
(после очистки начатая реплика не должна прозвучать).

    python tools/test_queue.py
"""
import asyncio
import sys
from pathlib import Path

APP = Path(__file__).resolve().parent.parent / "app"
if str(APP) not in sys.path:
    sys.path.insert(0, str(APP))

from speak_queue import SpeakQueue  # noqa: E402

FAILS = []


def check(name, cond, detail=""):
    if cond:
        print("  ok  %s" % name)
    else:
        print("  FAIL %s %s" % (name, detail))
        FAILS.append(name)


async def wait_until(pred, timeout=3.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        if pred():
            return True
        await asyncio.sleep(0.01)
    return False


def job(jid, text="тест"):
    return {"id": jid, "user": "юзер", "text": text, "voice_id": "v1",
            "voice_name": "Голос", "style": "", "vol": 1.0}


class Harness:
    """Очередь с подставным синтезом: события собираются в список."""

    def __init__(self, dur=0.05, ready=True):
        self.events = []
        self.dur = dur
        self.ready = ready
        self.gate = None
        self.queue = SpeakQueue(synthesize=self.synth, broadcast=self.broadcast,
                                is_ready=lambda: self.ready, on_state=lambda: None,
                                on_finished=lambda: None)
        self.queue.start(asyncio.get_running_loop())

    async def synth(self, j):
        if self.gate is not None:
            await self.gate.wait()
        return (Path("cache_%s.wav" % j["id"]), self.dur)

    def broadcast(self, event):
        self.events.append(event)

    def types(self):
        return [e["type"] for e in self.events]

    def speak_ids(self):
        return [e["id"] for e in self.events if e["type"] == "speak"]


async def test_order():
    print("порядок реплик")
    h = Harness()
    task = asyncio.ensure_future(h.queue.worker())
    for i in range(3):
        h.queue.enqueue(job("j%d" % i))
    ok = await wait_until(lambda: len(h.speak_ids()) == 3)
    check("все три прозвучали", ok, h.types())
    check("порядок сохранён", h.speak_ids() == ["j0", "j1", "j2"], h.speak_ids())
    task.cancel()


async def test_pause():
    print("пауза")
    h = Harness()
    task = asyncio.ensure_future(h.queue.worker())
    h.queue.pause()
    h.queue.enqueue(job("p1"))
    h.queue.enqueue(job("p2"))
    await asyncio.sleep(0.25)
    check("на паузе не озвучивается", h.speak_ids() == [], h.types())
    check("в очереди два задания", h.queue.pending_len() == 2, h.queue.pending_len())
    check("состояние: paused", h.queue.state()["paused"] is True)
    h.queue.resume()
    ok = await wait_until(lambda: len(h.speak_ids()) == 2)
    check("после паузы озвучились оба", ok, h.types())
    task.cancel()


async def test_clear():
    print("очистка")
    h = Harness()
    task = asyncio.ensure_future(h.queue.worker())
    h.queue.pause()
    h.queue.enqueue(job("c1"))
    h.queue.enqueue(job("c2"))
    h.queue.clear()
    check("очередь пуста", h.queue.pending_len() == 0, h.queue.pending_len())
    done = [e["id"] for e in h.events if e["type"] == "done"]
    check("очищенные получили done", sorted(done) == ["c1", "c2"], done)
    await asyncio.sleep(0.2)
    check("очищенные не озвучились", h.speak_ids() == [], h.types())
    task.cancel()


async def test_skip():
    print("пропуск")
    h = Harness(dur=5.0)
    task = asyncio.ensure_future(h.queue.worker())
    h.queue.enqueue(job("s1"))
    ok = await wait_until(lambda: h.types().count("speak") == 1)
    check("реплика зазвучала", ok, h.types())
    h.queue.skip()
    ok = await wait_until(lambda: "stop" in h.types())
    check("после пропуска пришло stop", ok, h.types())
    ok = await wait_until(lambda: "done" in h.types())
    check("после пропуска пришло done", ok, h.types())
    task.cancel()


async def test_generation():
    print("поколение заданий (задание до очистки не озвучивается)")
    h = Harness(dur=5.0)
    gate = asyncio.Event()
    h.gate = gate
    task = asyncio.ensure_future(h.queue.worker())
    h.queue.enqueue(job("g1"))
    ok = await wait_until(lambda: h.queue.state()["now"] is not None)
    check("задание взято в работу", ok, h.queue.state())
    h.queue.clear()
    gate.set()
    await asyncio.sleep(0.3)
    check("начатое до очистки не озвучилось", h.speak_ids() == [], h.types())
    check("состояние: nothing playing", h.queue.state()["now"] is None, h.queue.state())
    task.cancel()


async def test_not_ready():
    print("движок не готов")
    h = Harness()
    h.ready = False
    task = asyncio.ensure_future(h.queue.worker())
    h.queue.enqueue(job("r1"))
    await asyncio.sleep(0.25)
    check("не озвучивается, пока движок не готов", h.speak_ids() == [], h.types())
    h.ready = True
    h.queue.wake_up()
    ok = await wait_until(lambda: len(h.speak_ids()) == 1)
    check("после готовности озвучилось", ok, h.types())
    task.cancel()


async def main():
    for test in (test_order, test_pause, test_clear, test_skip,
                 test_generation, test_not_ready):
        await test()
    print()
    if FAILS:
        print("провалов: %d -> %s" % (len(FAILS), ", ".join(FAILS)))
        return 1
    print("все проверки очереди пройдены")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
