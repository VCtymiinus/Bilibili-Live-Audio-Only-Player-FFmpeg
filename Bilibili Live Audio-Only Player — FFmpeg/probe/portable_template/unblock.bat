@echo off
REM ============================================================
REM  Remove the "downloaded from the internet" tag from this folder.
REM
REM  WHY YOU MIGHT NEED THIS
REM  When a zip is downloaded (browser / WeChat / QQ / cloud drive),
REM  Windows tags it as "came from another computer". Every file
REM  extracted from it inherits that tag. Once tagged, Windows shows
REM  the dialog:
REM
REM      "Open File - Security Warning
REM       The publisher could not be verified. Are you sure you want
REM       to run this software?"
REM
REM  ...on EVERY run of start.bat, until the tag is removed.
REM
REM  run this once and the dialog stops appearing.
REM
REM  No admin rights needed. Only touches this folder.
REM ============================================================

cd /d "%~dp0"

echo Clearing the "downloaded from the internet" tag in:
echo   %~dp0
echo.

powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$n=0; Get-ChildItem -LiteralPath '%~dp0.' -Recurse -File -ErrorAction SilentlyContinue | ForEach-Object { if (Get-Item -LiteralPath $_.FullName -Stream Zone.Identifier -ErrorAction SilentlyContinue) { Unblock-File -LiteralPath $_.FullName -ErrorAction SilentlyContinue; $n++ } }; Write-Host ('  cleared on ' + $n + ' file(s)')"

echo.
echo Done. You can now run start.bat without the warning.
echo.
pause
