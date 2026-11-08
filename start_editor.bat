@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" goto missing
if not exist "editor_frontend\node_modules" goto missing_node
start "Clip Editor API" /min ".venv\Scripts\python.exe" "editor_api.py"
start "Clip Editor UI" /min cmd /c "cd /d editor_frontend && npm run dev -- --host 127.0.0.1"
timeout /t 2 /nobreak >nul
start "" "http://127.0.0.1:5173"
exit /b 0
:missing
echo [ERROR] Python .venv was not found.
pause
exit /b 1
:missing_node
echo [ERROR] Run npm install in editor_frontend first.
pause
exit /b 1
