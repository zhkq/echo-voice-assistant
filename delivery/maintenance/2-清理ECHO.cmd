@echo off
REM =====================================================================
REM  ECHO - CLEAN UP for reinstall (double-click me)
REM  Calls 2-清理ECHO.ps1 . It REFUSES to run unless a meeting-data
REM  backup exists (run 1-备份ECHO会议数据.cmd first), and it asks you
REM  to type CLEAN before deleting anything.
REM
REM  Arguments are passed through, e.g.:
REM     2-清理ECHO.cmd -DryRun
REM     2-清理ECHO.cmd -IncludeDev
REM  Keep this file ASCII-only.
REM =====================================================================
chcp 65001 >nul
setlocal EnableExtensions
cd /d "%~dp0"

echo.
echo  ============================================================
echo   ECHO  -  clean up before reinstall
echo  ============================================================
echo   This will REMOVE: the ECHO install folder, its cache, the
echo   autostart entry, shortcuts, scheduled task and firewall rule.
echo.
echo   It will NOT remove: the meeting-data backup (ECHO-backup\...),
echo   the delivery kit folder, C:\echo-dev (dev source tree), or the
echo   Obsidian notes.
echo.
echo   Safety: it stops unless a meeting-data backup is found.
echo   Tip: run "2-清理ECHO.cmd -DryRun" first to see the list only.
echo.

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp02-清理ECHO.ps1" %*
set "RC=%ERRORLEVEL%"

echo.
if not "%RC%"=="0" (
    echo  [x] cleanup stopped/failed, exit code %RC%.
    echo      Nothing else was touched; read the message above.
) else (
    echo  [ok] cleanup finished.
    echo       Next: install ECHO from the delivery kit (installer cmd),
    echo       then double-click 3-还原ECHO会议数据.cmd
)
echo.
pause
exit /b %RC%
