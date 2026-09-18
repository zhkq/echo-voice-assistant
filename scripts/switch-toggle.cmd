@echo off
REM =====================================================================
REM switch-toggle.cmd - double-click to flip between the stable install and
REM the dev install.  Thin wrapper around scripts\switch-instance.ps1 -Toggle.
REM
REM Extra arguments are forwarded, e.g.:
REM     switch-toggle.cmd -Force          (ignore an in-progress meeting check)
REM     switch-toggle.cmd -DryRun         (show what it would do)
REM     switch-toggle.cmd -Status         (just report, do not switch)
REM
REM ASCII-ONLY on purpose (see scripts\echo-instance-lib.ps1).
REM =====================================================================
setlocal
set "PS1=%~dp0switch-instance.ps1"
if not exist "%PS1%" (
    echo [x] not found: "%PS1%"
    echo     run this file from the ECHO scripts folder.
    pause
    exit /b 1
)
if "%~1"=="" (
    powershell -NoProfile -ExecutionPolicy Bypass -File "%PS1%" -Toggle
) else (
    powershell -NoProfile -ExecutionPolicy Bypass -File "%PS1%" %*
)
set "RC=%ERRORLEVEL%"
echo.
if not "%RC%"=="0" echo [x] switch failed ^(exit %RC%^) - scroll up for details.
pause
endlocal
exit /b %RC%
