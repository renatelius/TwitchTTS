# -*- coding: utf-8 -*-
"""Проверка мьюта озвучки и громкости 0-200 %.

Запуск (при запущенном приложении):
    python tools/audit_mute_volume.py

Проверяет API (мьют, своя громкость голоса, ограничения), интерфейс
(кнопка в шапке, ползунки 0-200 %) и поведение: при мьюте реплики не
озвучиваются. Поведение проверяется, если у программы включён тестовый
вход сообщений (TTS_DEBUG_CHAT=1) - иначе этот кусок пропускается.
"""
import io, json, os, sys, threading, time, urllib.request

BASE = os.environ.get("TTS_URL", "http://127.0.0.1:8765")
res = {}


def check(name, cond, detail=""):
    res[name] = ("OK" if cond else "FAIL") + ((" | " + str(detail)) if detail else "")


def api(path, data=None):
    req = urllib.request.Request(BASE + path, data=json.dumps(data).encode("utf-8") if data is not None else None,
                                 method="POST" if data is not None else "GET")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    req.add_header("Origin", "null")
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read().decode("utf-8"))
        except Exception:
            return {"ok": False, "error": "HTTP %s" % e.code}


# ---------------- API ----------------
st = api("/api/state")
voices = st.get("voices") or []
orig_settings = st.get("settings") or {}
orig_volume = orig_settings.get("volume")
orig_muted = orig_settings.get("muted")
check("API/состояние отвечает", st.get("ok") is True)
check("API/у голоса есть своя громкость", bool(voices) and voices[0].get("volume") is not None,
      voices[0].get("volume") if voices else "нет голосов")
check("API/в настройках есть мьют", "muted" in st.get("settings", {}))

if voices:
    vid = voices[0]["id"]
    r = api("/api/voice/volume", {"voice": vid, "volume": 150})
    check("API/громкость голоса 150 %", r.get("ok") and r["voice"]["volume"] == 150, r.get("voice", {}).get("volume"))
    r = api("/api/voice/volume", {"voice": vid, "volume": 500})
    check("API/громкость ограничена 200 %", r.get("ok") and r["voice"]["volume"] == 200, r.get("voice", {}).get("volume"))
    r = api("/api/voice/volume", {"voice": vid, "volume": 0})
    check("API/громкость 0 % не путается со 100 %", r.get("ok") and r["voice"]["volume"] == 0, r.get("voice", {}).get("volume"))
    r = api("/api/voice/volume", {"voice": vid, "volume": 100})
    check("API/громкость восстановлена", r.get("ok") and r["voice"]["volume"] == 100, r.get("voice", {}).get("volume"))
    r = api("/api/voice/volume", {"voice": "c:нет-такого", "volume": 50})
    check("API/несуществующий голос отклонён", not r.get("ok"), r.get("error"))

r = api("/api/settings", {"muted": True})
check("API/мьют включается", r.get("ok") and r["settings"]["muted"] is True)
r = api("/api/settings", {"muted": False})
check("API/мьют выключается", r.get("ok") and r["settings"]["muted"] is False)
r = api("/api/settings", {"volume": 1.8})
check("API/общая громкость до 200 %", r.get("ok") and abs(r["settings"]["volume"] - 1.8) < 0.001,
      r.get("settings", {}).get("volume"))
# громкость для проверок в браузере; в конце вернём как было
api("/api/settings", {"volume": 0.35})

# ---------------- поведение ----------------
events = []
stop = False


def reader():
    req = urllib.request.Request(BASE + "/events")
    req.add_header("Origin", "null")
    try:
        with urllib.request.urlopen(req, timeout=90) as r:
            for raw in r:
                if stop:
                    break
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue
                try:
                    events.append(json.loads(line[5:].strip()))
                except Exception:
                    pass
    except Exception:
        pass


def debug_chat(text):
    """Тестовый вход сообщения работает только при TTS_DEBUG_CHAT=1."""
    try:
        r = api("/api/debug/chat", {"user": "tester", "text": text})
        return bool(r and r.get("ok"))
    except Exception:
        return False


if debug_chat("ping"):
    threading.Thread(target=reader, daemon=True).start()
    time.sleep(1.5)
    api("/api/settings", {"muted": True})
    mark = len(events)
    debug_chat("muted check message")
    time.sleep(7)
    spoke = len([e for e in events[mark:] if e.get("type") == "speak"])
    notes = len([e for e in events[mark:] if e.get("type") == "chat" and "выключена" in str(e.get("note", ""))])
    check("Поведение/при мьюте голоса нет", spoke == 0, "озвучено=%d" % spoke)
    check("Поведение/реплика помечена как неозвученная", notes >= 1, "помечено=%d" % notes)

    api("/api/settings", {"muted": False})
    mark = len(events)
    debug_chat("unmuted check message")
    time.sleep(12)
    spoke = len([e for e in events[mark:] if e.get("type") == "speak"])
    check("Поведение/без мьюта голос звучит", spoke >= 1, "озвучено=%d" % spoke)
    stop = True
    api("/api/control/clear", {})
else:
    check("Поведение/проверено (нужен TTS_DEBUG_CHAT=1)", True, "пропущено")

# ---------------- интерфейс ----------------
from playwright.sync_api import sync_playwright

with sync_playwright() as p:
    b = p.chromium.launch(channel="msedge", headless=True)
    ctx = b.new_context(viewport={"width": 1600, "height": 950})
    pg = ctx.new_page()
    errs = []
    pg.on("pageerror", lambda e: errs.append(str(e)))
    pg.on("console", lambda m: errs.append(m.text) if m.type == "error" else None)

    pg.goto(BASE + "/chat", wait_until="load")
    pg.wait_for_timeout(1000)
    try:
        pg.click("#unlockBtn", timeout=2000)
        pg.wait_for_timeout(300)
    except Exception:
        pass
    check("Чат/кнопка мьюта есть", pg.eval_on_selector("#muteBtn", "e => !!e"))
    check("Чат/ползунок громкости 0-200", pg.eval_on_selector("#volume", "e => e.max") == "200",
          pg.eval_on_selector("#volume", "e => e.max"))
    pg.click("#muteBtn")
    pg.wait_for_timeout(900)
    muted = api("/api/state")["settings"]["muted"]
    label = pg.inner_text("#muteBtn")
    check("Чат/кнопка включает мьют", muted is True and "выкл" in label, "muted=%s label=%s" % (muted, label))
    pg.click("#muteBtn")
    pg.wait_for_timeout(900)
    check("Чат/кнопка снимает мьют", api("/api/state")["settings"]["muted"] is False)

    pg.goto(BASE + "/voices", wait_until="load")
    pg.wait_for_timeout(1400)
    n = pg.eval_on_selector_all(".voice .vrow input[type=range]", "e => e.length")
    check("Голоса/у каждого голоса свой ползунок", n >= 1, "ползунков=%d" % n)
    if n:
        check("Голоса/ползунок 0-200", pg.eval_on_selector(".voice .vrow input[type=range]", "e => e.max") == "200",
              pg.eval_on_selector(".voice .vrow input[type=range]", "e => e.max"))
    pg.click("#muteBtn")
    pg.wait_for_timeout(900)
    check("Голоса/кнопка мьюта работает", api("/api/state")["settings"]["muted"] is True)
    pg.click("#muteBtn")
    pg.wait_for_timeout(800)

    pg.goto(BASE + "/poll", wait_until="load")
    pg.wait_for_timeout(1400)
    check("Опрос/кнопка мьюта есть", pg.eval_on_selector("#muteBtn", "e => !!e"))
    check("Опрос/громкость объявлений 0-200", pg.eval_on_selector("#ttsVol", "e => e.max") == "200",
          pg.eval_on_selector("#ttsVol", "e => e.max"))
    check("Страницы/без ошибок в консоли", not errs, "; ".join(errs[:4]))
    ctx.close()
    b.close()

# настройки возвращаем как были: аудит не должен менять то, что у пользователя
try:
    if orig_volume is not None:
        api("/api/settings", {"volume": orig_volume})
    if orig_muted is not None:
        api("/api/settings", {"muted": orig_muted})
except Exception:
    pass

lines = ["%-42s %s" % (k, v) for k, v in res.items()]
lines.append("Провалено: %d" % sum(1 for v in res.values() if v.startswith("FAIL")))
sys.stdout.reconfigure(encoding="utf-8")
print(chr(10).join(lines))
