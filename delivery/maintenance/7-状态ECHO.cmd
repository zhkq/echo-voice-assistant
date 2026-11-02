@echo off
REM =====================================================================
REM  ECHO - STATUS (double-click me, read-only)
REM  Calls 7-状态ECHO.ps1 : service/port, components, meetings, paths,
REM  processes; optional log tail.
REM  Arguments are passed through, e.g.   7-状态ECHO.cmd -LogTail 30
REM  Keep this file ASCII-only.
REM =====================================================================
chcp 65001 >nul
setlocal EnableExtensions
cd /d "%~dp0"

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp07-状态ECHO.ps1" %*
set "RC=%ERRORLEVEL%"

echo.
pause
exit /b %RC%
