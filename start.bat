@echo off
chcp 65001 > nul
title Subtitle Bot

echo ================================
echo    Subtitle Bot Launcher
echo ================================
echo.

:: Load .env file
if exist .env (
    for /f "usebackq tokens=1,2 delims==" %%a in (".env") do (
        if /i "%%a"=="BOT_TOKEN"     set "BOT_TOKEN=%%b"
        if /i "%%a"=="WEBAPP_URL"    set "WEBAPP_URL=%%b"
        if /i "%%a"=="ADMIN_USERS"   set "ADMIN_USERS=%%b"
        if /i "%%a"=="WHISPER_MODEL" set "WHISPER_MODEL=%%b"
        if /i "%%a"=="API_PORT"      set "API_PORT=%%b"
    )
)

:: Check Python
python --version > nul 2>&1
if %errorlevel% neq 0 (
    echo [ERROR] Python not found!
    pause & exit /b 1
)

:: Check ngrok
set "NGROK_EXE="
if exist ngrok.exe (
    set "NGROK_EXE=ngrok.exe"
) else (
    where ngrok > nul 2>&1
    if %errorlevel% equ 0 set "NGROK_EXE=ngrok"
)

if "%NGROK_EXE%"=="" (
    echo [WARN] ngrok.exe not found - font sync disabled
    echo [WARN] Place ngrok.exe in this folder to enable it
    echo.
    python bot.py
    pause & exit /b 0
)

:: Start ngrok
echo [1/2] Starting ngrok on port 8765...
start "ngrok" cmd /c "%NGROK_EXE% http 8765"

echo [INFO] Waiting for ngrok to start...
timeout /t 4 /nobreak > nul

:: Get ngrok URL
for /f "delims=" %%i in ('python -c "import urllib.request,json; d=json.loads(urllib.request.urlopen(\"http://localhost:4040/api/tunnels\",timeout=3).read()); ts=d.get(\"tunnels\",[]); url=[t[\"public_url\"] for t in ts if t.get(\"proto\")==\"https\"]; print(url[0] if url else (ts[0][\"public_url\"] if ts else \"\"))" 2^>nul') do set "NGROK_URL=%%i"

if "%NGROK_URL%"=="" (
    echo [WARN] Could not get ngrok URL - open http://localhost:4040
) else (
    echo.
    echo ==================================================
    echo   ngrok URL: %NGROK_URL%
    echo   Paste this in WebApp when prompted
    echo ==================================================
    set "PUBLIC_URL=%NGROK_URL%"
)

echo.
echo [2/2] Starting bot...
echo.
python bot.py
pause
