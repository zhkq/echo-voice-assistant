@echo off
REM =====================================================================
REM  ECHO offline installer entry point - DOUBLE CLICK THIS FILE.
REM
REM  It calls install-offline.ps1 in this folder, which calls
REM  ECHO\scripts\install-all.ps1 -Offline -Yes -Agent none
REM  (wheels come from bundle\wheels, the model from bundle\models,
REM   so nothing is downloaded and no AI agent is involved).
REM
REM  Keep this file ASCII-only: cmd.exe reads .cmd in the console ANSI
REM  code page, and an ASCII body is safe in every code page.
REM
REM  Extra switches are forwarded, e.g.:
REM     install-offline.ps1 -Root D:\ECHO -Profile main
REM =====================================================================

setlocal
cd /d "%~dp0"

echo.
echo  ============================================
echo   ECHO  -  offline install (no network needed)
echo  ============================================
echo.

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install-offline.ps1" %*

echo.
echo  Done. Log: ^<install root^>\data\logs\install-all.log
echo.
pause
