# -*- coding: utf-8 -*-
"""Проверка связности модулей: циклов импортов быть не должно.

Цикл textproc <-> emotes появился из-за отложенных импортов: чистка текста
брала разбор ремарок из emotes, а звук эмоций - сравнение имён из textproc.
Теперь эти зависимости лежат в листовых модулях emote_text и textkeys.

Проверяем:
- в графе импортов app/ нет циклов;
- textproc не тянет emotes, emotes не тянет textproc (и наоборот);
- листовые модули не зависят ни от чего своего;
- все модули импортируются по отдельности.
"""
import ast
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP = ROOT / "app"

FAILED = []


def check(name, ok, detail=""):
    if not ok:
        FAILED.append(name)
    print("%-58s %s%s" % (name, "OK" if ok else "FAIL", (" | " + str(detail)) if detail else ""))


# ---------- граф импортов ----------
LOCAL = {p.stem for p in APP.glob("*.py")}
EDGES = {}
for path in sorted(APP.glob("*.py")):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    deps = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                if root in LOCAL:
                    deps.add(root)
        elif isinstance(node, ast.ImportFrom) and node.module:
            root = node.module.split(".")[0]
            if root in LOCAL:
                deps.add(root)
    EDGES[path.stem] = deps


def find_cycle(start, path, seen):
    for nxt in EDGES.get(start, ()):  # noqa: E501
        if nxt in path:
            return path[path.index(nxt):] + [nxt]
        if nxt in seen:
            continue
        seen.add(nxt)
        found = find_cycle(nxt, path + [nxt], seen)
        if found:
            return found
    return None


cycles = []
for name in sorted(EDGES):
    found = find_cycle(name, [name], set())
    if found:
        cycles.append(found)
check("в графе импортов нет циклов", not cycles, cycles[:3])

# ---------- конкретные связи ----------
check("textproc не импортирует emotes", "emotes" not in EDGES.get("textproc", set()),
      sorted(EDGES.get("textproc", set())))
check("emotes не импортирует textproc", "textproc" not in EDGES.get("emotes", set()),
      sorted(EDGES.get("emotes", set())))
check("textproc зависит только от листовых модулей",
      EDGES.get("textproc", set()) <= {"emote_text", "langs", "textkeys"},
      sorted(EDGES.get("textproc", set())))
check("emote_text - лист", not EDGES.get("emote_text", set()),
      sorted(EDGES.get("emote_text", set())))
check("textkeys - лист", not EDGES.get("textkeys", set()),
      sorted(EDGES.get("textkeys", set())))
check("voices берёт norm_key из textkeys", "textkeys" in EDGES.get("voices", set()),
      sorted(EDGES.get("voices", set())))

# norm_key живёт в textkeys, а не в textproc
textkeys_src = (APP / "textkeys.py").read_text(encoding="utf-8")
textproc_src = (APP / "textproc.py").read_text(encoding="utf-8")
check("norm_key определён в textkeys", "def norm_key(" in textkeys_src)
check("в textproc norm_key больше не определён", "def norm_key(" not in textproc_src)
check("textproc не импортирует emotes отложенно",
      "from emotes import" not in textproc_src)
check("emotes не импортирует textproc отложенно",
      "from textproc import" not in (APP / "emotes.py").read_text(encoding="utf-8"))

# ---------- импорт по отдельности ----------
def run(code):
    env = dict(os.environ)
    env["TTS_MOCK"] = "1"
    env["PYTHONPATH"] = str(APP)
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True,
                          text=True, env=env, timeout=180)
    return proc.returncode, (proc.stdout or "").strip(), (proc.stderr or "").strip()


code, out, err = run("import sys, textproc; print('emotes' in sys.modules)")
check("textproc импортируется без emotes", code == 0 and out == "False", err[-200:])

code, out, err = run("import sys, emotes; print('textproc' in sys.modules)")
check("emotes импортируется без textproc", code == 0 and out == "False", err[-200:])

code, out, err = run("import sys, emote_text, textkeys; print('ok')")
check("листовые модули импортируются", code == 0 and out == "ok", err[-200:])

# скрипты-точки входа импортировать нельзя: download_model качает модель,
# а rvc_worker и cosyvoice_worker перенаправляют stdout под свой протокол.
# Их проверяем компиляцией.
ENTRY_SCRIPTS = ("download_model", "rvc_worker", "cosyvoice_worker")
for name in ENTRY_SCRIPTS:
    proc = subprocess.run([sys.executable, "-m", "py_compile", str(APP / (name + ".py"))],
                          capture_output=True, text=True, timeout=120)
    check("скрипт %s компилируется" % name, proc.returncode == 0, proc.stderr[-200:])

modules = sorted(p.stem for p in APP.glob("*.py")
                  if p.stem not in ("__init__",) + ENTRY_SCRIPTS)
code, out, err = run("import %s; print('ok')" % ", ".join(modules))
check("все модули app/ импортируются вместе", code == 0 and out.endswith("ok"), err[-300:])

# ---------- неопределённые имена (бывшие "NameError в рантайме") ----------
import builtins

BUILTINS = set(dir(builtins)) | {"__file__", "__name__", "__doc__", "__package__", "__spec__"}


def bound_names(tree):
    """Все имена, которые где-либо объявляются в модуле."""
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.asname or alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                names.add(alias.asname or alias.name)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            names.add(node.id)
        elif isinstance(node, ast.arg):
            names.add(node.arg)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            names.add(node.name)
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            names.update(node.names)
    return names


def undefined_names(path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    bound = bound_names(tree)
    missing = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            if node.id not in bound and node.id not in BUILTINS:
                missing.add(node.id)
    return sorted(missing)


for path in sorted(APP.glob("*.py")):
    if path.stem == "download_model":
        continue
    missing = undefined_names(path)
    check("в %s нет неопределённых имён" % path.name, not missing, missing)

print("")
print("Провалено: %d" % len(FAILED))
sys.exit(1 if FAILED else 0)
