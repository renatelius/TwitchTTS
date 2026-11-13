@echo off
chcp 65001 >nul
cd /d "%~dp0"
title Озвучка чата Twitch
if not exist "venv\Scripts\python.exe" (
  echo  Сначала запустите install.bat
  pause
  exit /b 1
)
set PYTHONUTF8=1
"venv\Scripts\python.exe" "app\main.py"
echo.
echo  Программа остановилась. Если это ошибка - текст выше и файл data\log.txt помогут разобраться.
pause
