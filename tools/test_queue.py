# -*- coding: utf-8 -*-
"""Тесты очереди озвучки (app/speak_queue.py) без сервера и моделей.

Проверяются: порядок реплик, пауза, очистка, пропуск, «поколение» заданий
(после очистки начатая реплика не должна прозвучать) и предгенерация: пока
звучит текущая реплика, следующая уже синтезируется.

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
    while asyncio.get_running_loop().time() < deadline:
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
        self.timeline = []      # (что, id) - по нему видно порядок
        self.dur = dur
        self.ready = ready
        self.gate = None
        self.queue = SpeakQueue(synthesize=self.synth, broadcast=self.broadcast,
                                is_ready=lambda: self.ready, on_state=lambda: None,
                                on_finished=lambda: None)
        self.queue.start(asyncio.get_running_loop())

    async def synth(self, j):
        self.timeline.append(("synth", j["id"]))
        if self.gate is not None:
            await self.gate.wait()
        return (Path("cache_%s.wav" % j["id"]), self.dur)

    def broadcast(self, event):
        self.events.append(event)
        if event.get("type") in ("speak", "stop", "done"):
            self.timeline.append((event["type"], event.get("id")))

    def types(self):
        return [e["type"] for e in self.events]

    def speak_ids(self):
        return [e["id"] for e in self.events if e["type"] == "speak"]

    def done_ids(self):
        return [e["id"] for e in self.events if e["type"] == "done"]


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
    done = [e["id"] for e in self_events(h) if e["type"] == "done"]
    check("очищенные получили done", sorted(done) == ["c1", "c2"], done)
    await asyncio.sleep(0.2)
    check("очищенные не озвучились", h.speak_ids() == [], h.types())
    task.cancel()


def self_events(h):
    return h.events


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
    ok = await wait_until(lambda: ("synth", "g1") in h.timeline)
    check("задание готовится", ok, h.timeline)
    check("пока никто не играет", h.queue.state()["now"] is None, h.queue.state())
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


async def test_prefetch():
    print("предгенерация: следующая реплика готовится, пока звучит текущая")
    h = Harness(dur=3.0)
    task = asyncio.ensure_future(h.queue.worker())
    h.queue.enqueue(job("q0"))
    ok = await wait_until(lambda: h.types().count("speak") == 1)
    check("первая реплика зазвучала", ok, h.types())
    h.queue.enqueue(job("q1"))
    ok = await wait_until(lambda: ("synth", "q1") in h.timeline)
    check("вторая реплика синтезируется заранее", ok, h.timeline)
    check("счётчик очереди учитывает готовую реплику",
          h.queue.state()["queue"] == 2, h.queue.state())
    check("первая реплика ещё играет", h.queue.state()["now"] is not None
          and h.queue.state()["now"]["id"] == "q0", h.queue.state())
    ok = await wait_until(lambda: len(h.speak_ids()) == 2, timeout=8.0)
    check("вторая реплика прозвучала", ok, h.types())
    check("порядок сохранён", h.speak_ids() == ["q0", "q1"], h.speak_ids())
    tl = h.timeline
    i_speak0 = tl.index(("speak", "q0"))
    i_synth1 = tl.index(("synth", "q1"))
    i_done0 = tl.index(("done", "q0"))
    check("синтез второй реплики случился до конца первой",
          i_speak0 < i_synth1 < i_done0, tl)
    task.cancel()


async def test_clear_during_prefetch():
    print("очистка во время предгенерации")
    h = Harness(dur=1.0)
    gate = asyncio.Event()
    task = asyncio.ensure_future(h.queue.worker())
    h.queue.enqueue(job("d0"))
    ok = await wait_until(lambda: h.types().count("speak") == 1)
    check("первая реплика зазвучала", ok, h.types())
    h.gate = gate
    h.queue.enqueue(job("d1"))
    ok = await wait_until(lambda: ("synth", "d1") in h.timeline)
    check("вторая реплика готовится", ok, h.timeline)
    h.queue.clear()
    gate.set()
    ok = await wait_until(lambda: "d1" in h.done_ids(), timeout=5.0)
    check("предготовленная реплика получила done", ok, h.done_ids())
    check("предготовленная реплика не прозвучала", "d1" not in h.speak_ids(), h.speak_ids())
    task.cancel()


async def test_skip_with_prefetch():
    print("пропуск при готовой следующей реплике")
    h = Harness(dur=3.0)
    task = asyncio.ensure_future(h.queue.worker())
    h.queue.enqueue(job("k0"))
    ok = await wait_until(lambda: h.types().count("speak") == 1)
    check("первая реплика зазвучала", ok, h.types())
    h.queue.enqueue(job("k1"))
    ok = await wait_until(lambda: ("synth", "k1") in h.timeline)
    check("вторая реплика готовится", ok, h.timeline)
    h.queue.skip()
    ok = await wait_until(lambda: "stop" in h.types())
    check("пропуск остановил первую реплику", ok, h.types())
    ok = await wait_until(lambda: len(h.speak_ids()) == 2, timeout=5.0)
    check("готовая реплика прозвучала после пропуска", ok, h.types())
    check("порядок сохранён", h.speak_ids() == ["k0", "k1"], h.speak_ids())
    task.cancel()


async def main():
    for test in (test_order, test_pause, test_clear, test_skip,
                 test_generation, test_not_ready, test_prefetch,
                 test_clear_during_prefetch, test_skip_with_prefetch):
        await test()
    print()
    if FAILS:
        print("провалов: %d -> %s" % (len(FAILS), ", ".join(FAILS)))
        return 1
    print("все проверки очереди пройдены")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
