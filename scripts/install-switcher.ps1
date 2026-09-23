# =====================================================================
# install-switcher.ps1 - install the stable/dev switcher OUTSIDE the repo.
#
# WHY THIS EXISTS (2026-09-23)
#   The switcher keeps "the instance named in %USERPROFILE%\.echo-instances.json"
#   alive, and it is what the logon autostart runs. It used to live in the dev
#   tree (C:\echo-dev\scripts\...), which made the STABLE install depend on the
#   dev tree existing: delete or move the repo and stable ECHO would not come up
#   after logon. Deployment plumbing must not live inside either install.
#
# WHAT IT DOES
#   1. copies echo-supervisor.ps1 + echo-instance-lib.ps1 into %USERPROFILE%\.echo-switch\
#   2. writes %USERPROFILE%\.echo-switch\echo-switch-startup.vbs (self-locating, ANSI)
#   3. repoints the Startup shortcut "ECHO startup.lnk" at that vbs
#   4. replaces the running (repo-based) supervisor with the new one, so exactly
#      one loop is alive - the loop itself never restarts ECHO, it only keeps the
#      ACTIVE instance up
#
# USAGE
#   powershell -File scripts\install-switcher.ps1 -DryRun
#   powershell -File scripts\install-switcher.ps1            # install / refresh
#
# Re-run it after changing echo-supervisor.ps1 in the repo (that is the "refresh").
# NOTE: prefer this over `switch-instance.ps1 -InstallAutostart`, which points the
# autostart back into the repo.
#
# ASCII-ONLY on purpose (see echo-instance-lib.ps1).
# =====================================================================
param(
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path $PSScriptRoot -Parent
$dir = Join-Path $env:USERPROFILE '.echo-switch'

function Say([string]$m)  { Write-Host "  $m" }
function Ok([string]$m)   { Write-Host "  [ok]   $m" -ForegroundColor Green }
function Warn2([string]$m) { Write-Host "  [warn] $m" -ForegroundColor Yellow }
function Fail([string]$m) { Write-Host "  [fail] $m" -ForegroundColor Red }

Write-Host ''
Write-Host '  === install/refresh the stable-dev switcher ==='
Say "repo : $repoRoot"
Say "home : $dir"
if ($DryRun) { Warn2 'DRY RUN - nothing will be written' }
Write-Host ''

$files = @('echo-supervisor.ps1', 'echo-instance-lib.ps1')
foreach ($f in $files) {
    if (-not (Test-Path -LiteralPath (Join-Path $repoRoot "scripts\$f"))) {
        Fail "missing in the repo: scripts\$f"; exit 1
    }
}

if (-not $DryRun) {
    New-Item -ItemType Directory -Force -Path $dir | Out-Null
    foreach ($f in $files) {
        Copy-Item -LiteralPath (Join-Path $repoRoot "scripts\$f") -Destination (Join-Path $dir $f) -Force
    }
    Ok "copied $($files.Count) script(s) to $dir"
} else {
    Say "  [dry] would copy $($files -join ', ') to $dir"
}

# --- the launcher vbs (self-locating: contains NO absolute path) -------------
$vbsPath = Join-Path $dir 'echo-switch-startup.vbs'
$vbsBody = (
    "' ECHO switch supervisor - runs echo-supervisor.ps1 with no window.`r`n" +
    "' Self-locating: contains NO absolute path, so the folder may move.`r`n" +
    "Set fso = CreateObject(`"Scripting.FileSystemObject`")`r`n" +
    "Set sh  = CreateObject(`"WScript.Shell`")`r`n" +
    "here = fso.GetParentFolderName(WScript.ScriptFullName)`r`n" +
    "sh.CurrentDirectory = here`r`n" +
    "target = fso.BuildPath(here, `"echo-supervisor.ps1`")`r`n" +
    "sh.Run `"powershell -NoProfile -ExecutionPolicy Bypass -File `"`"`" & target & `"`"`"`", 0, False`r`n"
)
if ($DryRun) {
    Say "  [dry] would write $vbsPath"
} else {
    $ansi = [System.Text.Encoding]::GetEncoding(
        [System.Globalization.CultureInfo]::CurrentCulture.TextInfo.ANSICodePage)
    [System.IO.File]::WriteAllText($vbsPath, $vbsBody, $ansi)
    Ok "wrote $vbsPath"
}

# --- repoint the logon autostart --------------------------------------------
$startup = [Environment]::GetFolderPath('Startup')
$lnkPath = Join-Path $startup 'ECHO startup.lnk'
$legacy = Join-Path $startup 'ECHO (switch).lnk'
if ($DryRun) {
    Say "  [dry] would repoint $lnkPath -> wscript.exe `"$vbsPath`""
} else {
    $ws = New-Object -ComObject WScript.Shell
    if (Test-Path -LiteralPath $legacy) { Remove-Item -LiteralPath $legacy -Force -ErrorAction SilentlyContinue }
    $lnk = $ws.CreateShortcut($lnkPath)
    $lnk.TargetPath = "$env:SystemRoot\System32\wscript.exe"
    $lnk.Arguments = "`"$vbsPath`""
    $lnk.WorkingDirectory = $dir
    $lnk.Description = 'ECHO autostart (switch supervisor, outside the repo)'
    $lnk.Save()
    Ok "autostart repointed: $lnkPath"
}

# --- exactly one supervisor loop --------------------------------------------
$running = @(Get-CimInstance Win32_Process -Filter "Name='powershell.exe'" |
             Where-Object { $_.CommandLine -match 'echo-supervisor\.ps1|startup\.ps1' })
Say "supervisor-like processes now: $($running.Count)"
foreach ($p in $running) {
    $cmd = [string]$p.CommandLine
    $why = if ($cmd -match 'echo-supervisor\.ps1') { 'switch supervisor' } else { 'per-install watchdog' }
    Say "  pid=$($p.ProcessId)  $why"
}
if ($DryRun) {
    Say '  [dry] would stop the switch supervisor (if any) and start the new one'
    Say '  [dry] would leave the per-install watchdog of the ACTIVE instance alone'
} else {
    foreach ($p in $running) {
        if ([string]$p.CommandLine -match 'echo-supervisor\.ps1') {
            Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
            Say "  stopped old supervisor pid=$($p.ProcessId)"
        }
    }
    Start-Sleep -Seconds 1
    if (Test-Path -LiteralPath $vbsPath) {
        & wscript.exe $vbsPath
        Ok 'new supervisor started'
    }
    Start-Sleep -Seconds 3
    $now = @(Get-CimInstance Win32_Process -Filter "Name='powershell.exe'" |
             Where-Object { $_.CommandLine -match 'echo-supervisor\.ps1' })
    Say "switch supervisors running now: $($now.Count)"
}

Write-Host ''
if ($DryRun) { Warn2 'dry run finished - nothing changed' } else { Ok 'switcher installed' }
exit 0
