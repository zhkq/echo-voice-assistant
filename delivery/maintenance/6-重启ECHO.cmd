@echo off
REM =====================================================================
REM  ECHO - RESTART (double-click me)
REM  Calls 6-重启ECHO.ps1 : stop then start, restores the watchdog.
REM  Refuses while a meeting is recording, unless -Force is passed.
REM  Keep this file ASCII-only.
REM =====================================================================
chcp 65001 >nul
setlocal EnableExtensions
cd /d "%~dp0"

echo.
echo  ============================================================
echo   ECHO  -  restart
echo  ============================================================
echo.

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp06-重启ECHO.ps1" %*
set "RC=%ERRORLEVEL%"

echo.
if not "%RC%"=="0" (
    echo  [x] restart failed/refused, exit code %RC%.
    echo      Try: 7-状态ECHO.cmd -LogTail 30
) else (
    echo  [ok] ECHO restarted.
)
echo.
pause
exit /b %RC%
