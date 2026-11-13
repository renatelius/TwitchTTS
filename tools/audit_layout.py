# -*- coding: utf-8 -*-
"""Аудит вёрстки TwitchTTS: переполнения, обрезание, горизонтальный скролл.

Запуск (при запущенном приложении):
    python tools/audit_layout.py
Отчёт и скриншоты складываются в tools/audit_out/.
"""
import io, json, os, sys
from playwright.sync_api import sync_playwright

BASE = os.environ.get("TTS_URL", "http://127.0.0.1:8765")
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "audit_out")
SHOTS = os.path.join(OUT, "shots")
os.makedirs(SHOTS, exist_ok=True)

VIEWPORTS = [(1920, 1080), (1600, 900), (1366, 768), (1280, 800), (1024, 768), (860, 900), (640, 900)]
PAGES = [
    ("poll", "/poll", ["poll", "chat", "voice"]),
    ("chat", "/chat", [None]),
    ("voices", "/voices", [None]),
    ("overlay", "/overlay", [None]),
]

JS = r"""
() => {
  const out = {docW: document.documentElement.scrollWidth, vw: window.innerWidth, wide: [], inner: [], clipped: [], tiny: []};
  function sig(el) {
    let s = el.tagName.toLowerCase();
    if (el.id) s += '#' + el.id;
    let cn = (el.className && typeof el.className === 'string') ? el.className.trim() : '';
    if (cn) s += '.' + cn.split(/\s+/).slice(0, 3).join('.');
    return s;
  }
  const vw = window.innerWidth;
  document.querySelectorAll('body *').forEach(el => {
    const cs = getComputedStyle(el);
    if (cs.display === 'none' || cs.visibility === 'hidden') return;
    const r = el.getBoundingClientRect();
    if (r.width === 0 && r.height === 0) return;
    if (r.right > vw + 1 || r.left < -1) out.wide.push({sel: sig(el), l: Math.round(r.left), r: Math.round(r.right), w: Math.round(r.width)});
    if (el.scrollWidth - el.clientWidth > 2 && cs.overflowX !== 'visible' && el.clientWidth > 0)
      out.inner.push({sel: sig(el), sw: el.scrollWidth, cw: el.clientWidth, ox: cs.overflowX});
    if (el.scrollHeight - el.clientHeight > 4 && cs.overflowY !== 'visible' && el.clientHeight > 0)
      out.clipped.push({sel: sig(el), sh: el.scrollHeight, ch: el.clientHeight, oy: cs.overflowY});
    const fs = parseFloat(cs.fontSize);
    if (el.children.length === 0 && el.textContent.trim() && fs < 12) out.tiny.push({sel: sig(el), fs: fs, t: el.textContent.trim().slice(0, 30)});
  });
  out.hscroll = out.docW - out.vw > 1;
  return out;
}
"""

MEASURE = r"""
(sels) => {
  const res = {};
  sels.forEach(s => {
    const el = document.querySelector(s);
    if (!el) { res[s] = null; return; }
    const r = el.getBoundingClientRect();
    res[s] = {w: Math.round(r.width), h: Math.round(r.height), left: Math.round(r.left), right: Math.round(r.right),
              sw: el.scrollWidth, cw: el.clientWidth};
  });
  return res;
}
"""

def dedupe(items, key="sel", limit=12):
    seen, res = set(), []
    for it in items:
        k = it.get(key)
        if k in seen:
            continue
        seen.add(k)
        res.append(it)
        if len(res) >= limit:
            break
    return res

report, errors = {}, []

with sync_playwright() as p:
    browser = p.chromium.launch(channel="msedge", headless=True)
    for name, path, tabs in PAGES:
        for vw, vh in VIEWPORTS:
            ctx = browser.new_context(viewport={"width": vw, "height": vh}, device_scale_factor=1)
            page = ctx.new_page()
            msgs = []
            page.on("console", lambda m: msgs.append(m.type + ": " + m.text) if m.type in ("error", "warning") else None)
            page.on("pageerror", lambda e: msgs.append("pageerror: " + str(e)))
            try:
                page.goto(BASE + path, wait_until="load", timeout=20000)
                page.wait_for_timeout(1000)
                try:
                    page.click("#unlockBtn", timeout=2500)
                    page.wait_for_timeout(350)
                except Exception:
                    pass
                for tab in tabs:
                    if tab:
                        page.click('button.tab[data-tab="' + tab + '"]')
                        page.wait_for_timeout(900)
                        if tab == "chat":
                            try:
                                page.frame_locator("#chatFrame").locator("#unlockBtn").click(timeout=2500)
                                page.wait_for_timeout(400)
                            except Exception:
                                pass
                    key = "%s@%dx%d%s" % (name, vw, vh, (":" + tab) if tab else "")
                    res = page.evaluate(JS)
                    meas = {}
                    if name == "chat":
                        meas = page.evaluate(MEASURE, ["aside.side", ".now-card", ".btn-row", "#pauseBtn", "#skipBtn", "#clearBtn", "#testText", "#channel", "#connectBtn"])
                    elif name == "poll":
                        meas = page.evaluate(MEASURE, [".app", ".left", ".right", ".stage", ".rstats", ".radarwrap", ".lists", ".chat-embed"])
                    report[key] = {
                        "hscroll": res["hscroll"], "docW": res["docW"], "vw": res["vw"],
                        "wide": dedupe(res["wide"]), "inner": dedupe(res["inner"]),
                        "clipped": dedupe(res["clipped"]), "tiny": dedupe(res["tiny"], limit=6),
                        "measure": meas,
                    }
                    tag = ("-" + tab) if tab else ""
                    if vw in (1920, 1366, 640):
                        page.screenshot(path=os.path.join(SHOTS, "%s-%dx%d%s.png" % (name, vw, vh, tag)), full_page=False)
            except Exception as ex:
                errors.append("%s@%dx%d: %r" % (name, vw, vh, ex))
            if msgs:
                errors.append("%s@%dx%d console: %s" % (name, vw, vh, "; ".join(msgs[:6])))
            ctx.close()
    browser.close()

with io.open(os.path.join(OUT, "report.json"), "w", encoding="utf-8") as f:
    json.dump(report, f, ensure_ascii=False, indent=1)

lines = []
for k, v in report.items():
    lines.append("%-30s hscroll=%-6s wide=%-3d inner=%-3d clipped=%-3d" % (
        k, v["hscroll"], len(v["wide"]), len(v["inner"]), len(v["clipped"])))
text = chr(10).join(lines) + chr(10) + chr(10) + "ERRORS:" + chr(10) + (chr(10).join(errors) if errors else "нет")
sys.stdout.reconfigure(encoding="utf-8")
print(text)
