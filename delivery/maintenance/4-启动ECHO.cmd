@echo off
REM =====================================================================
REM  ECHO - START (double-click me)
REM  Calls 4-启动ECHO.ps1 : starts the service in the background, waits for
REM  the port, prints the panel address, and restores the watchdog.
REM  Arguments are passed through, e.g.   4-启动ECHO.cmd -OpenPanel
REM  Keep this file ASCII-only.
REM =====================================================================
chcp 65001 >nul
setlocal EnableExtensions
cd /d "%~dp0"

echo.
echo  ============================================================
echo   ECHO  -  start
echo  ============================================================
echo.

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp04-启动ECHO.ps1" -Supervise %*
set "RC=%ERRORLEVEL%"

echo.
if not "%RC%"=="0" (
    echo  [x] start failed, exit code %RC%.
    echo      Try: 7-状态ECHO.cmd -LogTail 30
) else (
    echo  [ok] ECHO is running.
)
echo.
pause
exit /b %RC%
