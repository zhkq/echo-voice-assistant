@echo off
REM =====================================================================
REM  ECHO - STOP (double-click me)
REM  Calls 5-停止ECHO.ps1 : stops the service, sidebar, watchdog and the
REM  switch supervisor. Data and autostart are NOT touched.
REM  It refuses while a meeting is recording, unless -Force is passed.
REM  Keep this file ASCII-only.
REM =====================================================================
chcp 65001 >nul
setlocal EnableExtensions
cd /d "%~dp0"

echo.
echo  ============================================================
echo   ECHO  -  stop
echo  ============================================================
echo.

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp05-停止ECHO.ps1" %*
set "RC=%ERRORLEVEL%"

echo.
if not "%RC%"=="0" (
    echo  [x] stop failed/refused, exit code %RC%.
    echo      Read the message above.
) else (
    echo  [ok] ECHO stopped. Autostart still works at next logon.
)
echo.
pause
exit /b %RC%
