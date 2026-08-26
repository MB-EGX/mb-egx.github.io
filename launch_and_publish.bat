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

setlocal enabledelayedexpansion
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

REM --- Decision-matrix worker parallelism ------------------------------
REM config.py hard-caps this to 2 by default as a RAM/page-file safety
REM net for low-memory Windows machines (each worker process reloads the
REM full numpy/scipy/pandas stack independently). If this machine has
REM more headroom (8GB+ RAM, 4+ cores), raising this speeds up "Execute
REM Matrix" / the recompute step below roughly in proportion to the
REM increase - e.g. 4 instead of 2 lets ~twice as many ticker chunks run
REM at once. Uncomment and adjust the line below; leave it commented to
REM keep config.py's conservative default.
REM set "MBEGX_MAX_WORKERS=4"

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
REM PERF: this used to be two separate powershell.exe invocations (one
REM here to find an already-running app_gui.py PID, one further below
REM just to read today's date) - each PowerShell process start is real,
REM fixed overhead (routinely 0.5-1.5s+ on Windows, more with AV
REM scanning) paid on EVERY single launch regardless of how much data
REM there is to publish. Merged into one script/one process that prints
REM both answers, prefixed so the batch side can tell them apart.
set "APP_ALREADY_RUNNING="
set "TODAY_STR="
set "PS_INIT_SCRIPT=%TEMP%\mbegx_init_%RANDOM%.ps1"
(
    echo $procs = Get-CimInstance Win32_Process -Filter "Name='pythonw.exe' or Name='python.exe'" -ErrorAction SilentlyContinue
    echo $match = $procs ^| Where-Object { $_.CommandLine -and $_.CommandLine -like '*app_gui.py*' } ^| Select-Object -First 1
    echo if ^($match^) { Write-Output "PID:$($match.ProcessId)" }
    echo Write-Output "DATE:$(Get-Date -Format 'yyyy-MM-dd')"
) > "%PS_INIT_SCRIPT%"

for /f "usebackq tokens=1,2 delims=:" %%A in (`powershell -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "%PS_INIT_SCRIPT%" 2^>nul`) do (
    if "%%A"=="PID" set "APP_ALREADY_RUNNING=%%B"
    if "%%A"=="DATE" set "TODAY_STR=%%B"
)
del "%PS_INIT_SCRIPT%" >nul 2>&1

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

REM --- Daily walk-forward + factor backtest refresh -------------------
REM Runs backtest_tools.py run --save (persists to walk_forward_runs, see
REM db_manager.save_walkforward_run) and backtest_tools.py factors once per
REM CALENDAR DAY, right here - after publish.py has released the DuckDB
REM lock and before app_gui.py grabs it for the rest of the session (see
REM the "one process at a time" note above; a backtest run mid-app-session
REM would hit the same IOException publish.py itself guards against).
REM
REM Once per day, not every launch: backtest_tools.py factors walks the full
REM tradeable universe with a walk-forward split per ticker (263 tickers
REM in your last run) - genuinely slow, and re-running it on every single
REM open (including a 3rd/4th launch in the same afternoon) would make
REM "double-click to check the dashboard" annoying for zero new signal,
REM since nothing about the backtest changes until tomorrow's session
REM data lands. The marker file below is the "have I already done this
REM today" gate - same pattern post_state.py already uses for the social
REM poster's daily reset. Delete backtests\last_backtest_date.txt (or
REM just edit the date inside it) any time you want to force an
REM out-of-schedule re-run.
REM
REM BEFORE running either slow script, a fast readiness check
REM (backtest_tools.py readiness - one SQL COUNT query, not a full
REM backtest) confirms at least one ticker actually has enough ingested
REM history (WALK_FORWARD_BACKTEST_DEFAULTS min_train_bars + test_bars,
REM 310 bars by default) to produce a fold at all. diagnose_backtest_
REM coverage.py found your real data currently sits at ~35 bars/ticker -
REM running the full 263-ticker walk-forward every day against that is
REM guaranteed to come back empty until enough calendar days of ingestion
REM pass (or you backfill deeper historical CSVs into market_data_feeds/).
REM The readiness check keeps every launch fast until that's no longer
REM true, then the real runs start automatically - no need to remember to
REM turn this back on yourself.
REM
REM Also refreshes the public Strategy Calculator summary
REM (backtest_tools.py export-summary) on its OWN 7-day cadence, per that
REM script's own docstring ("run by hand, or a slow weekly/monthly cron")
REM - not daily, since it's the same underlying engine and just as
REM guaranteed-empty while history is short. When it does run, its output
REM file is committed + pushed immediately rather than waiting for
REM tomorrow's publish.py to happen to pick it up.
REM
REM Never blocks the app from opening: any failure here is a warning,
REM not a stop - matching how a publish.py failure above is handled.
if not exist "backtests" mkdir "backtests"

REM TODAY_STR was already fetched in the merged init check above (no
REM second powershell.exe launch needed here anymore).

set "LAST_BACKTEST_DATE="
if exist "backtests\last_backtest_date.txt" set /p LAST_BACKTEST_DATE=<"backtests\last_backtest_date.txt"

if not defined TODAY_STR (
    echo [!] Could not determine today's date - skipping the daily backtest refresh gate check this run.
) else if "%LAST_BACKTEST_DATE%"=="%TODAY_STR%" (
    echo Walk-forward + factor backtest already refreshed today ^(%TODAY_STR%^) - skipping.
    echo ^(Delete backtests\last_backtest_date.txt to force a re-run.^)
) else (
    echo Checking backtest data readiness ^(fast - no full backtest run yet^)...
    "%PY%" backtest_tools.py readiness
    if not "!ERRORLEVEL!"=="0" (
        echo Skipping today's backtest refresh - not enough ingested history yet ^(see message above^).
        echo This check re-runs automatically on your next launch - no action needed until it's ready.
    ) else (
        echo ============================================
        echo  Refreshing walk-forward + factor backtests
        echo  ^(first launch today - this can take a while^)
        echo ============================================
        echo Log: backtests\backtest_%TODAY_STR%.log

        "%PY%" backtest_tools.py run --save > "backtests\backtest_%TODAY_STR%.log" 2>&1
        set "BACKTEST_RC=!ERRORLEVEL!"
        "%PY%" backtest_tools.py factors --out "backtests\factor_backtest_%TODAY_STR%.json" >> "backtests\backtest_%TODAY_STR%.log" 2>&1
        set "FACTOR_RC=!ERRORLEVEL!"

        if not "!BACKTEST_RC!"=="0" (
            echo [!] backtest_tools.py run failed - see backtests\backtest_%TODAY_STR%.log
        )
        if not "!FACTOR_RC!"=="0" (
            echo [!] backtest_tools.py factors failed - see backtests\backtest_%TODAY_STR%.log
        )
        if "!BACKTEST_RC!"=="0" (
            if "!FACTOR_RC!"=="0" (
                echo Backtest refresh complete - see backtests\backtest_%TODAY_STR%.log
                REM Only stamp today's date as "done" on a clean run - a failed
                REM run should still be retried on the NEXT launch today, not
                REM silently treated as done for the day.
                > "backtests\last_backtest_date.txt" echo %TODAY_STR%
            )
        )

        REM --- Weekly public Strategy Calculator summary (N6) ---------
        REM backtest_tools.py export-summary's own docstring explicitly says it's
        REM meant for a "hand run, or a slow weekly/monthly cron of your
        REM own" - NOT the daily cadence run_backtest.py/run_factor_
        REM backtest.py get above, so this gets its own 7-day gate rather
        REM than piggy-backing on the daily one. Only attempted here (inside
        REM the readiness-confirmed branch) since it runs the exact same
        REM walk-forward engine and would be just as guaranteed-empty as the
        REM daily backtests while history is still too short.
        set "LAST_SUMMARY_DATE="
        if exist "backtests\last_strategy_summary_date.txt" set /p LAST_SUMMARY_DATE=<"backtests\last_strategy_summary_date.txt"

        set "PS_WEEK_SCRIPT=%TEMP%\mbegx_week_%RANDOM%.ps1"
        (
            echo $today = Get-Date "%TODAY_STR%"
            echo $last = "%LAST_SUMMARY_DATE%"
            echo if ^(-not $last^) { Write-Output "RUN" } else {
            echo     try {
            echo         $lastDate = Get-Date $last
            echo         $days = ^($today - $lastDate^).Days
            echo         if ^($days -ge 7^) { Write-Output "RUN" } else { Write-Output "SKIP" }
            echo     } catch { Write-Output "RUN" }
            echo }
        ) > "!PS_WEEK_SCRIPT!"
        set "SUMMARY_DECISION=RUN"
        for /f "usebackq delims=" %%W in (`powershell -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "!PS_WEEK_SCRIPT!" 2^>nul`) do set "SUMMARY_DECISION=%%W"
        del "!PS_WEEK_SCRIPT!" >nul 2>&1

        if "!SUMMARY_DECISION!"=="RUN" (
            echo ============================================
            echo  Refreshing public Strategy Calculator summary
            echo  ^(weekly - backtest_tools.py export-summary^)
            echo ============================================
            "%PY%" backtest_tools.py export-summary >> "backtests\backtest_%TODAY_STR%.log" 2>&1
            set "SUMMARY_RC=!ERRORLEVEL!"
            if "!SUMMARY_RC!"=="0" (
                REM Push just this file now instead of waiting for tomorrow's
                REM publish.py run to happen to pick it up - see this
                REM script's git-sync comments near sync_with_remote_before_push
                REM in publish.py for why a plain push can be rejected here
                REM too (the social-poster bot may have committed since).
                git add web_public/data/strategy_performance.json web_public/data/cache_manifest.json
                git diff --staged --quiet
                if errorlevel 1 (
                    git commit -m "Weekly strategy performance summary refresh (%TODAY_STR%)"
                    git pull --rebase --autostash
                    git push
                )
                > "backtests\last_strategy_summary_date.txt" echo %TODAY_STR%
                echo Strategy summary refreshed and pushed.
            ) else (
                echo [!] backtest_tools.py export-summary failed - see backtests\backtest_%TODAY_STR%.log
            )
        )
    )
)

REM Launched with pythonw.exe: no console window at all, just the app
REM itself. Startup errors are caught inside app_gui.py and shown as a
REM message box (and logged to quant_app.log) instead of a traceback in
REM a console, since pythonw has none.
start "" "%PYW%" app_gui.py

REM This window closes itself once the app has launched - nothing left
REM on screen but the Stock-Web dashboard.
exit
