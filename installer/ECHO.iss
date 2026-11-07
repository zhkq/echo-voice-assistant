; ============================================================================
; ECHO.iss - single-file, current-user ECHO installer (phase 1, unsigned)
;
; Layout:
;   %LOCALAPPDATA%\Programs\ECHO\ECHO.exe        launcher (NativeAOT)
;   %LOCALAPPDATA%\Programs\ECHO\echo-core\      app/web/assets/docs/.dsh/scripts/sidebar
;   %LOCALAPPDATA%\Programs\ECHO\runtime-core\   relocatable CPython + deps
;   %LOCALAPPDATA%\ECHO\data\                    FIXED data root, never removed
;
; Deliberate non-goals:
;   * no admin/UAC             -> PrivilegesRequired=lowest, HKCU only
;   * no DSH payload           -> the first-run wizard installs the official desktop
;   * no capability backend    -> it is a multi-GB separate download
;   * no self-signed signing   -> phase 1 stays unsigned; phase 2 uses a real cert
; ============================================================================

#ifndef AppVersion
  #define AppVersion "3.0.1"
#endif
#ifndef VersionInfoVersion
  #define VersionInfoVersion "3.0.1.0"
#endif
#ifndef StageDir
  #define StageDir "..\dist\installer-stage"
#endif
#ifndef OutputDir
  #define OutputDir "..\dist"
#endif

[Setup]
; A fixed AppId is what makes an upgrade replace this install instead of creating a
; second one. Never regenerate it.
AppId={{0343EF50-9805-4751-B4E2-30CE838AD4C6}
AppName=ECHO
AppVersion={#AppVersion}
AppVerName=ECHO {#AppVersion}
AppPublisher=ECHO
DefaultDirName={localappdata}\Programs\ECHO
DefaultGroupName=ECHO
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
OutputDir={#OutputDir}
OutputBaseFilename=ECHO-Setup-{#AppVersion}
Compression=lzma2/ultra64
SolidCompression=yes
LZMAUseSeparateProcess=yes
WizardStyle=modern
SetupIconFile=assets\echo.ico
UninstallDisplayIcon={app}\ECHO.exe
VersionInfoVersion={#VersionInfoVersion}
VersionInfoProductName=ECHO
VersionInfoDescription=ECHO 安装程序
VersionInfoCompany=ECHO
CloseApplications=no
RestartApplications=no
ShowLanguageDialog=auto

[Languages]
Name: "chinesesimplified"; MessagesFile: "languages\ChineseSimplified.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: checkedonce
Name: "autostart"; Description: "开机时自动启动 ECHO（仅当前用户）"; GroupDescription: "启动选项："; Flags: unchecked

[Files]
Source: "{#StageDir}\ECHO.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#StageDir}\echo-core\*"; DestDir: "{app}\echo-core"; Flags: ignoreversion recursesubdirs createallsubdirs; Excludes: "*.pdb"
Source: "{#StageDir}\runtime-core\*"; DestDir: "{app}\runtime-core"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "assets\echo.ico"; DestDir: "{app}"; Flags: ignoreversion

[Dirs]
; The data root is intentionally outside {app} and marked never-uninstall.  ECHO also
; keeps models/meeting/backend below %LOCALAPPDATA%\ECHO; those directories are created
; by the program, not by [Files], so Inno's uninstaller never sees their contents.
Name: "{localappdata}\ECHO\data"; Flags: uninsneveruninstall

[Icons]
Name: "{autoprograms}\ECHO\ECHO"; Filename: "{app}\ECHO.exe"; WorkingDir: "{app}"
Name: "{autoprograms}\ECHO\卸载 ECHO"; Filename: "{uninstallexe}"
Name: "{autodesktop}\ECHO"; Filename: "{app}\ECHO.exe"; WorkingDir: "{app}"; Tasks: desktopicon

[Registry]
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "ECHO"; ValueData: """{app}\ECHO.exe"" --autostart"; Flags: uninsdeletevalue; Tasks: autostart

[Run]
Filename: "{app}\ECHO.exe"; Description: "立即启动 ECHO（首次启动会打开设置向导）"; Flags: nowait postinstall skipifsilent

[Code]
var
  WasUpgrade: Boolean;
  DataRoot: String;

function StopEcho(out ExitCode: Integer): Boolean;
var
  ResultCode: Integer;
begin
  ExitCode := 0;
  Result := True;
  if not FileExists(ExpandConstant('{app}\ECHO.exe')) then
    Exit;
  if not Exec(ExpandConstant('{app}\ECHO.exe'), '--stop --quiet',
              ExpandConstant('{app}'), SW_HIDE, ewWaitUntilTerminated, ResultCode) then
  begin
    ExitCode := -1;
    Result := False;
    Exit;
  end;
  ExitCode := ResultCode;
  Result := (ResultCode = 0);
end;

function InitializeSetup(): Boolean;
begin
  DataRoot := ExpandConstant('{localappdata}') + '\ECHO\data';
  Result := True;
end;

function InitializeUninstall(): Boolean;
begin
  DataRoot := ExpandConstant('{localappdata}') + '\ECHO\data';
  Result := True;
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  Code: Integer;
begin
  Result := '';

  // The installer knows whether this is a fresh install or an overwrite; the app relies
  // on this file to decide whether to force the first-run wizard.
  WasUpgrade := FileExists(ExpandConstant('{app}\echo-core\app\main.py')) or
                FileExists(DataRoot + '\echo.db') or
                DirExists(ExpandConstant('{app}\echo-core'));

  if not StopEcho(Code) then
  begin
    if Code = 2 then
      Result := 'ECHO 正在录音或正在处理指令，不能覆盖安装。请先结束会议/等待指令完成，再点一次“安装”。'
    else
      Result := '无法停止正在运行的 ECHO（退出码 ' + IntToStr(Code) + '）。' + #13#10 +
                '请先在任务管理器结束 ECHO/pythonw 后重试；如果问题持续，请看 ' +
                DataRoot + '\logs\launcher.log。';
    Exit;
  end;
end;

procedure WriteInstallMode();
var
  Mode: String;
  Json: String;
begin
  ForceDirectories(DataRoot);
  if WasUpgrade then
    Mode := 'upgrade'
  else
    Mode := 'fresh';
  // ASCII-only JSON on purpose: Inno's SaveStringToFile writes ANSI, and the file is
  // valid UTF-8 as long as every byte is ASCII (the app reads it with utf-8-sig).
  Json := '{"mode":"' + Mode + '","note":"inno-installer","at":"' +
          GetDateTimeString('yyyy-mm-dd hh:nn:ss', '-', ':') + '"}';
  if not SaveStringToFile(DataRoot + '\install-mode.json', Json, False) then
    SuppressibleMsgBox('无法写入 ' + DataRoot + '\install-mode.json。' + #13#10 +
           'ECHO 仍会安装，但首次启动可能不会自动进入向导。', mbError, MB_OK, IDOK);
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssPostInstall then
    WriteInstallMode();
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  Code: Integer;
begin
  if CurUninstallStep = usUninstall then
  begin
    if not StopEcho(Code) then
    begin
      if Code = 2 then
        SuppressibleMsgBox('ECHO 正在录音或正在处理指令，不能卸载。请先结束会议/等待指令完成。', mbError, MB_OK, IDOK)
      else
        SuppressibleMsgBox('无法停止正在运行的 ECHO（退出码 ' + IntToStr(Code) + '）。' + #13#10 +
               '请先在任务管理器结束 ECHO/pythonw 后重新卸载。', mbError, MB_OK, IDOK);
      Abort;
    end;
  end;

  if CurUninstallStep = usPostUninstall then
    SuppressibleMsgBox('ECHO 的程序文件已删除。' + #13#10 + #13#10 +
           '你的数据仍保留在：' + #13#10 + DataRoot + #13#10 + #13#10 +
           '会议、模型、后端配对与设置都不会被卸载程序删除。', mbInformation, MB_OK, IDOK);
end;
