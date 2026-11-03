@echo off
REM =====================================================================
REM  ECHO - RESTORE meeting data (double-click me)
REM  Calls 3-还原ECHO会议数据.ps1
REM
REM  Default mode = meetings only (keeps the fresh settings).
REM  Pass -Mode Full to replace the whole database instead.
REM  Keep this file ASCII-only.
REM =====================================================================
chcp 65001 >nul
setlocal EnableExtensions
cd /d "%~dp0"

echo.
echo  ============================================================
echo   ECHO  -  restore meeting data
echo  ============================================================
echo   Puts the backed-up meetings, speakers, transcripts and
echo   summaries back into the freshly installed ECHO.
echo   It finds the newest backup automatically, or pass:
echo       -BackupPath "D:\ECHO-backup\ECHO-20261102-103000"
echo   Modes:
echo       (default)      meetings only, keeps fresh settings
echo       -Mode Full     replace the whole database
echo.

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp03-还原ECHO会议数据.ps1" %*
set "RC=%ERRORLEVEL%"

echo.
if not "%RC%"=="0" (
    echo  [x] restore failed, exit code %RC%.
    echo      Read the message above; the backup was not modified.
) else (
    echo  [ok] restore finished.
    echo       Open the panel address printed above and check a meeting.
)
echo.
pause
exit /b %RC%
