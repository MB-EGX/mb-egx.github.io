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

REM --- app_gui.py's Firebase sign-in / usage-analytics sync needs the
REM     'requests' package. Check once here and auto-install it if it's
REM     missing, so a fresh machine doesn't fail with ImportError later. ---
"%PY%" -c "import requests" >nul 2>&1
if errorlevel 1 (
    echo ============================================
    echo  Installing missing dependency: requests...
    echo ============================================
    "%PY%" -m pip install requests
    if errorlevel 1 (
        echo.
        echo  [!] Failed to install 'requests' automatically.
        echo      Run this manually then re-launch:  "%PY%" -m pip install requests
        echo.
        pause
        exit /b 1
    )
)

REM NOTE: publish.py and app_gui.py both open quant_master.duckdb, and
REM DuckDB (like SQLite) only allows ONE process to hold that file open
REM at a time. Running them truly in parallel causes:
REM   IOException: Cannot open file ... being used by another process
REM So we run publish.py to completion FIRST (it's quick when there's
REM nothing new to ingest), then launch the app once the DB is free.

echo ============================================
echo  Publishing latest data to the website...
echo  (ingest -^> recompute -^> export -^> git push)
echo ============================================
"%PY%" publish.py

echo.
echo ============================================
echo  Starting Stock-Web desktop app...
echo ============================================
REM Launched with python.exe (not pythonw) in its own console window and
REM /k so that window stays open if the app crashes on startup — that
REM way any error/traceback is visible instead of silently disappearing.
start "Stock-Web App" cmd /k ""%PY%" app_gui.py"

echo.
echo ============================================
echo  Done. This window will stay open so you can
echo  read any messages/errors above.
echo ============================================
pause >nul
