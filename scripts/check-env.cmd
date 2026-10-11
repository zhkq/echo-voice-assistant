@echo off
REM Pre-install environment check for ECHO (READ-ONLY). ASCII-ONLY ON PURPOSE.
REM 2026-10-11: exists because a fresh install on a test machine picked an OLD
REM harness version (node was a packaged build: npm/npx sit next to node.exe but
REM are not on PATH), and because the first-run wizard only appears on a FRESH
REM (empty) target folder.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0check-env.ps1" %*
echo.
pause
