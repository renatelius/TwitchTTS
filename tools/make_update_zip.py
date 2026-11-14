# -*- coding: utf-8 -*-
"""Сборка архива обновления TwitchTTS-updated.zip.

Скрипт:
1. дописывает в "ОБНОВЛЕНИЕ - прочти меня.txt" описание новых функций (если ещё нет);
2. собирает архив из того же состава файлов, что и предыдущие сборки;
3. проверяет целостность архива и печатает отчёт.
"""
import os
import sys
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.abspath(os.path.join(ROOT, os.pardir, "TwitchTTS-updated.zip"))

FILES = [
    "ARCHITECTURE.md",
    "get_applio.bat",
    "install.bat",
    "README.md",
    "start.bat",
    "twitch-poll.html",
    "ИНСТРУКЦИЯ.txt",
    "ОБНОВЛЕНИЕ - прочти меня.txt",
    "ОПРОС-ОЗВУЧКА.txt",
    "app/cosyvoice.py",
    "app/emotes.py",
    "app/langs.py",
    "app/cosyvoice_worker.py",
    "app/download_model.py",
    "app/engine.py",
    "app/main.py",
    "app/rvc.py",
    "app/rvc_train.py",
    "app/rvc_worker.py",
    "app/textproc.py",
    "app/twitch.py",
    "app/voices.py",
    "app/web/index.html",
    "app/web/overlay.html",
    "app/web/poll.html",
    "app/web/style.css",
    "app/web/voices.html",
    "tools/sync_poll_css.py",
    "tools/audit_layout.py",
    "tools/audit_functional.py",
    "tools/audit_mute_volume.py",
    "tools/audit_emotes.py",
    "tools/test_emotes.py",
    "tools/test_langs.py",
]

MARKER = "Что нового в этом обновлении:"

NEW_BLOCK = """Что нового в этом обновлении:
- своя громкость каждого голоса: под каждым голосом на вкладке "Голоса" появился
  свой ползунок 0-200 %. Можно один голос сделать тише, другой - громче.
- общая громкость озвучки расширена до 0-200 % (ползунок в чате и в настройках опроса).
  Значения выше 100 % усиливают звук (это работает и для синтеза, и для видео-оверлея).
- в шапке на ВСЕХ страницах ("Опрос", "Голоса", чат) появилась кнопка
  "Озвучка вкл/выкл" - одна кнопка глушит озвучку чата целиком.
  Пока она выключена, реплики в чате помечаются как "озвучка выключена".
  Настройка сохраняется в программе, а не в браузере, и видна на всех вкладках.
- полностью переработан внешний вид: новая типографика, широкий макет,
  плитки результатов голосования, радар зала в рамке, аккуратные списки
  "Пришли/Ушли", новая панель настроек опроса и адаптивность от 640 до 1920 px.
"""

OLD_BLOCK_START = "Что нового:"


def update_note():
    path = os.path.join(ROOT, "ОБНОВЛЕНИЕ - прочти меня.txt")
    with open(path, "r", encoding="utf-8") as fh:
        text = fh.read()
    if MARKER in text:
        print("заметка уже обновлена")
        return
    idx = text.find(OLD_BLOCK_START)
    if idx == -1:
        print("WARNING: секция 'Что нового:' не найдена")
        return
    head = text[:idx]
    tail = text[idx:]
    # убираем старый блок до конца файла и заменяем новым
    with open(path, "w", encoding="utf-8", newline="\r\n") as fh:
        fh.write(head + NEW_BLOCK)
    print("заметка обновлена")


def build():
    missing = [name for name in FILES if not os.path.isfile(os.path.join(ROOT, name))]
    if missing:
        print("ОТСУТСТВУЮТ:", missing)
        return 1
    if os.path.exists(OUT):
        os.remove(OUT)
    with zipfile.ZipFile(OUT, "w", zipfile.ZIP_DEFLATED) as zf:
        for name in FILES:
            zf.write(os.path.join(ROOT, name), name)
    with zipfile.ZipFile(OUT) as zf:
        bad = zf.testzip()
        count = len(zf.namelist())
    size = os.path.getsize(OUT)
    print(f"архив: {OUT}")
    print(f"файлов: {count}, размер: {size} байт, целостность: {'OK' if bad is None else bad}")
    return 0 if bad is None else 1


if __name__ == "__main__":
    update_note()
    sys.exit(build())
