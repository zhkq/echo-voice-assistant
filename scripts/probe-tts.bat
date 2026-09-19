@echo off
rem probe-tts.bat - double-click to run the ECHO audio self check (dev tree).
rem Waits for ENTER between sections, so each sound can be judged on its own.
rem Self-locating (%~dp0): never hardcode a path.
rem Optional args are passed through, e.g.:
rem     probe-tts.bat --beep done --times 3      (repeat ONE beep, one y/n per play)
setlocal
chcp 65001 >nul 2>&1
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
set "HERE=%~dp0"
set "ROOT=%HERE%.."
echo ECHO tree : %ROOT%
echo.
"%ROOT%\venv\Scripts\python.exe" "%HERE%probe_tts.py" %*
echo.
echo === done - full log: %ROOT%\data\logs\probe-tts-last.txt ===
pause
