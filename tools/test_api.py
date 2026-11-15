# -*- coding: utf-8 -*-
"""Проверки HTTP-слоя после переноса обработчиков в app/api.py.

Проверяем три вещи:
- список маршрутов не изменился (сверка с золотым слепком);
- модуль api.py не зависит от внутренностей main.py (связность по шву);
- обработчики и маршруты собираются и отвечают ожидаемым форматом.
"""
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP = ROOT / "app"
if str(ROOT / "tools") not in sys.path:
    sys.path.insert(0, str(ROOT / "tools"))

import golden_api  # noqa: E402

CHECKS = []


def check(name, ok, detail=""):
    CHECKS.append(bool(ok))
    print("%-52s %s%s" % (name, "OK" if ok else "ПРОВАЛ",
                          (" | " + str(detail)) if detail else ""))


# ---------- маршруты ----------
routes = golden_api.routes_snapshot()
ref_path = ROOT / "tools" / "golden_api.json"
ref = json.loads(ref_path.read_text(encoding="utf-8")) if ref_path.exists() else {"routes": []}
check("маршруты совпадают с золотым слепком", routes == ref.get("routes"),
      "%d маршрутов" % len(routes))
paths = [r["path"] for r in routes]
for need in ("/", "/poll", "/chat", "/voices", "/overlay", "/player", "/brb",
             "/events", "/api/state", "/api/settings", "/api/say", "/api/brb",
             "/api/voices", "/audio/{name}", "/static"):
    check("маршрут %s есть" % need, need in paths)

# ---------- связность модулей ----------
api_src = (APP / "api.py").read_text(encoding="utf-8")
main_src = (APP / "main.py").read_text(encoding="utf-8")
check("api.py не ссылается на app_state", "app_state" not in api_src)
check("api.py не импортирует main", "import main" not in api_src)
check("api.py не импортирует app.main", "from app import" not in api_src)
check("в main.py нет HTTP-обработчиков", "async def api_" not in main_src)
check("в main.py нет функций страниц", "async def page_" not in main_src)
check("маршруты собирает api.build_routes", "apilib.build_routes(app_state)" in main_src)
check("логгер в main.py есть", "getLogger(\"main\")" in main_src)
check("логгер в api.py есть", "getLogger(\"api\")" in api_src)

# ---------- поведение модуля ----------
os.environ.setdefault("TTS_MOCK", "1")
os.environ.pop("TTS_DEBUG_CHAT", None)
if str(APP) not in sys.path:
    sys.path.insert(0, str(APP))
import api as apilib  # noqa: E402

resp = apilib.ok(значение=1)
data = json.loads(resp.body)
check("ok() отдаёт ok=True", data.get("ok") is True, resp.status_code)
check("ok() возвращает переданные поля", data.get("значение") == 1)

resp = apilib.fail("нет такого", 404)
data = json.loads(resp.body)
check("fail() отдаёт ok=False", data.get("ok") is False, resp.status_code)
check("fail() возвращает код", resp.status_code == 404)
check("fail() возвращает текст ошибки", data.get("error") == "нет такого")

built = apilib.build_routes(object())
check("build_routes отдаёт маршруты", len(built) == len(routes), len(built))
names = [getattr(r.endpoint, "__name__", "") for r in built if getattr(r, "endpoint", None)]
check("обработчики сохраняют имена", "api_state" in names and "events" in names)

# состояние передаётся обработчику явно: подмена состояния не ломает сборку
class Fake:
    def __init__(self):
        self.calls = []

fake = Fake()
built2 = apilib.build_routes(fake)
check("build_routes принимает состояние", len(built2) == len(routes))
check("маршруты не делят глобальное состояние",
      getattr(apilib, "app_state", None) is None)

# отладочный маршрут включается переменной окружения
os.environ["TTS_DEBUG_CHAT"] = "1"
debug_paths = [getattr(r, "path", "") for r in apilib.build_routes(object())]
check("отладочный маршрут появляется с TTS_DEBUG_CHAT=1",
      "/api/debug/chat" in debug_paths)
os.environ.pop("TTS_DEBUG_CHAT", None)
no_debug = [getattr(r, "path", "") for r in apilib.build_routes(object())]
check("без переменной отладочного маршрута нет",
      "/api/debug/chat" not in no_debug)

failed = [i for i, ok in enumerate(CHECKS) if not ok]
print("")
print("Провалено: %d" % len(failed))
sys.exit(1 if failed else 0)
