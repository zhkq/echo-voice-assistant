# =====================================================================
# switch-instance.ps1 - flip which ECHO install is the active one
#
# Two installs are described in %USERPROFILE%\.echo-instances.json:
#   stable -> a frozen tree (e.g. C:\echo1.0)   dev -> the working tree
#
# The `current` field of that file IS the switch. One resident supervisor
# (scripts\echo-supervisor.ps1) reads it and keeps the active instance up.
# This script writes the flag, then does the stop/start immediately so the
# switch feels instant instead of waiting for the next supervisor pass.
#
# Usage
#   powershell -File scripts\switch-instance.ps1 -Init        # write a template
#   powershell -File scripts\switch-instance.ps1 -Status
#   powershell -File scripts\switch-instance.ps1 stable
#   powershell -File scripts\switch-instance.ps1 -Toggle      # flip to the other
#   powershell -File scripts\switch-instance.ps1 dev -DryRun
#   powershell -File scripts\switch-instance.ps1 dev -Force   # ignore active meeting
#   powershell -File scripts\switch-instance.ps1 -InstallAutostart
#
# Refuses to switch while the running instance is recording a meeting
# (unless -Force) - a hard kill would lose that recording.
#
# ASCII-ONLY on purpose (see echo-instance-lib.ps1).
# =====================================================================
param(
    [Parameter(Position = 0)][string]$Use = '',
    [switch]$Toggle,
    [switch]$Status,
    [switch]$Init,
    [switch]$InstallAutostart,
    [switch]$SyncTooling,
    [switch]$DryRun,
    [switch]$Force,
    [string]$Config = '',
    [int]$WaitSeconds = 90
)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'echo-instance-lib.ps1')

$script:CfgPath = if ($Config) { $Config } else { $script:ConfigPathDefault }
$repoRoot = Get-EchoRepoRoot

function Ok([string]$m)    { Write-Host "  [ok]   $m" -ForegroundColor Green }
function Warn2([string]$m) { Write-Host "  [warn] $m" -ForegroundColor Yellow }
function Fail([string]$m)  { Write-Host "  [fail] $m" -ForegroundColor Red }
function Say([string]$m)   { Write-Host $m }

Write-Host ''
Write-Host '  === ECHO instance switch ==='

if ($Init) {
    if (Test-Path $script:CfgPath) {
        Warn2 "$($script:CfgPath) already exists - left untouched"
    } else {
        New-EchoInstanceTemplate $repoRoot | Set-Content $script:CfgPath -Encoding UTF8
        Ok "template written: $($script:CfgPath)"
        Say '    edit it if your frozen tree lives elsewhere, then run -Status'
    }
    Write-Host ''
    exit 0
}

$cfg = Get-EchoInstanceConfig $script:CfgPath
if (-not $cfg) {
    Fail "no config at $($script:CfgPath) - run with -Init first"
    Write-Host ''
    exit 1
}
$names = Get-EchoInstanceNames $cfg

# --------------------------------------------------- repoint autostart
if ($InstallAutostart) {
    $vbsPath = Join-Path $repoRoot 'scripts\echo-switch-startup.vbs'
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
    $startup = [Environment]::GetFolderPath('Startup')
    $lnkPath = Join-Path $startup 'ECHO startup.lnk'
    $legacy = Join-Path $startup 'ECHO (switch).lnk'
    if ($DryRun) {
        Say "  [dry] would write $vbsPath"
        Say "  [dry] would repoint $lnkPath at it"
        Write-Host ''
        exit 0
    }
    $ansi = [System.Text.Encoding]::GetEncoding(
        [System.Globalization.CultureInfo]::CurrentCulture.TextInfo.ANSICodePage)
    [System.IO.File]::WriteAllText($vbsPath, $vbsBody, $ansi)
    $ws = New-Object -ComObject WScript.Shell
    if (Test-Path $legacy) { Remove-Item $legacy -Force -ErrorAction SilentlyContinue }
    $lnk = $ws.CreateShortcut($lnkPath)
    $lnk.TargetPath = "$env:SystemRoot\System32\wscript.exe"
    $lnk.Arguments = "`"$vbsPath`""
    $lnk.WorkingDirectory = $repoRoot
    $lnk.Description = 'ECHO autostart (switch supervisor)'
    $lnk.Save()
    Ok "autostart repointed: $lnkPath"
    Say "    -> wscript $vbsPath   (-> scripts\echo-supervisor.ps1)"
    Say '    the supervisor now decides which instance comes up after logon.'
    Warn2 'Kill any old per-install supervisor, otherwise two loops will fight:'
    Say  '      powershell -File scripts\switch-instance.ps1 -Status'
    Write-Host ''
    exit 0
}

# --------------------------------------------------- sync tooling
# The two installs are INDEPENDENT physical copies, so a fix to the operational
# scripts (this file, echo-instance-lib.ps1, echo-supervisor.ps1, startup.ps1,
# start.ps1, ...) does NOT reach the other tree by itself. That matters because
# the frozen tree's own scripts\startup.ps1 IS the watchdog that runs there.
#
# Direction: FROM the tree this script lives in, unless -Use names another one.
# Do NOT default to config.current - that is the instance that is RUNNING, not
# the tree you are developing in (getting this backwards once reverted the dev
# tree's tooling to the frozen copy).
# Only scripts\*.ps1 / *.cmd are copied; app\ and web\ are never touched, so a
# frozen install stays frozen.
if ($SyncTooling) {
    $fromRoot = $repoRoot
    if ($Use) { $fromRoot = Get-EchoInstanceRoot $cfg $Use }
    if (-not (Test-Path $fromRoot)) { Fail "source root not found: $fromRoot"; exit 1 }
    $srcDir = Join-Path $fromRoot 'scripts'
    Say "  sync tooling from: $fromRoot"
    foreach ($n in $names) {
        $toRoot = Get-EchoInstanceRoot $cfg $n
        if ((Test-Path $toRoot) -and
            ((Resolve-Path $toRoot).Path -eq (Resolve-Path $fromRoot).Path)) { continue }
        if (-not (Test-Path $toRoot)) { Warn2 "$n : root missing, skipped"; continue }
        $dstDir = Join-Path $toRoot 'scripts'
        foreach ($pat in @('*.ps1', '*.cmd')) {
            Get-ChildItem (Join-Path $srcDir $pat) -File -ErrorAction SilentlyContinue | ForEach-Object {
                $dst = Join-Path $dstDir $_.Name
                $same = (Test-Path $dst) -and
                        ((Get-FileHash $_.FullName).Hash -eq (Get-FileHash $dst).Hash)
                if ($same) { Say "    $n : $($_.Name) unchanged" }
                else {
                    Say "    $n : copy $($_.Name)"
                    if (-not $DryRun) { Copy-Item $_.FullName $dst -Force }
                }
            }
        }
    }
    Say ''
    Warn2 "restart the other install's watchdog for startup.ps1 changes to take effect:"
    Say  "      switch-instance.ps1 <name>"
    Say ''
    exit 0
}

# --------------------------------------------------- status
function Show-Status {
    Say ''
    Say ("  config: {0}" -f $script:CfgPath)
    Say ("  autoStopOthers: {0}" -f $cfg.autoStopOthers)
    Say ''
    Say ("  {0} {1,-8} {2,-9} {3,-6} {4,-12} {5}" -f ' ', 'NAME', 'ROOT', 'ECHO', 'PORT', 'SUP/ROUTER')
    $procs = Get-EchoProcSnapshot
    foreach ($n in $names) {
        $root = Get-EchoInstanceRoot $cfg $n
        $port = Get-EchoPortFromFile $root
        $pids = @(Get-EchoInstanceProcs $root $procs)
        $echo = @($pids | Where-Object { $_.Kind -eq 'echo' }).Count
        $sup = @($pids | Where-Object { $_.Kind -eq 'supervisor' }).Count
        $rtr = @($pids | Where-Object { $_.Kind -eq 'router' -and $_.Role -ne 'stub' }).Count
        $mark = if ($cfg.current -eq $n) { '*' } else { ' ' }
        Say ("  {0} {1,-8} {2,-9} {3,-6} {4,-12} {5}/{6}" -f $mark, $n,
            $(if (Test-Path $root) { 'ok' } else { 'MISSING' }),
            $(if ($echo -gt 0) { 'run' } else { '-' }),
            $(if ($port -gt 0) { "$port" } else { '-' }),
            $sup, $rtr)
    }
    Say ''
    Say '  * = current (the supervisor keeps this one up)'
    Say '  NOTE: on Windows each venv launch shows as a stub+child PAIR = ONE instance.'
    Say ''
}

if ($Status -or (-not $Use -and -not $Toggle)) {
    Show-Status
    exit 0
}

# --------------------------------------------------- switch
$target = $Use
if ($Toggle) {
    $cur = [string]$cfg.current
    $target = @($names | Where-Object { $_ -ne $cur }) | Select-Object -First 1
    if (-not $target) { Fail 'only one instance configured - nothing to toggle to'; exit 1 }
    Say "  toggle: $cur -> $target"
}
if ($names -notcontains $target) {
    Fail "unknown instance '$target' (have: $($names -join ', '))"
    exit 1
}
$targetRoot = Get-EchoInstanceRoot $cfg $target
if (-not (Test-Path $targetRoot)) {
    Fail "$target : root not found: $targetRoot"
    Say  '    (freeze your stable tree there first, or edit the config)'
    exit 1
}

# 1) write the switch first: even if this script is interrupted, the resident
#    supervisor converges to the new target on its next pass.
if (-not $DryRun) {
    $cfg.current = $target
    Save-EchoInstanceConfig $script:CfgPath $cfg
    Ok "switch written: current=$target"
} else {
    Say "  [dry] would write current=$target"
}

# 2) stop the others (fast path; the supervisor would only warn by default)
foreach ($n in $names) {
    if ($n -eq $target) { continue }
    Say "  stop $n"
    $stopLog = { param($m) Say $m }
    if (-not (Stop-EchoInstance -Root (Get-EchoInstanceRoot $cfg $n) -Name $n `
                -SkipMeetingCheck:$Force -DryRun:$DryRun -Log $stopLog)) {
        Fail 'aborted (use -Force to override, or stop the meeting first)'
        exit 1
    }
}

# 3) make sure the target is up
# IMPORTANT: decide by "does the TARGET TREE own the running ECHO", not by the
# port alone - both installs share the same port (18060) in this serial setup, so
# a port probe reports "already running" while the OTHER install is the one
# serving. That bug made a dry run claim dev was up while stable held the port,
# and would silently skip starting the target after a half-finished stop.
$port = Get-EchoPortFromFile $targetRoot
$tprocs = @(Get-EchoInstanceProcs $targetRoot (Get-EchoProcSnapshot) | Where-Object { $_.Kind -eq 'echo' })
if ($tprocs.Count -gt 0 -and (Test-EchoPortListening $port)) {
    Ok "$target already running on port $port (pid $($tprocs[0].PID))"
} elseif ($tprocs.Count -gt 0) {
    Ok "$target is already running (pid $($tprocs[0].PID)) - waiting for port $port"
    for ($i = 0; $i -lt $WaitSeconds; $i++) {
        Start-Sleep -Seconds 1
        if (Test-EchoPortListening $port) { break }
    }
    if (Test-EchoPortListening $port) { Ok "$target is up on port $port" }
    else { Fail "$target never became reachable on port $port"; exit 1 }
} elseif (Test-EchoPortListening $port) {
    if ($DryRun) {
        Say "  [dry] port $port is currently served by another instance; the stop above would free it"
        Say "  [dry] would start $target"
    } else {
        Fail "$target is NOT running, but port $port is already served by something else."
        Say  "    stop it first (switch-instance.ps1 -Status), then retry."
        exit 1
    }
} else {
    Say "  start $target"
    $startLog = { param($m) Say $m }
    $p = Start-EchoInstance -Root $targetRoot -Name $target -WaitSeconds $WaitSeconds `
                            -DryRun:$DryRun -Log $startLog
    if ($p -eq 0) { exit 1 }
    if ($p -gt 0) { $port = $p }
}

Say ''
Say "  now serving: $target   ($targetRoot)"
if ($port -gt 0) { Say "  panel:  http://127.0.0.1:$port" }
Say ''
exit 0
