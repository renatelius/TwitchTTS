# -*- coding: utf-8 -*-
"""HTTP-слой: страницы, API-эндпоинты, поток событий и отдача звука.

Обработчики только переводят запрос в действие над состоянием аппликации (state). Глобальных переменных в модуле нет.
"""
import asyncio
import json
import logging
import math
import os
import shutil
import sys
import uuid
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

from starlette.responses import FileResponse, JSONResponse, RedirectResponse, StreamingResponse  # noqa: E402
from starlette.routing import Mount, Route  # noqa: E402
from starlette.staticfiles import StaticFiles  # noqa: E402

from paths import CACHE, MAX_UPLOAD, ROOT, TMP, WEB  # noqa: E402
import cosyvoice as cvlib  # noqa: E402
import rvc as rvclib  # noqa: E402
import styles as stylelib  # noqa: E402
from rvc_train import TrainError  # noqa: E402
from textproc import segments  # noqa: E402
from twitch import normalize_channel  # noqa: E402
from voices import VoiceError, measure_f0  # noqa: E402

log = logging.getLogger("api")

def ok(**data):
    return JSONResponse({"ok": True, **data})


def fail(message, code=400):
    return JSONResponse({"ok": False, "error": message}, status_code=code)


async def body(request):
    try:
        data = await request.json()
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


async def page_index(state, request):
    return FileResponse(WEB / "index.html")


async def page_root(state, request):
    """Главная страница теперь - вкладка «Опрос»."""
    return RedirectResponse("/poll", status_code=303)


async def page_voices(state, request):
    return FileResponse(WEB / "voices.html")


async def page_overlay(state, request):
    return FileResponse(WEB / "overlay.html")


async def page_poll(state, request):
    return FileResponse(WEB / "poll.html")


async def page_player(state, request):
    """Плеер озвучки отдельной страницей - для OBS (свой звуковой канал)."""
    return FileResponse(WEB / "player.html")


async def page_brb(state, request):
    """BRB-экран с таймером."""
    return FileResponse(WEB / "brb.html")


async def api_brb(state, request):
    """BRB-экран: GET - текущее состояние, POST - поставить таймер."""
    a = state
    if request.method == "POST":
        data = await body(request)
        try:
            seconds = int(data.get("seconds") or 0)
        except (TypeError, ValueError):
            seconds = 0
        a.set_brb(seconds, str(data.get("message") or ""))
    return ok(brb=a.brb_state())


async def events(state, request):
    a = state
    q = asyncio.Queue(maxsize=1000)
    a.clients.add(q)

    async def stream():
        try:
            yield "retry: 2000\n\n"
            hello = {"type": "hello", "history": list(a.history), "status": a.status()}
            yield "data: %s\n\n" % json.dumps(hello, ensure_ascii=False)
            while True:
                try:
                    ev = await asyncio.wait_for(q.get(), timeout=15)
                    yield "data: %s\n\n" % json.dumps(ev, ensure_ascii=False)
                except asyncio.TimeoutError:
                    yield ": ping\n\n"
        finally:
            a.clients.discard(q)

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


async def api_state(state, request):
    a = state
    return ok(status=a.status(), settings=a.settings, voices=a.voices.list(), brb=a.brb_state())


async def api_settings(state, request):
    a = state
    data = await body(request)
    was_muted = bool(a.settings.get("muted"))
    for key, default in state.defaults.items():
        if key not in data or key in ("channel", "disabled_voices", "applio_path", "cosyvoice_path"):
            continue
        val = data[key]
        try:
            if isinstance(default, bool):
                val = bool(val)
            elif isinstance(default, int):
                val = max(0, min(int(val), 2000))
            elif isinstance(default, float):
                val = float(val)
            elif isinstance(default, list):
                val = [str(x).strip() for x in val if str(x).strip()][:200]
            elif isinstance(default, dict):
                if not isinstance(val, dict):
                    continue
                val = {str(k)[:40]: str(v)[:40] for k, v in list(val.items())[:50]}
            else:
                val = str(val)
        except (TypeError, ValueError):
            continue
        a.settings[key] = val
    a.settings["speed"] = max(0.7, min(a.settings["speed"], 1.5))
    a.settings["volume"] = max(0.0, min(a.settings["volume"], 2.0))
    a.settings["max_len"] = max(20, min(a.settings["max_len"], 500))
    a.settings["max_queue"] = max(1, min(a.settings["max_queue"], 100))
    a.save_settings()
    if a.settings.get("muted") and not was_muted:
        # мьют: замолкаем сразу - текущая реплика и вся очередь
        a.clear()
    a.broadcast({"type": "settings", "settings": a.settings})
    return ok(settings=a.settings)


async def api_connect(state, request):
    a = state
    data = await body(request)
    channel = normalize_channel(data.get("channel", ""))
    if not channel:
        return fail("Введите имя канала, например: mychannel")
    a.settings["channel"] = channel
    a.save_settings()
    await a.twitch.connect(channel)
    return ok(channel=channel)


async def api_disconnect(state, request):
    await state.twitch.disconnect()
    return ok()


async def api_control(state, request):
    a = state
    action = request.path_params["action"]
    if action == "skip":
        a.skip()
    elif action == "clear":
        a.clear()
    elif action == "pause":
        a.paused = True
    elif action == "resume":
        a.paused = False
        a.wake.set()
    else:
        return fail("неизвестная команда", 404)
    a.push_status()
    return ok()


async def api_say(state, request):
    """Проверочная фраза: идёт через общую очередь, как сообщение из чата."""
    a = state
    data = await body(request)
    style_key, raw_text = stylelib.match_style(str(data.get("text", "")))
    text = a.clean(raw_text)
    if not text:
        return fail("Введите текст")
    voice = a.voices.get(str(data.get("voice", ""))) or a.default_voice()
    if not voice:
        return fail("Нет доступных голосов")
    if a.engine.status != "ready":
        return fail("Модель озвучки ещё не готова")
    event = {"type": "chat", "id": uuid.uuid4().hex, "user": "Проверка", "color": "#9aa4b2",
             "text": text, "tts": True, "note": "", "voice": voice["name"]}
    a.broadcast(event)
    a.enqueue(event["id"], "Проверка", text, voice, style_key)
    return ok()


async def api_preview(state, request):
    """Прослушать голос на странице голосов (мимо очереди)."""
    a = state
    data = await body(request)
    voice = a.voices.get(str(data.get("voice", "")))
    text = a.clean(str(data.get("text", "")), None, 200) or "Привет, чат! Это проверка голоса."
    if not voice:
        return fail("Голос не найден", 404)
    if a.engine.status != "ready":
        return fail("Модель озвучки ещё не готова")
    job = {"text": text, "voice_id": voice["id"]}
    try:
        path, _ = await a.loop.run_in_executor(a.executor, a._synth, job)
    except Exception as e:
        log.exception("ошибка прослушивания")
        return fail("Ошибка озвучки: %s" % e, 500)
    return ok(url="/audio/" + path.name)


async def api_announce(state, request):
    """Синтез одной фразы вне очереди - для внешних страниц (например, голосования)."""
    a = state
    data = await body(request)
    text = a.clean(str(data.get("text", "")))
    if not text:
        return fail("Пустой текст")
    voice = a.voices.get(str(data.get("voice", ""))) or a.default_voice()
    if not voice:
        return fail("Нет доступных голосов")
    if a.engine.status != "ready":
        return fail("Модель озвучки ещё не готова", 503)
    job = {"text": text, "voice_id": voice["id"]}
    try:
        path, dur = await a.loop.run_in_executor(a.executor, a._synth, job)
    except Exception as e:
        log.exception("ошибка озвучки")
        return fail("Ошибка озвучки: %s" % e, 500)
    return ok(url="/audio/" + path.name, duration=dur, voice=voice["name"])


async def api_emotes(state, request):
    """Список эмоций, записи в sounds/ и счётчик звуков, сгенерированных голосом."""
    a = state
    return ok(enabled=bool(a.settings["emotes"]), folder=str(a.emotes.root),
              emotes=a.emotes.summary(), generated=dict(a._emote_gen))


async def api_emotes_reset(state, request):
    """Сбрасывает кэш сгенерированных звуков - они будут созданы заново."""
    a = state
    a._emote_cache.clear()
    return ok(generated=dict(a._emote_gen))


async def api_voices(state, request):
    return ok(voices=state.voices.list())


async def api_voice_create(state, request):
    a = state
    work = TMP / uuid.uuid4().hex
    work.mkdir(parents=True)
    try:
        form = await request.form(max_files=40, max_part_size=MAX_UPLOAD)
        name = str(form.get("name", ""))
        aliases = [x for x in str(form.get("aliases", "")).replace(";", ",").split(",")]
        paths, names, total = [], {}, 0
        for n, f in enumerate(form.getlist("files")):
            if not hasattr(f, "read"):
                continue
            ext = os.path.splitext(f.filename or "")[1][:8]
            ext = "".join(c for c in ext if c.isalnum() or c == ".")
            dst = work / ("src_%02d%s" % (n, ext))
            with open(dst, "wb") as out:
                while True:
                    chunk = await f.read(1024 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > MAX_UPLOAD:
                        return fail("Файлы слишком большие (больше 500 МБ).")
                    out.write(chunk)
            paths.append(dst)
            names[str(dst)] = os.path.basename(f.filename or dst.name)[:80]
        if not paths:
            return fail("Добавьте хотя бы один файл с голосом.")
        try:
            a.voices.check_name(name, aliases)
            voice, report = await a.loop.run_in_executor(a.executor, a.voices.create, name, aliases, paths, names)
        except VoiceError as e:
            return fail(str(e))
        except Exception as e:
            log.exception("ошибка создания голоса")
            return fail("Не получилось создать голос: %s" % e, 500)
        a.broadcast({"type": "voices"})
        return ok(voice=voice, report=report)
    finally:
        shutil.rmtree(work, ignore_errors=True)



async def api_voice_pitch(state, request):
    """Сдвиг тона своего голоса (полутона)."""
    a = state
    data = await body(request)
    try:
        voice = a.voices.update_pitch(str(data.get("voice", "")), data.get("pitch", 0))
    except VoiceError as e:
        return fail(str(e))
    except Exception as e:
        log.exception("ошибка сдвига тона")
        return fail("Не удалось изменить тон: %s" % e, 500)
    a.broadcast({"type": "voices"})
    return ok(voice=voice)


async def api_voice_volume(state, request):
    """Своя громкость голоса (проценты, 0-200)."""
    a = state
    data = await body(request)
    try:
        voice = a.voices.update_volume(str(data.get("voice", "")), data.get("volume", 100))
    except VoiceError as e:
        return fail(str(e))
    except Exception as e:
        log.exception("ошибка громкости голоса")
        return fail("Не удалось изменить громкость: %s" % e, 500)
    a.broadcast({"type": "voices"})
    return ok(voice=voice)


async def api_voice_calibrate(state, request):
    """Подстраивает тон голоса под тон записи-эталона.

    XTTS переносит тембр, но не абсолютную высоту: клон почти всегда звучит
    выше или ниже оригинала. Здесь мы синтезируем короткую пробу, измеряем её
    основной тон и вычисляем сдвиг, который вернёт голос к высоте эталона.
    """
    a = state
    data = await body(request)
    voice = a.voices.get(str(data.get("voice", "")))
    if not voice or voice["kind"] != "custom":
        return fail("Подстройка доступна только для своих голосов.")
    ref = float(voice.get("ref_f0") or 0)
    if ref <= 0:
        return fail("Нет данных эталона. Переимпортируйте голос с записями.")
    if a.engine.status != "ready":
        return fail("Модель озвучки ещё не готова", 503)
    probe = "Привет, это проверка тона голоса."
    path = CACHE / ("cal_%s.wav" % uuid.uuid4().hex)
    try:
        segs = segments(probe, a.settings["language"])
        latents = a.voices.latents(voice["id"])
        await a.loop.run_in_executor(a.executor, a.engine.synthesize, segs, latents, path,
                                     a.settings["speed"], 0.0)
        measured = measure_f0(path)
    except Exception as e:
        log.exception("ошибка подстройки тона")
        return fail("Не удалось подобрать тон: %s" % e, 500)
    finally:
        try:
            path.unlink()
        except OSError:
            pass
    if measured <= 0:
        return fail("Не удалось измерить тон пробной записи.")
    steps = 12.0 * math.log2(ref / measured)
    steps = max(-24.0, min(steps, 24.0))
    try:
        updated = a.voices.update_pitch(voice["id"], steps)
    except VoiceError as e:
        return fail(str(e))
    a.broadcast({"type": "voices"})
    return ok(voice=updated, ref_f0=round(ref, 1), measured=round(measured, 1),
              pitch=round(steps, 1),
              note="Тон голоса подстроен под запись образца: сдвиг %+.1f полутона."
                   % steps)


async def api_voice_delete(state, request):
    a = state
    try:
        a.voices.delete(request.path_params["voice_id"])
    except VoiceError as e:
        return fail(str(e))
    a.broadcast({"type": "voices"})
    return ok()


async def api_voice_toggle(state, request):
    """Включить/выключить голос."""
    a = state
    data = await body(request)
    target, enabled = str(data.get("voice", "")), bool(data.get("enabled"))
    ids = [v["id"] for v in a.voices.list() if v["id"] == target]
    disabled = set(a.settings["disabled_voices"])
    for i in ids:
        (disabled.discard if enabled else disabled.add)(i)
    a.settings["disabled_voices"] = sorted(disabled)
    a.save_settings()
    a.broadcast({"type": "voices"})
    return ok()


async def api_cosyvoice_setup(state, request):
    """Указать папку CosyVoice 3 и запустить её."""
    a = state
    data = await body(request)
    path = str(data.get("path", "")).strip().strip('"') or cvlib.find_cosyvoice(ROOT)
    if not path:
        return fail("Укажите папку с CosyVoice 3 (в ней должны быть env и pretrained_models).")
    try:
        cvlib.check_cosyvoice(path)
    except cvlib.CosyVoiceError as e:
        return fail(str(e))
    try:
        a.cosyvoice.start(path)
    except cvlib.CosyVoiceError as e:
        return fail(str(e))
    except Exception as e:
        log.exception("ошибка запуска CosyVoice 3")
        return fail("Не удалось запустить CosyVoice 3: %s" % e, 500)
    a.settings["cosyvoice_path"] = a.cosyvoice.path
    a.save_settings()
    a.push_status()
    return ok(cosyvoice=a.cosyvoice.info())


async def api_voice_engine(state, request):
    """Переключить движок озвучки своего голоса: XTTS или CosyVoice 3."""
    a = state
    data = await body(request)
    try:
        voice = a.voices.set_engine(str(data.get("voice", "")), str(data.get("engine", "xtts")))
    except (VoiceError, ValueError, TypeError) as e:
        return fail(str(e))
    a.broadcast({"type": "voices"})
    return ok(voice=voice)


async def api_voice_cosyvoice(state, request):
    """Подготовить образец CosyVoice 3 для голоса и расшифровать его."""
    a = state
    data = await body(request)
    voice_id = str(data.get("voice", ""))
    try:
        path, text = a.voices.prepare_cosyvoice(voice_id)
    except (VoiceError, OSError) as e:
        return fail(str(e))
    if not text:
        try:
            a.ensure_cosyvoice()
            text = a.cosyvoice.transcribe(path)
        except Exception as e:
            log.warning("не удалось расшифровать образец: %s", e)
            text = ""
        if text:
            try:
                a.voices.set_transcript(voice_id, text)
            except VoiceError:
                pass
    a.broadcast({"type": "voices"})
    return ok(voice=a.voices.get(voice_id), transcript=text)


async def api_voice_transcript(state, request):
    """Сохранить текст образца CosyVoice 3."""
    a = state
    data = await body(request)
    try:
        voice = a.voices.set_transcript(str(data.get("voice", "")), data.get("text", ""))
    except (VoiceError, ValueError, TypeError) as e:
        return fail(str(e))
    a.broadcast({"type": "voices"})
    return ok(voice=voice)


async def api_rvc_setup(state, request):
    """Указать папку Applio и запустить RVC."""
    a = state
    data = await body(request)
    path = str(data.get("path", "")).strip().strip('"') or rvclib.find_applio(ROOT)
    if not path:
        return fail("Укажите папку, в которую установлен Applio, например D:\\Applio")
    try:
        a.rvc.start(path)
    except rvclib.RvcError as e:
        return fail(str(e))
    except Exception as e:
        log.exception("ошибка запуска RVC")
        return fail("Не удалось запустить Applio: %s" % e, 500)
    a.settings["applio_path"] = a.rvc.path
    a.save_settings()
    a.push_status()
    return ok(rvc=a.rvc.info())


def _set_progress(state, text):
    a = state
    a.progress = text
    a.loop.call_soon_threadsafe(a.broadcast, {"type": "progress", "text": text})


async def api_rvc_train(state, request):
    """Обучение RVC-модели из загруженных записей."""
    a = state
    form = await request.form(max_files=40, max_part_size=MAX_UPLOAD)
    name = str(form.get("name", ""))
    aliases = [x.strip() for x in str(form.get("aliases", "")).replace(";", ",").split(",") if x.strip()]
    try:
        epochs = max(10, min(2000, int(str(form.get("epochs", "200")) or 200)))
        batch = max(1, min(32, int(str(form.get("batch", "4")) or 4)))
        sample_rate = int(str(form.get("sample_rate", "40000")) or 40000)
        pitch = int(str(form.get("pitch", "0")) or 0)
    except ValueError:
        return fail("Неверные параметры обучения.")
    base = str(form.get("base", ""))
    work = TMP / ("train_%s" % uuid.uuid4().hex)
    work.mkdir(parents=True, exist_ok=True)
    files, total = [], 0
    for n, f in enumerate(form.getlist("files")):
        if not hasattr(f, "read"):
            continue
        ext = os.path.splitext(f.filename or "")[1][:8]
        ext = "".join(c for c in ext if c.isalnum() or c == ".") or ".wav"
        dst = work / ("src_%02d%s" % (n, ext))
        with open(dst, "wb") as out:
            while True:
                chunk = await f.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_UPLOAD:
                    shutil.rmtree(work, ignore_errors=True)
                    return fail("Файлы слишком большие (больше 500 МБ).")
                out.write(chunk)
        files.append(dst)
    if not files:
        shutil.rmtree(work, ignore_errors=True)
        return fail("Добавьте записи голоса.")
    a.trainer.opts = {"base": base, "pitch": pitch, "epochs": epochs,
                      "batch": batch, "sample_rate": sample_rate}
    a._train_done = False
    try:
        status = a.trainer.start(name, aliases, files, a.trainer.opts)
    except TrainError as e:
        shutil.rmtree(work, ignore_errors=True)
        return fail(str(e))
    return ok(train=status)


async def api_rvc_train_status(state, request):
    """Текущее состояние обучения."""
    return ok(train=state.trainer.status())


async def api_rvc_train_cancel(state, request):
    """Остановить обучение."""
    a = state
    return ok(canceled=a.trainer.cancel())


async def api_rvc_update(state, request):
    a = state
    data = await body(request)
    try:
        voice = a.voices.update_rvc(str(data.get("voice", "")), data.get("base"), data.get("pitch"),
                                    data.get("index_rate"))
    except (VoiceError, ValueError, TypeError) as e:
        return fail(str(e))
    a.broadcast({"type": "voices"})
    return ok(voice=voice)


async def audio(state, request):
    name = request.path_params["name"]
    path = CACHE / name
    if "/" in name or "\\" in name or not name.endswith(".wav") or not path.exists():
        return fail("нет файла", 404)
    return FileResponse(path, media_type="audio/wav", headers={"Cache-Control": "no-store"})


async def api_debug_chat(state, request):
    """Только для проверки с заглушкой: подставить сообщение, как будто оно пришло из чата."""
    d = await body(request)
    state.on_chat({"id": uuid.uuid4().hex, "login": d.get("user", "test").lower(), "name": d.get("user", "test"),
                       "color": d.get("color", ""), "text": d.get("text", ""), "emotes": [],
                       "is_mod": bool(d.get("mod")), "is_broadcaster": False, "is_reward": bool(d.get("reward")),
                       "is_sub": False, "is_vip": False})
    return ok()


def build_routes(state):
    """Собирает маршруты HTTP-слоя. Состояние передаётся обработчикам явно."""
    def bind(handler):
        """Привязывает обработчик к состоянию приложения."""
        async def endpoint(request):
            return await handler(state, request)
        endpoint.__name__ = getattr(handler, "__name__", "endpoint")
        return endpoint

    return [
        Route("/", bind(page_root)),
        Route("/chat", bind(page_index)),
        Route("/voices", bind(page_voices)),
        Route("/overlay", bind(page_overlay)),
        Route("/poll", bind(page_poll)),
        Route("/player", bind(page_player)),
        Route("/brb", bind(page_brb)),
        Route("/events", bind(events)),
        Route("/api/state", bind(api_state)),
        Route("/api/settings", bind(api_settings), methods=["POST"]),
        Route("/api/connect", bind(api_connect), methods=["POST"]),
        Route("/api/disconnect", bind(api_disconnect), methods=["POST"]),
        Route("/api/control/{action}", bind(api_control), methods=["POST"]),
        Route("/api/say", bind(api_say), methods=["POST"]),
        Route("/api/preview", bind(api_preview), methods=["POST"]),
        Route("/api/announce", bind(api_announce), methods=["POST"]),
        Route("/api/brb", bind(api_brb), methods=["GET", "POST"]),
        Route("/api/emotes", bind(api_emotes), methods=["GET"]),
        Route("/api/emotes/reset", bind(api_emotes_reset), methods=["POST"]),
        Route("/api/voices", bind(api_voices), methods=["GET"]),
        Route("/api/voices", bind(api_voice_create), methods=["POST"]),
        Route("/api/voices/toggle", bind(api_voice_toggle), methods=["POST"]),
        Route("/api/rvc/setup", bind(api_rvc_setup), methods=["POST"]),
        Route("/api/cosyvoice/setup", bind(api_cosyvoice_setup), methods=["POST"]),
        Route("/api/voice/engine", bind(api_voice_engine), methods=["POST"]),
        Route("/api/voice/cosyvoice", bind(api_voice_cosyvoice), methods=["POST"]),
        Route("/api/voice/transcript", bind(api_voice_transcript), methods=["POST"]),
        Route("/api/rvc/update", bind(api_rvc_update), methods=["POST"]),
        Route("/api/rvc/train", bind(api_rvc_train), methods=["POST"]),
        Route("/api/rvc/train/status", bind(api_rvc_train_status), methods=["GET"]),
        Route("/api/rvc/train/cancel", bind(api_rvc_train_cancel), methods=["POST"]),
        Route("/api/voices/{voice_id:path}", bind(api_voice_delete), methods=["DELETE"]),
        Route("/api/voice/pitch", bind(api_voice_pitch), methods=["POST"]),
        Route("/api/voice/volume", bind(api_voice_volume), methods=["POST"]),
        Route("/api/voice/calibrate", bind(api_voice_calibrate), methods=["POST"]),
        Route("/audio/{name}", bind(audio)),
        # тестовое подключение сообщения из чата - включается переменной окружения
        *([Route("/api/debug/chat", bind(api_debug_chat), methods=["POST"])] if os.environ.get("TTS_DEBUG_CHAT") == "1" else []),
        Mount("/static", StaticFiles(directory=WEB), name="static"),
    ]
