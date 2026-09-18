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
# It also honours `bootDefault`: the autostart starts this script once per logon,
# so its FIRST pass is "just booted" - if bootDefault names an instance, the
# switch is snapped back to it there. That is how "always come up on the
# released install" is expressed, while switching to dev mid-session still
# works (later passes never write the switch).
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
$script:FirstPass = $true

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
    # --- bootDefault: FIRST pass only (that pass IS the logon launch) -------
    # Deliberately not applied on later passes: switching to dev mid-session has
    # to survive until the machine is rebooted.
    $boot = ''
    if ($cfg.PSObject.Properties.Name -contains 'bootDefault') { $boot = [string]$cfg.bootDefault }
    if ($script:FirstPass -and $boot) {
        $bootNames = Get-EchoInstanceNames $cfg
        if ($bootNames -notcontains $boot) {
            SupLog "config.bootDefault='$boot' is not a configured instance ($($bootNames -join ', ')) - ignored" 'WARN'
        } elseif ([string]$cfg.current -ne $boot) {
            if ($DryRun) {
                SupLog "[dry] bootDefault: would reset current '$([string]$cfg.current)' -> '$boot'"
            } else {
                $was = [string]$cfg.current
                $cfg | Add-Member -NotePropertyName current -NotePropertyValue $boot -Force
                Save-EchoInstanceConfig $script:CfgPath $cfg
                SupLog "bootDefault: reset current '$was' -> '$boot' (first pass after logon)" 'OK'
            }
        }
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
    $mine = @(Get-EchoInstanceProcs $root $procs | Where-Object { $_.Kind -eq 'echo' })
    $owned = ($mine.Count -gt 0)

    # 1) keep the ACTIVE instance up.
    # Decide by OWNERSHIP, not by the port alone: both installs use the same port
    # (18060) in this serial setup, so "something is listening" does NOT mean the
    # active instance is the thing listening.
    if ($owned -and $listening) {
        $rp = @(Get-EchoInstanceProcs $root $procs | Where-Object { $_.Kind -eq 'router' -and $_.Role -ne 'stub' }).Count
        $sp = @(Get-EchoInstanceProcs $root $procs | Where-Object { $_.Kind -eq 'supervisor' }).Count
        SupLog "active '$desired' healthy on port $port (echo=1 router=$rp supervisor=$sp)"
    } elseif ($owned) {
        SupLog "active '$desired' is booting (pid $($mine[0].PID), port $port not ready yet)"
    } elseif ($listening) {
        # The port is served by an instance that is NOT the active one. Starting
        # ours would only fight for the port, so just say so (rate-limited).
        if (-not $script:WarnedPort) {
            $script:WarnedPort = $true
            SupLog "port $port is served by another instance, but active is '$desired'." 'WARN'
            SupLog "  run: switch-instance.ps1 $desired   (or switch back)" 'WARN'
        }
    } else {
        $script:WarnedPort = $false
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
    $script:FirstPass = $false
    return $true
}

while ($true) {
    $ok = Invoke-Pass
    if ($Once) { break }
    Start-Sleep -Seconds $IntervalSeconds
}
if (-not $ok) { exit 1 }
exit 0
