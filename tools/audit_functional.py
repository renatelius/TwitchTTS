# -*- coding: utf-8 -*-
"""Функциональная проверка интерфейса: клики, API, диалоги, автономный режим.

Запуск (при запущенном приложении):
    python tools/audit_functional.py
"""
import io, os, sys, json
from playwright.sync_api import sync_playwright

BASE = os.environ.get("TTS_URL", "http://127.0.0.1:8765")
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "audit_out")
SHOTS = os.path.join(OUT, "shots")
res = {}
errors = []

def check(name, cond, detail=""):
    res[name] = ("OK" if cond else "FAIL") + ((" | " + detail) if detail else "")

with sync_playwright() as p:
    b = p.chromium.launch(channel="msedge", headless=True)

    # --- 1. Опрос: тестовые голоса и полосы ---
    ctx = b.new_context(viewport={"width": 1600, "height": 950})
    pg = ctx.new_page()
    errs = []
    pg.on("pageerror", lambda e: errs.append(str(e)))
    pg.goto(BASE + "/poll", wait_until="load")
    pg.wait_for_timeout(900)
    # состояние голосования живёт в самой странице, эфир не затрагивается
    pg.click("#startBtn"); pg.wait_for_timeout(800)
    running = pg.inner_text("#startBtn")
    check("опрос/запуск голосования", "досрочно" in running, running)
    pg.click('button[data-fake="1"]'); pg.click('button[data-fake="1"]'); pg.click('button[data-fake="2"]')
    pg.wait_for_timeout(700)
    v1 = pg.inner_text(".bars .bar:nth-child(1) .val")
    v2 = pg.inner_text(".bars .bar:nth-child(2) .val")
    w1 = pg.eval_on_selector(".bars .bar:nth-child(1) .fill", "e => e.style.width")
    check("опрос/тестовые голоса", v1.startswith("2") and v2.startswith("1"), "вариант1=%s вариант2=%s" % (v1, v2))
    check("опрос/полоса заполняется", w1.startswith("66"), "width=" + w1)
    head = pg.inner_text("#headline")
    check("опрос/счётчик в заголовке", "3" in head, head)
    pg.screenshot(path=os.path.join(SHOTS, "fn-poll-votes.png"))
    pg.click("#startBtn"); pg.wait_for_timeout(700)
    win = pg.eval_on_selector(".bars .bar:nth-child(1)", "e => e.className")
    done_head = pg.inner_text("#headline")
    check("опрос/итоги и победитель", "win" in win and "Победил" in done_head, done_head[:70])
    pg.screenshot(path=os.path.join(SHOTS, "fn-poll-done.png"))

    # сворачивание настроек
    pg.click("#compactBtn"); pg.wait_for_timeout(400)
    left_hidden = pg.eval_on_selector(".left", "e => getComputedStyle(e).display")
    check("опрос/сворачивание настроек", left_hidden == "none", "display=" + left_hidden)
    pg.keyboard.press("Escape"); pg.wait_for_timeout(400)
    left_back = pg.eval_on_selector(".left", "e => getComputedStyle(e).display")
    check("опрос/возврат настроек по Escape", left_back != "none", "display=" + left_back)

    # вкладки
    for t in ("chat", "voice", "poll"):
        pg.click('button.tab[data-tab="%s"]' % t); pg.wait_for_timeout(500)
    body_cls = pg.get_attribute("body", "class")
    check("опрос/вкладки", "tab-poll" in body_cls, body_cls)
    check("опрос/нет ошибок JS", not errs, "; ".join(errs[:3]))
    ctx.close()

    # --- 2. Чат: диалог настроек и кнопки ---
    ctx = b.new_context(viewport={"width": 1600, "height": 950})
    pg = ctx.new_page()
    errs = []
    pg.on("pageerror", lambda e: errs.append(str(e)))
    pg.goto(BASE + "/chat", wait_until="load")
    pg.wait_for_timeout(900)
    try:
        pg.click("#unlockBtn", timeout=2000)
    except Exception:
        pass
    pg.click("#settingsBtn"); pg.wait_for_timeout(500)
    groups = pg.eval_on_selector_all("#dlg .settings-group .group-title", "els => els.map(e => e.textContent)")
    check("чат/диалог настроек", pg.eval_on_selector("#dlg", "e => e.open") and len(groups) == 6 and "Эмоции" in groups and "Текст и действия" in groups and "События и интеграции" in groups, str(groups))
    pg.click("#dlgCancel"); pg.wait_for_timeout(300)
    pg.click("#pauseBtn"); pg.wait_for_timeout(800)
    t = pg.inner_text("#pauseBtn")
    check("чат/пауза", t in ("Пауза", "Продолжить"), t)
    pg.click("#pauseBtn"); pg.wait_for_timeout(500)
    w = pg.eval_on_selector(".btn-row", "e => e.scrollWidth - e.clientWidth")
    check("чат/ряд кнопок без обрезки", w <= 0, "overflow=" + str(w))
    tw = pg.eval_on_selector("#testText", "e => e.scrollWidth - e.clientWidth")
    check("чат/поле теста не обрезано", tw <= 0, "overflow=" + str(tw))
    pg.screenshot(path=os.path.join(SHOTS, "fn-chat.png"))
    check("чат/нет ошибок JS", not errs, "; ".join(errs[:3]))
    ctx.close()

    # --- 3. Голоса: переключатель движка (запрос перехватываем, модель не грузим) ---
    ctx = b.new_context(viewport={"width": 1600, "height": 950})
    pg = ctx.new_page()
    errs = []
    pg.on("pageerror", lambda e: errs.append(str(e)))
    calls = []
    def fake(route):
        calls.append(json.loads(route.request.post_data or "{}"))
        route.fulfill(status=200, content_type="application/json", body=json.dumps({"ok": True}))
    pg.route("**/api/voice/engine", fake)
    pg.goto(BASE + "/voices", wait_until="load")
    pg.wait_for_timeout(1200)
    n = pg.eval_on_selector_all(".voice", "els => els.length")
    seg = pg.eval_on_selector_all(".voice .seg button", "els => els.map(e => e.textContent)")
    pg.eval_on_selector(".voice .seg button:nth-child(2)", "e => e.click()")
    pg.wait_for_timeout(900)
    check("голоса/переключатель движка", len(calls) == 1 and calls[0].get("engine") == "cosy", str(calls))
    check("голоса/список голосов", n >= 5, "голосов=" + str(n))
    check("голоса/сегмент движка", seg[:2] == ["XTTS", "CosyVoice 3"], str(seg[:2]))
    check("голоса/нет ошибок JS", not errs, "; ".join(errs[:3]))
    ctx.close()

    # --- 4. Автономная копия опроса с диска ---
    ctx = b.new_context(viewport={"width": 1366, "height": 900})
    pg = ctx.new_page()
    errs = []
    pg.on("pageerror", lambda e: errs.append(str(e)))
    pg.goto("file:///B:/projects/TwitchTTS/TwitchTTS/twitch-poll.html", wait_until="load")
    pg.wait_for_timeout(1500)
    w = pg.evaluate("document.documentElement.scrollWidth - document.documentElement.clientWidth")
    styled = pg.eval_on_selector("body", "e => getComputedStyle(e).backgroundColor")
    check("автономная копия/без горизонтального скролла", w <= 0, "overflow=" + str(w))
    check("автономная копия/стили применены", styled == "rgb(11, 12, 16)", styled)
    pg.screenshot(path=os.path.join(SHOTS, "fn-standalone.png"))
    ctx.close()
    b.close()

lines = []
for k, v in res.items():
    lines.append("%-46s %s" % (k, v))
text = chr(10).join(lines) + chr(10) + chr(10) + "Провалено: " + str(sum(1 for v in res.values() if v.startswith("FAIL")))
import sys
sys.stdout.reconfigure(encoding="utf-8")
print(text)
