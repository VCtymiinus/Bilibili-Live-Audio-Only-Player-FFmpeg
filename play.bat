@echo off
REM ============================================================
REM  bililive - play ONLY the audio of a Bilibili live room
REM
REM  NOTE: keep this file pure ASCII.
REM  cmd.exe reads .bat using the system OEM codepage (GBK on
REM  Chinese Windows), not UTF-8. Chinese text here gets decoded
REM  into garbage and executed as commands, which shreds the
REM  script - the window flashes and closes with no error.
REM ============================================================

setlocal enabledelayedexpansion
cd /d "%~dp0"

echo ============================================
echo   bililive v1.1 - Bilibili live AUDIO only
echo ============================================
echo.

REM Locate Python without hardcoding a personal path.
REM Order: BILILIVE_PYTHON env var -> py launcher -> python on PATH.
REM NOTE: never store "prog arg" in one variable - cmd treats it as a single
REM command name containing a space and fails with 9009.
set "PY="
set "PYARG="
if defined BILILIVE_PYTHON (
    if exist "%BILILIVE_PYTHON%" set "PY=%BILILIVE_PYTHON%"
)
if not defined PY (
    for /f "delims=" %%L in ('where py.exe 2^>nul') do (
        if not defined PY (
            set "PY=%%L"
            set "PYARG=-3"
        )
    )
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
    echo [ERROR] Python not found. Install Python 3.8+ or set BILILIVE_PYTHON.
    goto :end
)
echo Python: !PY! !PYARG!
echo.

REM NOTE: run bililive_main.py, NOT "-m bililive".
REM The package's __main__.py still routes to the deprecated pipe-based
REM cli.py; bililive_main.py is the supported entry point.
if not exist "%~dp0bililive_main.py" (
    echo [ERROR] bililive_main.py not found next to this script.
    goto :end
)

if not exist "%~dp0tools\ffplay.exe" (
    echo [ERROR] tools\ffplay.exe not found.
    echo         Run: python probe\fetch_ffmpeg.py
    goto :end
)

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

echo.
echo Room: !ROOM!    Volume: !VOL!
echo Connecting... first connection can take 5-20 seconds.
echo Press Ctrl+C to stop.
echo.

"%PY%" !PYARG! "%~dp0bililive_main.py" !ROOM! --volume !VOL!
set "RC=!ERRORLEVEL!"

echo.
if not "!RC!"=="0" (
    echo [ERROR] Exited with code !RC!
    echo.
    echo Common causes:
    echo   * ffplay missing  -^> run: python probe\fetch_ffmpeg.py
    echo   * room number wrong, or streamer is offline
    echo   * cannot reach Bilibili API
) else (
    echo Stopped normally.
)

:end
echo.
pause
