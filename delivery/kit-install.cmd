@echo off
REM =====================================================================
REM  ECHO - DOUBLE CLICK THIS FILE to install.
REM
REM  It asks which folder to install into, whether DSH is already installed
REM  (fresh installs pass -AskDsh; upgrades ask about upgrading DSH instead),
REM  and later how the backend is reached. It then calls the installer:
REM
REM      echo-core\scripts\install-all.ps1
REM          -Profile minimal -Agent harness -Yes -AskDsh -Root "<your folder>"
REM      plus -Offline when bundle\ sits next to this file.
REM
REM  Two shapes are supported on purpose (2026-10-01):
REM
REM    A) the kit is ALREADY UNPACKED next to this file
REM       (echo-core\ + echo-install\ + bundle\ ...) -> used as is.
REM    B) only the kit ZIP is next to this file
REM       (ECHO-kit-*.zip, the shape "one script + a few zips") ->
REM       this script unpacks it into .\echo-kit\ and installs from there.
REM       You do NOT have to right-click / extract by hand.
REM
REM  The BACKEND (the GPU box that transcribes meetings) is chosen at the
REM  END of the install: the installer asks whether you have a pairing
REM  string (echo://pair?...) or want to run it on this machine. For the
REM  "on this machine" answer it looks for these two zips NEXT TO THIS
REM  FILE (that is what -BackendDir passes along):
REM
REM      ECHO-backend-portable-*.zip    thin pack (source + interpreter)
REM      ECHO-backend-offline-*.zip     offline pack (deps already inside)
REM
REM  With the offline pack nothing is downloaded for the backend; without
REM  any of the two, the installer just says where to put it later.
REM
REM  * bundle\ present  -> the Python runtime, the dependencies and the
REM    model all come out of the kit: nothing is downloaded for them.
REM    The DSH agent: ECHO first looks for an already installed official
REM    DeepSeek Harness desktop (its bundled dsh CLI needs no Node/npm).
REM    If absent, it downloads the official Windows installer (latest,
REM    ~277 MB) and installs it silently. Only if that fails does it fall
REM    back to the old node/npm install.
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

REM %~dp0 ends with a backslash: KIT needs it ("%KIT%bundle\wheels"), but a
REM trailing backslash inside a quoted PowerShell argument would escape the
REM closing quote - so keep a stripped copy for -BackendDir.
set "KIT=%~dp0"
set "HERE=%~dp0"
if "%HERE:~-1%"=="\" set "HERE=%HERE:~0,-1%"

:havetree
set "ENTRY=%KIT%echo-core\scripts\install-all.ps1"
if not exist "%ENTRY%" set "ENTRY=%KIT%ECHO\scripts\install-all.ps1"
if exist "%ENTRY%" goto :havekit

REM ---- shape B: only the kit zip is here -> unpack it (once) and retry
if defined TRIED goto :noentry
set "TRIED=1"
set "KITZIP="
for %%f in ("%HERE%\ECHO-kit-*.zip") do (
  echo "%%~nxf" | findstr /i "macos" >nul
  if errorlevel 1 set "KITZIP=%%~ff"
)
if not defined KITZIP goto :noentry

set "UNPACK=%HERE%\echo-kit"
echo.
echo   Unpacking %KITZIP%
echo   into      %UNPACK%
if exist "%UNPACK%" rd /s /q "%UNPACK%"
mkdir "%UNPACK%"
tar -xf "%KITZIP%" -C "%UNPACK%"
if errorlevel 1 goto :unpackfail
set "TOP="
for /d %%d in ("%UNPACK%\*") do set "TOP=%%~fd"
if defined TOP (set "KIT=%TOP%\") else (set "KIT=%UNPACK%\")
goto :havetree

:havekit

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

REM Normalize what people actually paste:
REM   * Explorer's "copy as path" gives "C:\ECHO" (quotes included);
REM   * a trailing backslash turns -Root "C:\ECHO\" into an escaped quote and
REM     the next switch (-BackendDir ...) gets swallowed into the value.
REM Both showed up in the field (2026-10-21: typing C:\echo reported a bad
REM path). Strip quotes and one trailing backslash BEFORE building ARGS.
set "ROOT=%ROOT:"=%"
if "%ROOT:~-1%"=="\" set "ROOT=%ROOT:~0,-1%"
if not defined ROOT set "ROOT=%DEF%"

REM ---- existing install? say what an upgrade will and will not touch ----
REM WHY: colleagues re-run this same file to update. Without a word they cannot
REM tell whether their data/models/backend pairing/agent settings survive, and
REM the old flow used to overwrite engine + agent settings (fixed 2026-10-23).
set "EXISTING="
if exist "%ROOT%\echo-core\app\main.py" set "EXISTING=1"
if exist "%ROOT%\app\main.py" set "EXISTING=1"
if defined EXISTING (
  echo.
  echo   Found an existing ECHO in this folder - this run UPGRADES it.
  echo     updates : program code only ^(UI + core features^)
  echo     keeps   : database + settings, meetings, model weights,
  echo               backend pairing, phone/watch settings, agent settings
  echo     asks    : whether to upgrade DSH ^(optional; agent settings stay unchanged^)
  echo     note    : it stops ECHO briefly; refuse if a meeting is recording.
)

REM Upgrades are interactive (they ask about DSH); fresh installs keep -Yes.
set "ARGS=-Profile minimal -Agent harness -Root "%ROOT%" -BackendDir "%HERE%""
if not defined EXISTING set "ARGS=%ARGS% -Yes"
REM Fresh install: ask whether an existing DSH is already on this machine
REM (install-all.ps1 -AskDsh does the probing). Upgrades already ask about
REM upgrading DSH, so they deliberately do not pass this.
if not defined EXISTING set "ARGS=%ARGS% -AskDsh"
if exist "%KIT%bundle\wheels" (
  set "ARGS=%ARGS% -Offline"
  echo.
  echo   bundle\ found - installing offline: the runtime, the dependencies
  echo   and the model come from this kit. The agent needs an already
  echo   installed official DeepSeek Harness desktop; otherwise ECHO falls
  echo   back to npm ^(which needs a network connection^).
) else (
  echo.
  echo   no bundle\ here - runtime, dependencies and model come from the
  echo   internet. This needs a working connection, and takes longer.
)
echo.
echo   Folder : %ROOT%
echo   This takes a few minutes; the panel address is printed at the end.
echo   The agent: an already installed DeepSeek Harness desktop is reused;
echo   otherwise its official Windows installer (~277 MB) is downloaded.
echo   At the end it also asks how the BACKEND (GPU transcription) is
echo   reached: paste a pairing string, or run it on this machine.
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

:unpackfail
echo.
echo  [x] could not unpack %KITZIP%
echo      The zip may be incomplete (copy it again), or "tar" is missing
echo      (it ships with Windows 10 1803 and later).
echo.
pause
exit /b 1

:noentry
echo.
echo  [x] install-all.ps1 was not found, and no kit zip was found either.
echo      Looked for:
echo        %KIT%echo-core\scripts\install-all.ps1
echo        %KIT%ECHO\scripts\install-all.ps1
echo        %HERE%\ECHO-kit-*.zip   (would be unpacked automatically)
echo      Put THIS file next to ECHO-kit-*.zip, or unpack the kit zip and
echo      double click the launcher cmd inside it (same file name as this
echo      one). The folder layout matters.
echo.
pause
exit /b 1
