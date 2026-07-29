@echo off
REM ===================================================================
REM launch_and_publish.bat
REM
REM Double-click this to:
REM   1) Run publish.py (ingest -> recompute -> export -> git push).
REM   2) Then open the desktop app (app_gui.py) with NO console window.
REM
REM This window auto-closes once publish.py finishes successfully -
REM it only stays open (so you can read the error) if something failed.
REM
REM Place this file in the same folder as app_gui.py / publish.py
REM (the repo root — same place as export_json.py / config.py).
REM ===================================================================

setlocal
cd /d "%~dp0"

REM --- OpenBLAS/multiprocessing memory-fix safety net ---
REM config.py sets these same caps as a Python-level side effect, but that
REM only works if config.py is the FIRST thing to import numpy/pandas in
REM a given script - easy to get wrong by accident in a new script. Setting
REM them here, at the OS environment level, means every child process
REM launched below (and every worker any of them spawns) starts with these
REM caps already in place, independent of any file's import order.
set "OPENBLAS_NUM_THREADS=1"
set "MKL_NUM_THREADS=1"
set "OMP_NUM_THREADS=1"
set "NUMEXPR_NUM_THREADS=1"

REM --- Prefer the project's venv Python if present, else fall back to PATH ---
if exist "venv\Scripts\python.exe" (
    set "PY=venv\Scripts\python.exe"
) else (
    set "PY=python"
)
REM pythonw.exe = same interpreter, but with no console window attached.
REM Used only for launching the GUI app itself.
if exist "venv\Scripts\pythonw.exe" (
    set "PYW=venv\Scripts\pythonw.exe"
) else (
    set "PYW=pythonw"
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
set "PUBLISH_RC=%ERRORLEVEL%"

if not "%PUBLISH_RC%"=="0" (
    echo.
    echo ============================================
    echo  [!] publish.py failed - see the messages above.
    echo      The app will still open, but the website
    echo      may not reflect the latest data.
    echo ============================================
    pause
)

REM Launched with pythonw.exe: no console window at all, just the app
REM itself. Startup errors are caught inside app_gui.py and shown as a
REM message box (and logged to quant_app.log) instead of a traceback in
REM a console, since pythonw has none.
start "" "%PYW%" app_gui.py

REM This window closes itself once the app has launched - nothing left
REM on screen but the Stock-Web dashboard.
exit
