# -*- coding: utf-8 -*-
"""Общие пути приложения: данные, кэш звука, временные файлы и веб-страницы."""
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
ROOT = APP_DIR.parent
DATA = ROOT / "data"
CACHE = DATA / "cache"
TMP = DATA / "tmp"
WEB = APP_DIR / "web"
MAX_UPLOAD = 500 * 1024 * 1024
