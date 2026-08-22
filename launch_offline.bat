@echo off
REM ===================================================================
REM launch_offline.bat  —  CREATOR-ONLY PRIVATE LAUNCHER (OFFLINE)
REM
REM Opens the MB-EGX desktop app with NO internet and NO Firebase login.
REM This is exclusively for the (the creator) — it is NOT part of the
REM normal launch_and_publish.bat flow and is NOT meant to be shared
REM with or distributed to other users.
REM
REM How the gate works:
REM   * The app only shows the "Continue Offline" button when BOTH are true:
REM       1) MBEGX_OFFLINE=1          (set here)
REM       2) MBEGX_OFFLINE_KEY matches the SHA-256 digest stored in
REM          app_gui.py (only the hash lives in the the code, never the
REM          plaintext key — so copying this file alone does NOT unlock
REM          offline mode; it must carry the matching secret).
REM   * In offline mode every cloud sync (session heartbeat, usage
REM     analytics, dealing stats push) is skipped, so nothing waits on
REM     the network or blocks the dashboard from opening.
REM
REM Place this file in the same folder as app_gui.py (the repo root).
REM ===================================================================

setlocal enabledelayedexpansion
cd /d "%~dp0"

REM --- Creator secret (see the the comment above; keep this file private) ---
set "MBEGX_OFFLINE=1"
set "MBEGX_OFFLINE_KEY=MBEGX-CREATOR-827f1086fc786f5c"

REM --- Prefer the project venv Python, else fall back to PATH ---
if exist "venv\Scripts\pythonw.exe" (
    set "PYW=venv\Scripts\pythonw.exe"
) else (
    set "PYW=pythonw"
)

REM --- Launch the desktop app with NO console window ---
start "" "%PYW%" app_gui.py

endlocal
