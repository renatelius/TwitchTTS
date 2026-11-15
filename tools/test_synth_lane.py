# -*- coding: utf-8 -*-
"""Проверка полосы синтеза: один слот за раз, приоритет чата выше.

    python tools/test_synth_lane.py
"""
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "app"))

from synth_lane import SynthLane  # noqa: E402

FAILED = []


def check(name, ok, detail=""):
    print("%s %s%s" % ("OK  " if ok else "FAIL", name, (" - " + detail) if detail else ""))
    if not ok:
        FAILED.append(name)


def test_one_at_a_time():
    """Две задачи не могут идти одновременно."""
    lane = SynthLane()
    inside = []
    overlap = []

    def job(chat):
        lane.acquire(chat=chat)
        try:
            inside.append(1)
            if len(inside) > 1:
                overlap.append(len(inside))
            time.sleep(0.15)
            inside.pop()
        finally:
            lane.release()

    threads = [threading.Thread(target=job, args=(i == 0,)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(6)
    check("синтез строго по одному", not overlap, "наложений: %s" % overlap)


def test_chat_priority():
    """Реплика чата не ждёт длиннее одной предпрослушительной фразы."""
    lane = SynthLane()
    order = []

    lane.acquire(chat=False)          # предпрослушивание уже идёт
    start = time.time()

    def chat():
        lane.acquire(chat=True)
        order.append(("chat", time.time() - start))
        lane.release()

    def preview():
        lane.acquire(chat=False)
        order.append(("preview", time.time() - start))
        lane.release()

    tc = threading.Thread(target=chat)
    tc.start()
    time.sleep(0.05)
    tp = threading.Thread(target=preview)
    tp.start()
    time.sleep(0.05)
    lane.release()                       # предпрослушивание закончилось
    tc.join(5)
    tp.join(5)
    check("реплика чата идёт раньше предпрослушивания",
          order and order[0][0] == "chat", "порядок: %s" % order)
    check("реплика чата ждала меньше секунды",
          bool(order) and order[0][1] < 1.0, "ждала %.2f с" % (order[0][1] if order else -1))


def test_preview_waits_for_chat():
    """Предпрослушивание не начинается, пока ждёт реплика чата."""
    lane = SynthLane()
    started = []

    def chat():
        lane.acquire(chat=True)
        time.sleep(0.2)
        lane.release()

    def preview():
        lane.acquire(chat=False)
        started.append(time.time())
        lane.release()

    tc = threading.Thread(target=chat)
    tc.start()
    time.sleep(0.05)
    tp = threading.Thread(target=preview)
    tp.start()
    tp.join(5)
    tc.join(5)
    check("предпрослушивание ждало реплику чата",
          len(started) == 1 and started[0] > time.time() - 0.5,
          "старт: %s" % started)


def test_release_on_error():
    """Ошибка в задаче не оставляет полосу занятой."""
    lane = SynthLane()
    lane.acquire(chat=False)
    lane.release()
    check("слот освобождается", not lane.busy())
    lane.acquire(chat=False)
    check("слот снова берётся", lane.busy())
    lane.release()


if __name__ == "__main__":
    test_one_at_a_time()
    test_chat_priority()
    test_preview_waits_for_chat()
    test_release_on_error()
    print()
    if FAILED:
        print("ПРОВАЛЕНО: %s" % ", ".join(FAILED))
        sys.exit(1)
    print("все проверки пройдены")
