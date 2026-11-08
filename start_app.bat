@echo off
chcp 65001 > NUL
setlocal enabledelayedexpansion

echo ===================================================
echo   Clip Automation Unified Editor Service
echo ===================================================

cd /d "%~dp0"

if not exist ".\.venv\Scripts\python.exe" (
    echo [ERROR] .venv が見つかりません。セットアップを行ってください。
    pause
    exit /b 1
)

if not exist ".\editor_frontend\dist\index.html" (
    echo [INFO] editor_frontend/dist をビルドします...
    call npm --prefix editor_frontend run build
)

echo [INFO] ブラウザで Unified Editor (http://127.0.0.1:8766/) を開きます...
if not defined CLIP_AUTOMATION_HOST set "CLIP_AUTOMATION_HOST=127.0.0.1"
if not defined CLIP_AUTOMATION_PORT set "CLIP_AUTOMATION_PORT=8766"

echo [INFO] Unified Editor Server: http://%CLIP_AUTOMATION_HOST%:%CLIP_AUTOMATION_PORT%/
echo [INFO] Remote client: ssh -L 8766:127.0.0.1:%CLIP_AUTOMATION_PORT% galleria
echo [INFO] Open http://127.0.0.1:8766/ in the client browser.

echo [INFO] FastAPI Server を起動中 (port 8766)...
".\.venv\Scripts\python.exe" editor_api.py

pause
