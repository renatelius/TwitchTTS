@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
title Установка озвучки чата Twitch

echo.
echo  === Установка озвучки чата Twitch ===
echo  Скачается около 6 ГБ (PyTorch и модель голоса). Это надолго: 10-30 минут.
echo.

set "PY="
for %%V in (3.12 3.11 3.10 3.13) do if not defined PY (py -%%V -c "import sys" >nul 2>&1 && set "PY=py -%%V")
if not defined PY (python -c "import sys; sys.exit(0 if (3,10) <= sys.version_info[:2] <= (3,13) else 1)" >nul 2>&1 && set "PY=python")
if defined PY goto have_python

echo  Не найден Python версии 3.10 - 3.12.
echo  Могу поставить Python 3.12 через winget (стандартный установщик Windows).
choice /c YN /m "  Установить Python 3.12 сейчас"
if errorlevel 2 goto no_python
winget install -e --id Python.Python.3.12 --accept-package-agreements --accept-source-agreements
echo.
echo  Python установлен. Закройте это окно и запустите install.bat ещё раз.
pause
exit /b 0

:no_python
echo.
echo  Установите Python 3.12 с сайта https://www.python.org/downloads/
echo  (при установке поставьте галочку "Add python.exe to PATH") и запустите install.bat снова.
pause
exit /b 1

:have_python
echo  [1/5] Python найден: %PY%
if not exist "venv\Scripts\python.exe" (
  echo  [2/5] Создаю отдельное окружение в папке venv...
  %PY% -m venv venv || goto fail
) else (
  echo  [2/5] Окружение venv уже есть.
)
set "VPY=%~dp0venv\Scripts\python.exe"

echo  [3/5] Ставлю PyTorch под вашу видеокарту...
"%VPY%" -m pip install --upgrade pip
"%VPY%" -m pip install pypinyin cutlet unidic-lite uv --quiet || goto fail
"%VPY%" -m uv pip install --python "%VPY%" torch torchaudio --torch-backend=auto || goto fail
"%VPY%" -m uv pip install --python "%VPY%" torchcodec --torch-backend=auto || echo  (torchcodec не поставился - это не страшно)

echo  [4/5] Ставлю движок озвучки и остальное...
"%VPY%" -m uv pip install --python "%VPY%" coqui-tts starlette uvicorn python-multipart imageio-ffmpeg numpy six --torch-backend=auto || goto fail

echo.
"%VPY%" -c "import torch; ok = torch.cuda.is_available(); print('  Видеокарта:', torch.cuda.get_device_name(0) if ok else 'NVIDIA НЕ НАЙДЕНА - озвучка будет очень медленной. Обновите драйвер видеокарты и запустите install.bat снова.')"
echo.

echo  [5/5] Модель голоса XTTS v2 (около 2 ГБ).
echo  Модель распространяется по лицензии Coqui Public Model License:
echo  её можно использовать только в НЕКОММЕРЧЕСКИХ целях.
echo  Лицензия указана на странице модели: https://huggingface.co/coqui/XTTS-v2
choice /c YN /m "  Принимаете условия лицензии и скачиваем модель"
if errorlevel 2 goto no_license
if not exist data mkdir data
echo accepted> "data\license_ok.txt"
"%VPY%" "app\download_model.py" || goto fail

echo.
echo  === Готово! Запускайте start.bat ===
pause
exit /b 0

:no_license
echo  Без модели озвучка работать не будет. Запустите install.bat снова, когда решите.
pause
exit /b 1

:fail
echo.
echo  !!! Установка не удалась. Скопируйте текст ошибки выше и покажите Claude.
pause
exit /b 1
