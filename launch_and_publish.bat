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
REM
REM That handles the app THIS script launches - but if an earlier run of
REM this same .bat is still open in the background (the app keeps running
REM after the .bat exits, so double-clicking this again while it's still
REM up is easy to do by accident), publish.py will hit that exact lock
REM error. Check for that specific case up front instead of discovering
REM it partway through ingestion.
echo Checking whether MB-EGX is already running...
set "APP_ALREADY_RUNNING="
set "PS_CHECK_SCRIPT=%TEMP%\mbegx_check_running_%RANDOM%.ps1"
(
    echo $procs = Get-CimInstance Win32_Process -Filter "Name='pythonw.exe' or Name='python.exe'" -ErrorAction SilentlyContinue
    echo $match = $procs ^| Where-Object { $_.CommandLine -and $_.CommandLine -like '*app_gui.py*' } ^| Select-Object -First 1
    echo if ^($match^) { Write-Output $match.ProcessId }
) > "%PS_CHECK_SCRIPT%"

for /f "usebackq delims=" %%P in (`powershell -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "%PS_CHECK_SCRIPT%" 2^>nul`) do set "APP_ALREADY_RUNNING=%%P"
del "%PS_CHECK_SCRIPT%" >nul 2>&1

if defined APP_ALREADY_RUNNING (
    echo.
    echo ============================================
    echo  [!] MB-EGX is already running ^(PID %APP_ALREADY_RUNNING%^).
    echo      Close it first, then re-run this script -
    echo      publish.py can't update the database while
    echo      the app has it open.
    echo ============================================
    pause
    exit /b 1
)
REM If the check above couldn't run (PowerShell unavailable/blocked,
REM different Windows edition, etc.), APP_ALREADY_RUNNING just stays
REM empty and we fall through to the normal flow below - publish.py's
REM own retry-then-clear-error handling is the safety net either way.

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
