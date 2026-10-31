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
#   "bootDefault": "stable",          <- optional. The autostart launches the
#                                        supervisor once per logon, and its FIRST
#                                        pass snaps `current` back to this name.
#                                        "" (or missing) = off, i.e. whatever you
#                                        switched to last survives the reboot.
#                                        Later passes never touch the switch, so
#                                        a mid-session switch to dev still works.
#   "autoStopOthers": false,          <- true = supervisor kills the other one
#   "instances": {
#     "stable": { "root": "C:\\echo1.0" },
#     "dev":    { "root": "C:\\...\\ECHO-public" }
#   }
# }
function New-EchoInstanceTemplate([string]$repoRoot) {
    $tpl = [ordered]@{
        current         = 'dev'
        bootDefault     = ''
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

# --------------------------------------------------------------- logging
# Rotate once the log passes `MaxBytes`, keeping exactly ONE backup
# (`<path>.1`, always overwritten). The supervisor calls this BEFORE every
# append: a log at 1.36 MB with one line every 6 s is how 2026-10-19 happened,
# so the bound must hold even for a single huge line.
function Invoke-EchoLogRotation {
    param(
        [string]$Path,
        [long]$MaxBytes = 2097152,
        [switch]$DryRun
    )
    if (-not $Path) { return $false }
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return $false }
    $len = [long](Get-Item -LiteralPath $Path).Length
    if ($len -le $MaxBytes) { return $false }
    if ($DryRun) { return $true }
    $bak = "$Path.1"
    if (Test-Path -LiteralPath $bak) {
        Remove-Item -LiteralPath $bak -Force -ErrorAction SilentlyContinue
    }
    Move-Item -LiteralPath $Path -Destination $bak -Force
    return $true
}

# Heartbeat de-duplication. The resident supervisor used to write
# "active 'x' healthy ..." every 6 s forever, so the log only ever grew.
# Kind = 'change'  -> the state really changed: write the real line
#        'summary' -> same state for N minutes: write ONE still-healthy line
#        'none'    -> stay quiet
# `State` is an opaque hashtable owned by the caller; pass $null the first time
# and store $decision.State afterwards.
function Get-EchoHeartbeatDecision {
    param(
        $State,
        [string]$Key,
        [datetime]$Now = (Get-Date),
        [int]$SummaryMinutes = 10
    )
    if ($null -eq $State -or -not ($State -is [hashtable]) -or $State['key'] -ne $Key) {
        $fresh = @{ key = $Key; since = $Now; last = $Now; lastSummary = $Now }
        return [pscustomobject]@{ State = $fresh; Kind = 'change'; Minutes = 0 }
    }
    $State['last'] = $Now
    if ($SummaryMinutes -gt 0 -and
        ($Now - $State['lastSummary']).TotalMinutes -ge $SummaryMinutes) {
        $State['lastSummary'] = $Now
        $mins = [int][Math]::Floor(($Now - $State['since']).TotalMinutes)
        return [pscustomobject]@{ State = $State; Kind = 'summary'; Minutes = $mins }
    }
    return [pscustomobject]@{ State = $State; Kind = 'none'; Minutes = 0 }
}

# ------------------------------------------------- logon autostart shortcut
# ONE source of truth for "what must the Startup shortcut point at".
# install-switcher.ps1 creates it; echo-supervisor.ps1 re-checks/repairs it.
# Shared on purpose: the 2026-10-19 incident was a MISSING "ECHO startup.lnk"
# (the logon never started ECHO). A second hardcoded copy would eventually
# disagree with the first, and the supervisor would then "repair" the shortcut
# onto the wrong target - so both callers MUST go through this spec.
function Get-EchoStartupShortcutSpec {
    param(
        [string]$StartupDir = '',
        [string]$SwitchDir = ''
    )
    if (-not $StartupDir) { $StartupDir = [Environment]::GetFolderPath('Startup') }
    # GetFolderPath('Startup') returns "" when the process runs with a redirected
    # USERPROFILE (observed 2026-10-19 in a test harness). The roaming path is
    # the very same folder, so fall back to it instead of building empty paths.
    if (-not $StartupDir -and $env:APPDATA) {
        $StartupDir = Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs\Startup'
    }
    if (-not $SwitchDir)  { $SwitchDir  = Join-Path $env:USERPROFILE '.echo-switch' }
    $vbs = Join-Path $SwitchDir 'echo-switch-startup.vbs'
    $link = ''
    if ($StartupDir) { $link = Join-Path $StartupDir 'ECHO startup.lnk' }
    return [pscustomobject]@{
        LinkPath         = $link
        TargetPath       = (Join-Path $env:SystemRoot 'System32\wscript.exe')
        Arguments        = ('"' + $vbs + '"')
        VbsPath          = $vbs
        WorkingDirectory = $SwitchDir
        Description      = 'ECHO autostart (switch supervisor, outside the repo)'
    }
}

# Compare paths the way Windows would, not byte-for-byte: the link may store
# "C:\Windows\System32\wscript.exe" while we hold the short name, or the quotes
# may differ. Anything that still launches the same program+script is CORRECT
# and must not be rewritten.
function ConvertTo-EchoPathKey([string]$path) {
    if (-not $path) { return '' }
    $p = $path.Trim()
    if ($p.Length -ge 2 -and $p.StartsWith('"') -and $p.EndsWith('"')) {
        $p = $p.Substring(1, $p.Length - 2)
    }
    try { $p = [System.IO.Path]::GetFullPath($p) } catch { }
    return $p.TrimEnd('\').ToLowerInvariant()
}

function Get-EchoShortcutArgumentPath([string]$arguments) {
    if (-not $arguments) { return '' }
    $t = $arguments.Trim()
    if ($t.StartsWith('"')) {
        $end = $t.IndexOf('"', 1)
        if ($end -gt 1) { return $t.Substring(1, $end - 1) }
    }
    return $t
}

# Thin COM seam: these two functions are the ONLY places that touch a real
# .lnk. Everything above them is pure decision logic, so it can be exercised
# without writing a real shortcut. That matters on this dev box: the endpoint
# protection agent kills any process that creates a standalone .lnk outside the
# Start Menu (tested 2026-10-19 - PowerShell just disappears, and the file is
# removed a second later), so tests override these two names with a text-file
# stand-in. Production keeps the real implementation below.
function Read-EchoShortcutLink {
    param([string]$Path)
    $ws = New-Object -ComObject WScript.Shell
    $lnk = $ws.CreateShortcut($Path)
    return [pscustomobject]@{
        TargetPath = [string]$lnk.TargetPath
        Arguments  = [string]$lnk.Arguments
    }
}

function Write-EchoShortcutLink {
    param($Spec)
    $ws = New-Object -ComObject WScript.Shell
    $lnk = $ws.CreateShortcut($Spec.LinkPath)
    $lnk.TargetPath = $Spec.TargetPath
    $lnk.Arguments = $Spec.Arguments
    $lnk.WorkingDirectory = $Spec.WorkingDirectory
    $lnk.Description = $Spec.Description
    $lnk.Save()
}

# Read-only verdict: Needed=$false means "leave the .lnk alone".
function Get-EchoStartupShortcutState {
    param(
        [string]$StartupDir = '',
        [string]$SwitchDir = ''
    )
    $spec = Get-EchoStartupShortcutSpec -StartupDir $StartupDir -SwitchDir $SwitchDir
    $vbsExists = $false
    if ($spec.VbsPath) { $vbsExists = (Test-Path -LiteralPath $spec.VbsPath -PathType Leaf) }
    $state = [pscustomobject]@{
        Spec      = $spec
        Needed    = $false
        Reason    = ''
        Detail    = ''
        VbsExists = $vbsExists
    }
    if (-not $spec.LinkPath) {
        # Cannot even locate the Startup folder: report it, but never pretend
        # to "repair" an address we do not have.
        $state.Reason = 'no-startup-folder'
        return $state
    }
    if (-not (Test-Path -LiteralPath $spec.LinkPath -PathType Leaf)) {
        $state.Needed = $true
        $state.Reason = 'missing'
        return $state
    }
    try {
        $actual = Read-EchoShortcutLink -Path $spec.LinkPath
        $target = [string]$actual.TargetPath
        $arguments = [string]$actual.Arguments
    } catch {
        $state.Needed = $true
        $state.Reason = 'unreadable'
        $state.Detail = "$_"
        return $state
    }
    $state.Detail = "target=$target args=$arguments"
    if ((ConvertTo-EchoPathKey $target) -ne (ConvertTo-EchoPathKey $spec.TargetPath)) {
        $state.Needed = $true
        $state.Reason = 'wrong-target'
        return $state
    }
    $argPath = Get-EchoShortcutArgumentPath $arguments
    if ((ConvertTo-EchoPathKey $argPath) -ne (ConvertTo-EchoPathKey $spec.VbsPath)) {
        $state.Needed = $true
        $state.Reason = 'wrong-arguments'
        return $state
    }
    return $state
}

# Write the shortcut ONLY when the verdict says it is missing/wrong. Returns
# Changed=$false when it was already correct (idempotent - a correct link is
# never touched, so its target semantics stay whatever install-switcher meant).
function Restore-EchoStartupShortcut {
    param(
        [string]$StartupDir = '',
        [string]$SwitchDir = '',
        [switch]$DryRun
    )
    $state = Get-EchoStartupShortcutState -StartupDir $StartupDir -SwitchDir $SwitchDir
    $result = [pscustomobject]@{
        Changed     = $false
        WouldChange = $false
        Reason      = $state.Reason
        LinkPath    = $state.Spec.LinkPath
        VbsExists   = $state.VbsExists
    }
    if (-not $state.Needed) { return $result }
    if ($DryRun) { $result.WouldChange = $true; return $result }
    $parent = Split-Path -Parent $state.Spec.LinkPath
    if ($parent -and -not (Test-Path -LiteralPath $parent -PathType Container)) {
        New-Item -ItemType Directory -Force -Path $parent | Out-Null
    }
    try {
        Write-EchoShortcutLink -Spec $state.Spec
    } catch {
        $result.Reason = "save-failed: $_"
        return $result
    }
    $after = Get-EchoStartupShortcutState -StartupDir $StartupDir -SwitchDir $SwitchDir
    if ($after.Needed) {
        $result.Reason = "save-ineffective: $($after.Reason)"
        return $result
    }
    $result.Changed = $true
    return $result
}

# --------------------------------------------------------------- probes
# 3.0 install-base layout: an instance 'root' is the CODE dir (the command line carries
# -File <root>\scripts\startup.ps1, which is what we match on), while data\echo.pid and
# data\echo-port.txt live in the INSTALL BASE - the parent folder when the code dir is
# named echo-core. Same rule as app\paths.py:echo_base(); on a flat tree both are equal.
function Get-EchoDataRoot([string]$root) {
    if ((Split-Path $root -Leaf) -ieq 'echo-core') { return (Split-Path $root -Parent) }
    return $root
}

function Get-EchoPidFileValue([string]$root) {
    $f = Join-Path (Get-EchoDataRoot $root) 'data\echo.pid'
    if (-not (Test-Path $f)) { return 0 }
    $v = Get-Content $f -Raw -ErrorAction SilentlyContinue
    if ($null -eq $v) { return 0 }
    $v = $v.Trim()
    if ($v -notmatch '^[0-9]+$') { return 0 }
    return [int]$v
}

function Get-EchoPortFromFile([string]$root) {
    $f = Join-Path (Get-EchoDataRoot $root) 'data\echo-port.txt'
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
    # 3.0 layout: the code root may be `<install>\echo-core`, while the router /
    # harness / data live one level UP in the DATA root (`<install>\dsh\...`).
    # Matching only the code root missed BOTH of those for the stable tree -
    # switching to dev then left D:\ECHO's harness on 43199 (dev boot correctly
    # refused it, but the old process stayed and nobody could use dev's own dsh).
    $dr = (Get-EchoDataRoot $root).TrimEnd('\').ToLower()
    $hit = @()
    foreach ($p in $procs) {
        if (-not $p.CommandLine) { continue }
        $cl = $p.CommandLine.ToLower()
        $nm = if ($p.Name) { $p.Name.ToLower() } else { '' }
        $inCode = ($cl -like "*$r\*")
        $inData = ($dr -ne $r) -and ($cl -like "*$dr\*")
        $kind = ''
        # Match on process TYPE as well as path: a diagnostic shell whose command
        # TEXT merely mentions these paths must not be counted - and must not be
        # KILLED (a diagnostic script that also grepped for startup.ps1 killed
        # itself here on 2026-09-18). Real watchdogs are started with -File, and
        # `-Command` is how an interactive/diagnostic shell arrives.
        if ($nm -match '^python' -and
            (($cl -like "*$r\dsh-failover\proxy.py*") -or
             (($dr -ne $r) -and ($cl -like "*$dr\dsh-failover\proxy.py*"))) -and
            $p.ProcessId -ne $PID) {
            $kind = 'router'
        } elseif ($nm -match '^node' -and ($inCode -or $inData) -and
                  $cl -like '*@deepseek-ai\dsh\lib\bin.js*') {
            # 2026-10-10: the STANDALONE HARNESS (node) must follow its tree like every
            # other process here. Both trees use port 43199, and ECHO's
            # ensure_running() used to reuse whoever answered on that port - so after a
            # switch the new tree kept talking to the OLD tree's harness, whose
            # DSH_HOME has different workspace ids: `session/create` ->
            # `workspace/not-found` -> the panel showed a 500 on "start daily review"
            # (user hit it twice).
            # Matches both layouts: <root>\dsh\app\node_modules\... (new) and
            # <root>\harness\dsh\node_modules\... (old).
            $kind = 'harness'
        } elseif ($nm -match '^(powershell|pwsh)' -and $cl -notlike '*-command*' -and
                  $cl -like '*-file*' -and $cl -like "*$r\scripts\startup.ps1*" -and
                  $p.ProcessId -ne $PID) {
            $kind = 'supervisor'
        }
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

# The sidebar is launched as:
#   <root>\sidebar\bin\Release\...\echo-sidebar.exe --url=http://127.0.0.1:<port>/ --width=450
# NOTE it takes --url, NOT --port - an earlier matcher looked for "--port <n>" and
# therefore never matched, which left a stray sidebar holding the old tree's exe
# open and blocking a directory move. Match by EXECUTABLE PATH so a sidebar that
# belongs to another tree is never touched.
function Get-EchoSidebars([string]$root, $procs) {
    $r = $root.TrimEnd('\').ToLower()
    if (-not $r) { return @() }
    return @($procs | Where-Object {
        $_.Name -like 'echo-sidebar*' -and $_.CommandLine -and
        $_.CommandLine.ToLower() -like "*$r\sidebar\*"
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
    $targets = @(Get-EchoInstanceProcs $Root $procs) + @(Get-EchoSidebars $Root $procs)
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
    # Skip launching if this install's own supervisor is ALREADY running. Both the
    # root supervisor and switch-instance.ps1 call this function, and during a
    # switch there is a window in which both see the instance as down - spawning
    # twice left two watchdog loops racing (2026-09-18 observed).
    $existing = @(Get-EchoInstanceProcs $Root (Get-EchoProcSnapshot) |
                  Where-Object { $_.Kind -eq 'supervisor' })
    if ($existing.Count -gt 0) {
        & $Log "  $Name supervisor already running (pid $($existing[0].PID)) - not launching another"
    } elseif (Test-Path $vbs) {
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
