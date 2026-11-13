# -*- coding: utf-8 -*-
"""Скачивает модель XTTS v2. Запускается из install.bat после согласия с лицензией."""
import os
import sys

os.environ["COQUI_TOS_AGREED"] = "1"
from TTS.utils.manage import ModelManager

res = ModelManager().download_model("tts_models/multilingual/multi-dataset/xtts_v2")
path = res[0] if isinstance(res, (tuple, list)) else res
print("  Модель на месте:", path)
sys.exit(0)
