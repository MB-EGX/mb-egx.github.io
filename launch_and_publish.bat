@echo off
REM ===================================================================
REM launch_and_publish.bat
REM
REM Double-click this to:
REM   1) Open the desktop app (app_gui.py) right away.
REM   2) At the same time, run publish.py in this window, which
REM      ingests new data, rebuilds market_data.json, and pushes
REM      everything to GitHub so the live website updates.
REM
REM Place this file in the same folder as app_gui.py / publish.py
REM (the repo root — same place as export_json.py / config.py).
REM ===================================================================

setlocal
cd /d "%~dp0"

REM --- Prefer the project's venv Python if present, else fall back to PATH ---
if exist "venv\Scripts\python.exe" (
    set "PY=venv\Scripts\python.exe"
) else (
    set "PY=python"
)

echo ============================================
echo  Starting Stock-Web desktop app...
echo ============================================
REM Launched with python.exe (not pythonw) in its own console window and
REM /k so that window stays open if the app crashes on startup — that
REM way any error/traceback is visible instead of silently disappearing.
start "Stock-Web App" cmd /k ""%PY%" app_gui.py"

echo.
echo ============================================
echo  Publishing latest data to the website...
echo  (ingest -^> recompute -^> export -^> git push)
echo ============================================
"%PY%" publish.py

echo.
echo ============================================
echo  Done. This window will stay open so you can
echo  read any messages/errors above.
echo ============================================
pause >nul
