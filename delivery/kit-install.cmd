@echo off
REM =====================================================================
REM  ECHO - DOUBLE CLICK THIS FILE to install.
REM
REM  It asks ONE question (which folder to install into) and then calls
REM  the installer that ships inside this kit:
REM
REM      echo-core\scripts\install-all.ps1
REM          -Profile minimal -Agent harness -Yes -Root "<your folder>"
REM      plus -Offline when bundle\ sits next to this file.
REM
REM  * bundle\ present  -> the Python runtime, the dependencies and the
REM    model all come out of the kit: nothing is downloaded for them.
REM    The DSH standard edition (the agent) still comes from npm, which
REM    is the one download that is allowed.
REM  * no bundle\       -> runtime, dependencies and model come from the
REM    internet (python.org / PyPI mirrors / ModelScope).
REM
REM  The panel address is printed at the end. This window always ends in
REM  a pause, so an error message stays readable instead of flashing by.
REM
REM  Keep this file ASCII-only: cmd.exe reads .cmd in the console code
REM  page, and an ASCII body is safe in every code page. build_kit.py
REM  also refuses to copy a non-ASCII launcher into the kit.
REM =====================================================================

setlocal EnableExtensions
cd /d "%~dp0"

set "KIT=%~dp0"
set "ENTRY=%KIT%echo-core\scripts\install-all.ps1"
if not exist "%ENTRY%" set "ENTRY=%KIT%ECHO\scripts\install-all.ps1"
if not exist "%ENTRY%" goto :noentry

set "DEF=D:\ECHO"
if not exist "D:\" set "DEF=C:\ECHO"

echo.
echo  ============================================================
echo   ECHO  -  one-click install
echo  ============================================================
echo.
echo   Install to which folder?  Press Enter for the default.
set "ROOT="
set /p "ROOT=  Folder [%DEF%]: "
if not defined ROOT set "ROOT=%DEF%"

set "ARGS=-Profile minimal -Agent harness -Yes -Root "%ROOT%""
if exist "%KIT%bundle\wheels" (
  set "ARGS=%ARGS% -Offline"
  echo.
  echo   bundle\ found - installing offline: the runtime, the dependencies
  echo   and the model come from this kit. The only thing fetched from the
  echo   network is the DSH standard edition.
) else (
  echo.
  echo   no bundle\ here - runtime, dependencies and model come from the
  echo   internet. This needs a working connection, and takes longer.
)
echo.
echo   Folder : %ROOT%
echo   This takes a few minutes; the panel address is printed at the end.
echo.

powershell -NoProfile -ExecutionPolicy Bypass -File "%ENTRY%" %ARGS%
set "RC=%ERRORLEVEL%"

echo.
if not "%RC%"=="0" (
  echo  [x] install failed, exit code %RC%.
  echo      Log: %ROOT%\data\logs\install-all.log
  echo      Send that log back and we will have a look at it.
) else (
  echo  [ok] install finished. Open the panel address printed above.
)
echo.
pause
exit /b %RC%

:noentry
echo.
echo  [x] install-all.ps1 was not found inside this kit. Looked for:
echo      %KIT%echo-core\scripts\install-all.ps1
echo      %KIT%ECHO\scripts\install-all.ps1
echo      Unpack the whole kit zip again - the folder layout matters.
echo.
pause
exit /b 1
