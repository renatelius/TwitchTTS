@echo off
chcp 65001 >nul
setlocal
title Загрузка Applio (RVC)
set "DRV=%~d0"
set "DEST=%DRV%\Applio"
set "ZIP=%DRV%\ApplioV3.7.1.zip"
set "URL=https://huggingface.co/IAHispano/Applio/resolve/main/Compiled/Windows/ApplioV3.7.1.zip"
set "SHA=7fcaed5f24814194192d7853b9759cd553d292a4cd4f53f8dbe4e44ac3464de8"

echo.
echo  === Загрузка Applio 3.7.1 (готовая сборка, 6.8 ГБ) ===
echo  Откуда: официальное хранилище IAHispano на HuggingFace
echo  Куда:   %DEST%
echo  Нужно около 25 ГБ свободного места на диске %DRV%
echo.
if exist "%DEST%\" (
  echo  Папка %DEST% уже существует - ничего не делаю.
  pause
  exit /b 1
)

echo  [1/3] Скачиваю... (если оборвётся - запустите файл ещё раз, загрузка продолжится)
curl -L --fail --retry 5 --retry-delay 5 -C - -o "%ZIP%" "%URL%"
if errorlevel 1 goto fail

echo.
echo  [2/3] Проверяю, что файл скачался без повреждений...
certutil -hashfile "%ZIP%" SHA256 | findstr /i /c:"%SHA%" >nul
if errorlevel 1 (
  echo  !!! Контрольная сумма не совпала. Удалите %ZIP% и запустите этот файл снова.
  goto fail
)

echo  [3/3] Распаковываю в %DEST% (несколько минут)...
mkdir "%DEST%"
tar -xf "%ZIP%" -C "%DEST%"
if errorlevel 1 goto fail
del "%ZIP%"

echo.
echo  === Готово! Applio лежит в %DEST% ===
echo  Теперь закройте программу озвучки (чёрное окно) и запустите start.bat заново.
pause
exit /b 0

:fail
echo.
echo  !!! Не получилось. Скопируйте текст выше и покажите Claude.
pause
exit /b 1
