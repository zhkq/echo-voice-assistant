@echo off
REM =====================================================================
REM  ECHO - BACKUP meeting data (double-click me)
REM  Calls 1-备份ECHO会议数据.ps1 (see the .ps1 for options).
REM  Keep this file ASCII-only: cmd.exe reads it in the console code page.
REM =====================================================================
chcp 65001 >nul
setlocal EnableExtensions
cd /d "%~dp0"

echo.
echo  ============================================================
echo   ECHO  -  backup meeting data
echo  ============================================================
echo   Backs up: database snapshot + meeting folder (recordings,
echo   transcripts, summaries) + table dumps.
echo   Default target: ^<drive^>\ECHO-backup\ECHO-^<timestamp^>\
echo.
echo   Keep this window open; it prints what it is doing.
echo.

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp01-备份ECHO会议数据.ps1" %*
set "RC=%ERRORLEVEL%"

echo.
if not "%RC%"=="0" (
    echo  [x] backup failed, exit code %RC%.
    echo      Read the message above; nothing was deleted.
) else (
    echo  [ok] backup finished.
    echo       Next: double-click 2-清理ECHO.cmd
)
echo.
pause
exit /b %RC%
