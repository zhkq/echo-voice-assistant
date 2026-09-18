# =====================================================================
# echo-supervisor.ps1 - ONE resident supervisor that schedules by a switch
#
# Replaces "one supervisor per install". Instead of every install fighting to
# keep its own ECHO alive, a single supervisor reads which instance SHOULD be
# running (the `current` field of %USERPROFILE%\.echo-instances.json, i.e. the
# switch) and:
#
#   1. keeps the ACTIVE instance up        (start if down, restart if crashed)
#   2. reports the INACTIVE one if it is running
#   3. optionally kills the inactive one   (autoStopOthers = true)
#
# Why one supervisor: with one per install, switching must not forget to stop
# the other supervisor, or it revives the instance you just stopped within
# ~15s - and you silently end up with two.
#
# Why autoStopOthers defaults to FALSE: a supervisor that kills whatever it did
# not start is a great way to make ECHO look like it "randomly dies". Turning it
# on is a deliberate choice for strict single-instance enforcement.
#
# Log: %USERPROFILE%\.echo-supervisor.log
#
# Usage
#   powershell -File scripts\echo-supervisor.ps1                 # resident
#   powershell -File scripts\echo-supervisor.ps1 -Once           # one pass, then exit
#   powershell -File scripts\echo-supervisor.ps1 -Once -DryRun   # decide only, do nothing
#
# Autostart: switch-instance.ps1 -InstallAutostart  (repoints the Startup
# shortcut here, so after a reboot the switch decides what comes up).
#
# ASCII-ONLY on purpose (see echo-instance-lib.ps1).
# =====================================================================
param(
    [string]$Config = '',
    [int]$IntervalSeconds = 5,
    [int]$WaitSeconds = 90,
    [switch]$Once,
    [switch]$DryRun,
    [switch]$Quiet
)

$ErrorActionPreference = 'Continue'
. (Join-Path $PSScriptRoot 'echo-instance-lib.ps1')

$script:CfgPath = if ($Config) { $Config } else { $script:ConfigPathDefault }
$script:LogPath = Join-Path $env:USERPROFILE '.echo-supervisor.log'
$script:WarnedOthers = @{}

function SupLog([string]$m, [string]$level = 'INFO') {
    $line = "[{0}] [{1}] {2}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $level, $m
    if (-not $Quiet) { Write-Host $line }
    try {
        if ((Test-Path $script:LogPath) -and (Get-Item $script:LogPath).Length -gt 2MB) {
            Move-Item $script:LogPath "$script:LogPath.1" -Force
        }
        Add-Content -Path $script:LogPath -Value $line -Encoding UTF8
    } catch { }
}

SupLog "===== echo-supervisor start (interval=${IntervalSeconds}s, dryRun=$DryRun, once=$Once) ====="

function Invoke-Pass {
    $cfg = Get-EchoInstanceConfig $script:CfgPath
    if (-not $cfg) {
        SupLog "no config at $($script:CfgPath) - run: switch-instance.ps1 -Init" 'ERROR'
        return $true
    }
    $desired = [string]$cfg.current
    $names = Get-EchoInstanceNames $cfg
    if ($names -notcontains $desired) {
        SupLog "config.current='$desired' is not a configured instance ($($names -join ', '))" 'ERROR'
        return $true
    }
    $root = Get-EchoInstanceRoot $cfg $desired
    if (-not (Test-Path $root)) {
        SupLog "active '$desired' root missing: $root" 'ERROR'
        return $false
    }

    $procs = Get-EchoProcSnapshot
    $port = Get-EchoPortFromFile $root
    $listening = Test-EchoPortListening $port

    # 1) keep the ACTIVE instance up
    if ($listening) {
        $ec = @($procs | Where-Object { $_.ProcessId -eq (Get-EchoPidFileValue $root) }).Count
        $rp = @(Get-EchoInstanceProcs $root $procs | Where-Object { $_.Kind -eq 'router' -and $_.Role -ne 'stub' }).Count
        $sp = @(Get-EchoInstanceProcs $root $procs | Where-Object { $_.Kind -eq 'supervisor' }).Count
        SupLog "active '$desired' healthy on port $port (echo=$ec router=$rp supervisor=$sp)"
    } elseif (Test-EchoInstanceAlive $root $procs) {
        SupLog "active '$desired' is booting (pid alive, port $port not ready yet)"
    } else {
        SupLog "active '$desired' is down - starting"
        $p = Start-EchoInstance -Root $root -Name $desired -WaitSeconds $WaitSeconds -DryRun:$DryRun `
                                -Log { param($m) SupLog $m }
        if ($p -gt 0) { SupLog "active '$desired' is up on port $p" 'OK' }
        elseif ($p -eq 0) { SupLog "active '$desired' failed to come up" 'ERROR' }
    }

    # 2/3) the other instances
    foreach ($n in $names) {
        if ($n -eq $desired) { continue }
        $oroot = Get-EchoInstanceRoot $cfg $n
        $opros = @(Get-EchoInstanceProcs $oroot $procs)
        if ($opros.Count -eq 0) { continue }
        $autoStop = $false
        if ($cfg.PSObject.Properties.Name -contains 'autoStopOthers') { $autoStop = [bool]$cfg.autoStopOthers }
        if ($autoStop) {
            SupLog "inactive '$n' is running ($($opros.Count) proc) - autoStopOthers=true, stopping it" 'WARN'
            Stop-EchoInstance -Root $oroot -Name $n -DryRun:$DryRun -Log { param($m) SupLog $m } | Out-Null
        } elseif (-not $script:WarnedOthers[$n]) {
            $script:WarnedOthers[$n] = $true
            SupLog "inactive '$n' is ALSO running ($($opros.Count) proc) - only one should run." 'WARN'
            SupLog "  stop it with: switch-instance.ps1 $desired    (or set autoStopOthers=true)" 'WARN'
        }
    }
    # Let the warning fire again if that instance later stops and comes back.
    foreach ($k in @($script:WarnedOthers.Keys)) {
        $kroot = Get-EchoInstanceRoot $cfg $k
        if (@(Get-EchoInstanceProcs $kroot $procs).Count -eq 0) { $script:WarnedOthers.Remove($k) }
    }
    return $true
}

while ($true) {
    $ok = Invoke-Pass
    if ($Once) { break }
    Start-Sleep -Seconds $IntervalSeconds
}
if (-not $ok) { exit 1 }
exit 0
