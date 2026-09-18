# =====================================================================
# echo-instance-lib.ps1 - shared helpers for instance switching
#
# Dot-sourced by:
#   scripts\echo-supervisor.ps1   (resident: keeps the ACTIVE instance up)
#   scripts\switch-instance.ps1   (one-shot: flips the active flag)
#
# ASCII-ONLY on purpose: Windows PowerShell 5.1 parses a BOM-less .ps1 as ANSI,
# so non-ASCII literals can silently break a script. tests/test_script_encoding.py
# enforces "ASCII or BOM" for this directory.
# =====================================================================

$script:ConfigPathDefault = Join-Path $env:USERPROFILE '.echo-instances.json'

function Get-EchoRepoRoot { return (Split-Path $PSScriptRoot -Parent) }

# --------------------------------------------------------------- config
# {
#   "current": "dev",                 <- which instance SHOULD be running
#   "autoStopOthers": false,          <- true = supervisor kills the other one
#   "instances": {
#     "stable": { "root": "C:\\echo1.0" },
#     "dev":    { "root": "C:\\...\\ECHO-public" }
#   }
# }
function New-EchoInstanceTemplate([string]$repoRoot) {
    $tpl = [ordered]@{
        current         = 'dev'
        autoStopOthers  = $false
        instances       = [ordered]@{
            stable = [ordered]@{ root = 'C:\echo1.0' }
            dev    = [ordered]@{ root = $repoRoot }
        }
    }
    return ($tpl | ConvertTo-Json -Depth 4)
}

function Get-EchoInstanceConfig([string]$path) {
    if (-not (Test-Path $path)) { return $null }
    $raw = Get-Content $path -Raw -Encoding UTF8
    return ($raw | ConvertFrom-Json)
}

function Save-EchoInstanceConfig([string]$path, $cfg) {
    ($cfg | ConvertTo-Json -Depth 4) | Set-Content $path -Encoding UTF8
}

function Get-EchoInstanceNames($cfg) {
    return @($cfg.instances.PSObject.Properties | ForEach-Object { $_.Name })
}

function Get-EchoInstanceRoot($cfg, [string]$name) {
    $p = $cfg.instances.PSObject.Properties | Where-Object { $_.Name -eq $name }
    if (-not $p) { throw "no such instance: $name" }
    return [string]$p.Value.root
}

# --------------------------------------------------------------- probes
function Get-EchoPidFileValue([string]$root) {
    $f = Join-Path $root 'data\echo.pid'
    if (-not (Test-Path $f)) { return 0 }
    $v = Get-Content $f -Raw -ErrorAction SilentlyContinue
    if ($null -eq $v) { return 0 }
    $v = $v.Trim()
    if ($v -notmatch '^[0-9]+$') { return 0 }
    return [int]$v
}

function Get-EchoPortFromFile([string]$root) {
    $f = Join-Path $root 'data\echo-port.txt'
    if (-not (Test-Path $f)) { return 0 }
    $v = Get-Content $f -Raw -ErrorAction SilentlyContinue
    if ($null -eq $v) { return 0 }
    $v = $v.Trim()
    if ($v -notmatch '^[0-9]+$') { return 0 }
    return [int]$v
}

function Test-EchoPortListening([int]$port) {
    if ($port -le 0) { return $false }
    $c = New-Object System.Net.Sockets.TcpClient
    try {
        $iar = $c.BeginConnect('127.0.0.1', $port, $null, $null)
        if (-not $iar.AsyncWaitHandle.WaitOne(800)) { return $false }
        $c.EndConnect($iar)
        return $true
    } catch { return $false } finally { $c.Dispose() }
}

function Get-EchoProcSnapshot {
    return @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
             Select-Object ProcessId, Name, ParentProcessId, CommandLine, ExecutablePath)
}

# Windows + venv: `venv\Scripts\python*.exe` is a ~250 KB LAUNCHER STUB that
# spawns the real interpreter as a child. So every launch shows up as TWO
# processes - they are ONE instance. Classify by path: the stub lives under a
# Scripts\ directory, the real interpreter does not. (See docs/DEPLOY.md.)
function Test-VenvLauncherStub($proc) {
    if (-not $proc -or -not $proc.ExecutablePath) { return $false }
    return ($proc.ExecutablePath -match '\\Scripts\\python(w)?\.exe$')
}

# Processes belonging to one install:
#   supervisor  - command line carries <root>\scripts\startup.ps1
#   router      - command line carries <root>\dsh-failover\proxy.py
#   echo        - from <root>\data\echo.pid (its own command line has no path)
#   echo-stub   - the venv launcher stub that spawned it (parent of the above)
# Entries also carry Role = 'stub' | 'real' so callers can count INSTANCES
# rather than processes.
function Get-EchoInstanceProcs([string]$root, $procs) {
    $r = $root.TrimEnd('\').ToLower()
    $hit = @()
    foreach ($p in $procs) {
        if (-not $p.CommandLine) { continue }
        $cl = $p.CommandLine.ToLower()
        $kind = ''
        if ($cl -like "*$r\scripts\startup.ps1*")       { $kind = 'supervisor' }
        elseif ($cl -like "*$r\dsh-failover\proxy.py*") { $kind = 'router' }
        if ($kind) {
            $role = if (Test-VenvLauncherStub $p) { 'stub' } else { 'real' }
            $hit += [pscustomobject]@{ PID = $p.ProcessId; Kind = $kind; Role = $role; CommandLine = $p.CommandLine }
        }
    }
    $echoPid = Get-EchoPidFileValue $root
    if ($echoPid -gt 0) {
        $real = $procs | Where-Object { $_.ProcessId -eq $echoPid }
        if ($real -and $real.CommandLine -and $real.CommandLine -match 'app\.main') {
            $hit += [pscustomobject]@{ PID = $echoPid; Kind = 'echo'; Role = 'real'; CommandLine = $real.CommandLine }
            $stub = $procs | Where-Object { $_.ProcessId -eq $real.ParentProcessId }
            if ($stub -and $stub.CommandLine -and $stub.CommandLine -match 'app\.main') {
                $hit += [pscustomobject]@{ PID = $stub.ProcessId; Kind = 'echo-stub'; Role = 'stub'; CommandLine = $stub.CommandLine }
            }
        }
    }
    return $hit
}

function Get-EchoSidebars([int]$port, $procs) {
    if ($port -le 0) { return @() }
    return @($procs | Where-Object {
        $_.Name -like 'echo-sidebar*' -and $_.CommandLine -and
        $_.CommandLine -match "--port\s+$port(\s|$)"
    } | ForEach-Object { [pscustomobject]@{ PID = $_.ProcessId; Kind = 'sidebar'; CommandLine = $_.CommandLine } })
}

# Is ECHO itself alive (pid file + really an ECHO process, guards pid reuse)?
function Test-EchoInstanceAlive([string]$root, $procs) {
    $pid_ = Get-EchoPidFileValue $root
    if ($pid_ -le 0) { return $false }
    $real = $procs | Where-Object { $_.ProcessId -eq $pid_ }
    if (-not $real) { return $false }
    if (-not $real.CommandLine) { return $false }
    return ($real.CommandLine -match 'app\.main')
}

function Test-EchoMeetingActive([int]$port) {
    if ($port -le 0) { return $false }
    try {
        $r = Invoke-RestMethod "http://127.0.0.1:$port/api/meeting/status" -TimeoutSec 4
        return [bool]$r.active
    } catch { return $false }
}

# --------------------------------------------------------------- actions
function Stop-EchoInstance {
    param(
        [string]$Root,
        [string]$Name,
        [switch]$SkipMeetingCheck,
        [switch]$DryRun,
        [scriptblock]$Log = { param($m) Write-Host $m }
    )
    $procs = Get-EchoProcSnapshot
    $port = Get-EchoPortFromFile $Root
    $targets = @(Get-EchoInstanceProcs $Root $procs) + @(Get-EchoSidebars $port $procs)
    if ($targets.Count -eq 0) { return $true }

    if (-not $SkipMeetingCheck -and (Test-EchoMeetingActive $port)) {
        & $Log "  REFUSED: $Name is recording a meeting (port $port). Stop it first or use -Force."
        return $false
    }
    foreach ($t in $targets) {
        & $Log ("  kill {0,-10} pid={1}" -f $t.Kind, $t.PID)
        if (-not $DryRun) { Stop-Process -Id $t.PID -Force -ErrorAction SilentlyContinue }
    }
    if ($DryRun) { return $true }
    # Wait for the port to be released. The SUPERVISOR must be gone first,
    # otherwise it restarts ECHO while we wait (it probes every loop).
    for ($i = 0; $i -lt 20; $i++) {
        Start-Sleep -Milliseconds 500
        if (-not (Test-EchoPortListening $port)) { break }
    }
    if (Test-EchoPortListening $port) {
        & $Log "  WARN: $Name port $port still listening"
        return $false
    }
    return $true
}

function Start-EchoInstance {
    param(
        [string]$Root,
        [string]$Name,
        [int]$WaitSeconds = 90,
        [switch]$DryRun,
        [scriptblock]$Log = { param($m) Write-Host $m }
    )
    if (-not (Test-Path $Root)) { & $Log "  ERROR: $Name root not found: $Root"; return 0 }
    $vbs = Join-Path $Root 'scripts\echo-startup.vbs'
    $sup = Join-Path $Root 'scripts\startup.ps1'
    if (Test-Path $vbs) {
        & $Log "  launch $Name supervisor (hidden) via echo-startup.vbs"
        if (-not $DryRun) { Start-Process wscript.exe -ArgumentList "`"$vbs`"" -WorkingDirectory $Root }
    } elseif (Test-Path $sup) {
        & $Log "  launch $Name supervisor via startup.ps1"
        if (-not $DryRun) {
            Start-Process powershell -ArgumentList @('-NoProfile', '-ExecutionPolicy', 'Bypass',
                '-File', "`"$sup`"") -WorkingDirectory $Root -WindowStyle Hidden
        }
    } else {
        & $Log "  ERROR: $Name has neither scripts\echo-startup.vbs nor scripts\startup.ps1"
        return 0
    }
    if ($DryRun) { return -1 }

    # main() writes data\echo-port.txt just before uvicorn binds, so poll it.
    $port = 0
    for ($i = 0; $i -lt $WaitSeconds; $i++) {
        Start-Sleep -Seconds 1
        $port = Get-EchoPortFromFile $Root
        if ($port -gt 0 -and (Test-EchoPortListening $port)) { break }
    }
    if ($port -le 0 -or -not (Test-EchoPortListening $port)) {
        & $Log "  ERROR: $Name not listening within ${WaitSeconds}s (see $Root\data\logs\echo-server.log.err)"
        return 0
    }
    return $port
}
