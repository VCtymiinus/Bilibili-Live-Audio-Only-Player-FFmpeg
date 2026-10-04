@echo off
REM ============================================================
REM  bililive - play ONLY the audio of a Bilibili live room
REM
REM  IMPORTANT: keep this file pure ASCII.
REM  cmd.exe reads .bat with the system OEM codepage (GBK on
REM  Chinese Windows), NOT UTF-8. Chinese text here gets decoded
REM  into garbage and executed as commands, which shreds the
REM  script - the window flashes and closes without any error.
REM ============================================================

setlocal enabledelayedexpansion
cd /d "%~dp0"
title bililive v@VERSION@

REM Never write .pyc files.
REM Without this, every run recreates bililive\__pycache__, which pollutes
REM the release folder and contradicts the README claim of "6 modules only".
REM It also keeps the folder read-only-friendly (can run from a USB stick).
set "PYTHONDONTWRITEBYTECODE=1"

echo ============================================
echo   bililive v@VERSION@ - Bilibili live AUDIO only
echo ============================================
echo.

REM ---------- remove Mark-of-the-Web ----------
REM If this folder came from a downloaded or extracted zip, Windows tags every
REM file with "came from another computer" (a Zone.Identifier stream). That tag
REM makes the "Open File - Security Warning / publisher cannot be verified"
REM dialog appear on EVERY run.
REM
REM Run this on EVERY start, not "only if tagged":
REM   * cmd's `if exist "file:Zone.Identifier"` does NOT detect alternate data
REM     streams - it always reports "not found", so a conditional check here
REM     silently never fires. Verified the hard way.
REM   * Unblock-File is idempotent and costs ~0.2s, so always running it is
REM     simpler AND more robust: files added later get unblocked too.
REM
REM Needs no admin rights. Touches ONLY this folder, never system locations.
REM Set BILILIVE_NO_UNBLOCK=1 to skip.
if not defined BILILIVE_NO_UNBLOCK (
    powershell -NoProfile -ExecutionPolicy Bypass -Command "Get-ChildItem -LiteralPath '%~dp0.' -Recurse -File -ErrorAction SilentlyContinue | Unblock-File -ErrorAction SilentlyContinue" >nul 2>&1
)

REM ---------- locate ffplay ----------
if not exist "%~dp0ffplay.exe" (
    echo [ERROR] ffplay.exe not found next to this script.
    echo         It must sit in the SAME folder as start.bat.
    goto :end
)

REM ---------- locate Python ----------
REM Try, in order: env override, py launcher, python on PATH,
REM then a few common install locations.
set "PY="
if defined BILILIVE_PYTHON (
    if exist "%BILILIVE_PYTHON%" set "PY=%BILILIVE_PYTHON%"
)

if not defined PY (
    for /f "delims=" %%L in ('where py.exe 2^>nul') do (
        if not defined PY set "PY=%%L"
    )
)
REM NOTE: if PY ends up as py.exe we must pass -3 separately at call time.
REM cmd.exe treats a variable holding "prog arg" as ONE command name and
REM fails with 9009, so never stuff arguments into PY.
if defined PY (
    echo %PY% | findstr /i /c:"py.exe" >nul
    if not errorlevel 1 set "PYARG=-3"
)

if not defined PY (
    for /f "delims=" %%L in ('where python.exe 2^>nul') do (
        if not defined PY (
            echo %%L | findstr /i "WindowsApps" >nul
            if errorlevel 1 set "PY=%%L"
        )
    )
)

if not defined PY (
    for %%D in (
        "%LOCALAPPDATA%\Programs\Python"
        "C:\Python313" "C:\Python312" "C:\Python311" "C:\Python310" "C:\Python39"
        "D:\Python" "E:\Python"
    ) do (
        if not defined PY (
            if exist "%%~fD\python.exe" set "PY=%%~fD\python.exe"
        )
        if not defined PY (
            for /d %%S in ("%%~fD\Python*") do (
                if not defined PY if exist "%%~fS\python.exe" set "PY=%%~fS\python.exe"
            )
        )
    )
)

if not defined PY (
    echo [ERROR] Python not found.
    echo.
    echo   Install Python 3.8+ from https://www.python.org/downloads/
    echo   and tick "Add python.exe to PATH" during setup.
    echo.
    echo   Or set the BILILIVE_PYTHON environment variable to your
    echo   python.exe full path, e.g.
    echo       set BILILIVE_PYTHON=D:\Python\python.exe
    goto :end
)

echo Python: !PY! !PYARG!
echo.

REM ---------- room number ----------
set "ROOM=%~1"
if "!ROOM!"=="" (
    set /p ROOM=Enter live room number: 
)
if "!ROOM!"=="" (
    echo [ERROR] No room number given.
    goto :end
)

set "VOL=%~2"
if "!VOL!"=="" set "VOL=100"

echo Room: !ROOM!    Volume: !VOL!
echo Connecting... first connection can take 5-20 seconds.
echo Press Ctrl+C to stop.
echo.

REM Two separate tokens - never merge PY and PYARG into one variable.
"!PY!" !PYARG! "%~dp0bililive_main.py" !ROOM! --volume !VOL!
set "RC=!ERRORLEVEL!"

echo.
if not "!RC!"=="0" (
    echo [ERROR] Exited with code !RC!
    echo.
    echo Common causes:
    echo   * room number wrong, or streamer is offline
    echo   * cannot reach Bilibili API ^(network / firewall^)
    echo   * ffplay.exe blocked by antivirus
    echo.
    echo See the README.txt in this folder.
) else (
    echo Stopped normally.
)

:end
echo.
pause
