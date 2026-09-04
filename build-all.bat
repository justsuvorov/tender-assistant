@echo off
REM Сборка обоих EXE (API-сервис и GUI) с копированием конфигов в dist/

echo.
echo ========================================
echo  Сборка Tender Assistant (2 EXE)
echo ========================================
echo.

if not exist "main.py" (
    echo ERROR: main.py не найден!
    echo Запусти этот батник из корня проекта
    pause
    exit /b 1
)

echo [1/5] Установка зависимостей...
pip install -r requirements.txt -q
pip install -r app\requirements.txt -q
pip install pyinstaller -q
if errorlevel 1 (
    echo ERROR: Установка зависимостей не удалась!
    pause
    exit /b 1
)

echo [2/5] Сборка API-сервиса...
pyinstaller build_api.spec --clean --noconfirm
if not exist "dist\Tender Assistant API.exe" (
    echo ERROR: Сборка API не удалась!
    pause
    exit /b 1
)
echo OK: dist\Tender Assistant API.exe

echo [3/5] Сборка GUI-приложения...
pyinstaller build.spec --clean --noconfirm
if not exist "dist\Tender Assistant.exe" (
    echo ERROR: Сборка GUI не удалась!
    pause
    exit /b 1
)
echo OK: dist\Tender Assistant.exe

echo [4/5] Копирование конфига API (.env)...
REM Ключи провайдеров в EXE не зашиваются — .env читается рядом с ним
REM в рантайме (core/config.py: env_file=".env"). Свой .env, если есть,
REM в приоритете перед .env.example — на случай повторной сборки после
REM того, как оператор уже заполнил ключи в dist/.env.
if exist "dist\.env" (
    echo OK: dist\.env уже существует, не перезаписываю
) else if exist ".env" (
    copy .env dist\.env >nul
    echo OK: dist\.env скопирован из .env — проверьте, не попал ли туда чужой ключ
) else (
    copy .env.example dist\.env >nul
    echo OK: dist\.env создан из .env.example — впишите ключ провайдера перед запуском
)

echo [5/5] Копирование конфига GUI (config.json)...
REM Рядом с EXE, не в подпапку app\ — во frozen-режиме app\main.py ищет его
REM через APP_DIR (папка самого .exe), см. комментарий в app\main.py.
if exist "dist\config.json" (
    echo OK: dist\config.json уже существует, не перезаписываю
) else (
    copy app\config.json dist\config.json >nul
    echo OK: dist\config.json
)

if exist "run-all.bat" copy run-all.bat dist\run-all.bat >nul

echo.
echo ========================================
echo  Сборка завершена успешно!
echo ========================================
echo.
echo Структура dist\:
echo   Tender Assistant API.exe    (сервис, консоль с логами)
echo   Tender Assistant.exe        (GUI)
echo   .env                        (ключ провайдера — для API)
echo   config.json                 (адрес API — для GUI)
echo   run-all.bat                 (запуск обоих сразу)
echo.
echo Перед первым запуском впишите ключ провайдера в dist\.env
echo.
echo Для запуска:
echo   dist\run-all.bat
echo.
pause
