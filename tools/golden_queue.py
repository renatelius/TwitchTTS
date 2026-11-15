# -*- coding: utf-8 -*-
"""Золотой слепок очереди озвучки: порядок, пауза, очистка, пропуск.

Очередь проверяется на живом сервере с заглушкой (TTS_MOCK=1). Чтобы счётчик
был предсказуемым, очередь сначала ставят на паузу: тогда задания копятся и
их видно в состоянии.

    python tools/golden_queue.py --base http://127.0.0.1:8769 --out tools/golden_queue.json
    python tools/golden_queue.py --base http://127.0.0.1:8769 --check tools/golden_queue.json
"""
import argparse
import json
import sys
import time
import urllib.request
from pathlib import Path


def api(base, path, payload=None, method=None):
    """Запрос к API. method выводится из payload, если не задан явно."""
    if method is None:
        method = "POST" if payload is not None else "GET"
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(base.rstrip("/") + path, data=data,
                                 headers={"Content-Type": "application/json"},
                                 method=method)
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def queue_state(base):
    st = api(base, "/api/state")["status"]
    return {"queue": st.get("queue"), "paused": bool(st.get("paused")),
            "now": bool(st.get("now"))}


def wait_for(base, predicate, timeout=90.0, step=""):
    """Ждёт состояния очереди, удовлетворяющего условию.

    Без этого слепок зависел бы от того, успел ли воркер взять задание.
    """
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        last = queue_state(base)
        if predicate(last):
            return last
        time.sleep(0.02)
    raise SystemExit("не дождались состояния «%s»: последнее %r" % (step, last))


def warm_up(base):
    """Прогревает движок: первый синтез поднимает CosyVoice (это не заглушка).

    Пока идёт загрузка модели, очередь ждать нельзя - слепок снимали бы
    разные состояния в зависимости от скорости машины.
    """
    try:
        api(base, "/api/announce", {"text": "разогрев"})
    except Exception as e:
        print("предупреждение: прогрев не удался: %r" % e)


def snapshot(base):
    steps = []
    warm_up(base)

    # приводим очередь в известное состояние
    api(base, "/api/control/resume", {})
    api(base, "/api/control/clear", {})
    wait_for(base, lambda s: s["queue"] == 0 and not s["now"], step="пустая очередь")
    steps.append({"step": "начало", "state": queue_state(base)})

    api(base, "/api/control/pause", {})
    steps.append({"step": "пауза", "state": queue_state(base)})

    for i in range(3):
        api(base, "/api/say", {"text": "очередь %d" % (i + 1)})
    steps.append({"step": "три задания", "state": queue_state(base)})

    api(base, "/api/control/clear", {})
    steps.append({"step": "очистка", "state": queue_state(base)})

    api(base, "/api/say", {"text": "очередь 1"})
    steps.append({"step": "одно задание", "state": queue_state(base)})

    api(base, "/api/control/resume", {})
    wait_for(base, lambda s: s["now"], step="озвучка началась")
    steps.append({"step": "озвучка", "state": queue_state(base)})

    api(base, "/api/control/skip", {})
    wait_for(base, lambda s: not s["now"], step="озвучка остановлена")
    steps.append({"step": "пропуск", "state": queue_state(base)})

    # возвращаем сервер в исходное состояние
    api(base, "/api/control/clear", {})
    api(base, "/api/control/resume", {})
    return {"steps": steps}


def cleanup(base):
    """Оставляет очередь пустой, даже если слепок не удалось снять."""
    try:
        api(base, "/api/control/clear", {})
        api(base, "/api/control/resume", {})
    except Exception:
        pass


def diff(before, after, path=""):
    out = []
    if isinstance(before, dict) and isinstance(after, dict):
        for key in sorted(set(before) | set(after)):
            out += diff(before.get(key), after.get(key), "%s/%s" % (path, key))
    elif isinstance(before, list) and isinstance(after, list):
        if len(before) != len(after):
            out.append("%s: длина %d -> %d" % (path, len(before), len(after)))
        for i, (b, a) in enumerate(zip(before, after)):
            out += diff(b, a, "%s[%d]" % (path, i))
    elif before != after:
        out.append("%s: %r -> %r" % (path, before, after))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8769")
    ap.add_argument("--out")
    ap.add_argument("--check")
    args = ap.parse_args()

    try:
        snap = snapshot(args.base)
    finally:
        cleanup(args.base)
    if args.out:
        Path(args.out).write_text(json.dumps(snap, ensure_ascii=False, indent=2), encoding="utf-8")
        print("слепок очереди сохранён: %s (шагов %d)" % (args.out, len(snap["steps"])))
    if args.check:
        ref = json.loads(Path(args.check).read_text(encoding="utf-8"))
        problems = diff(ref, snap)
        if problems:
            print("РАСХОЖДЕНИЯ с эталоном очереди:")
            for p in problems[:30]:
                print("  -", p)
            return 1
        print("слепок очереди совпадает с эталоном (шагов %d)" % len(snap["steps"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
