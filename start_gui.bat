@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" goto missing_venv

where ffmpeg >nul 2>nul
if errorlevel 1 echo [WARNING] ffmpeg was not found in PATH.

where ffprobe >nul 2>nul
if errorlevel 1 echo [WARNING] ffprobe was not found in PATH.

echo Starting YouTube Clip Automation...
echo Open http://127.0.0.1:7860 in your browser.
echo Before closing this window, stop the active job safely in the GUI.
echo.

".venv\Scripts\python.exe" "ui\app.py"
set "APP_EXIT_CODE=%ERRORLEVEL%"
echo.
echo The app stopped with exit code %APP_EXIT_CODE%.
pause
exit /b %APP_EXIT_CODE%

:missing_venv
echo [ERROR] .venv was not found.
echo Follow the initial setup instructions in README.md.
pause
exit /b 1
