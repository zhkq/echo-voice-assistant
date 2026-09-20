# gh-push.ps1 - commit (optional) and push ECHO to GitHub through DNS pollution.
#
# WHY THIS EXISTS: the corporate network blackholes the bare github.com name, so a plain
# `git push` cannot even resolve. Fix: probe reachable GitHub front IPs and let git connect
# straight to them while keeping SNI/Host = github.com (http.curloptResolve).
#
# LESSON 2026-09-12 (this is why the script looks like this):
#   * An IP that answers /info/refs with 401 can still BLACKHOLE a large upload. The first
#     attempt pinned 140.82.113.3, hung for 17 minutes and sent ~0 bytes; git never timed
#     out on its own. The same push via 20.27.177.113 finished instantly (46 MiB, 286 MiB/s).
#     So now we
#       - probe every candidate, measure latency, and prefer the fastest,
#       - hand git several addresses in ONE resolve entry (curl fails over at connect time),
#       - set http.lowSpeedLimit / http.lowSpeedTime so a stalled transfer aborts (~90s),
#       - retry automatically with a fresh probe (rotating the IP order) instead of telling
#         the user to rerun the script,
#       - use http.version=HTTP/1.1 and a big http.postBuffer, the usual cures for hangs on
#         large pushes over flaky links/proxies.
#
# FALLBACK 2026-09-21: on this network `http.curloptResolve` often half-works - the probe
#   answers 401 and the real push still dies with "Failed to connect to github.com port 443".
#   So when a direct attempt fails, this script starts a local CONNECT proxy
#   (scripts\gh-proxy.py - same technique as the `github-dns-bypass-push` skill) that tunnels
#   raw TCP to a freshly probed IP while SNI/Host stay github.com, then pushes through it with
#   `-c http.proxy=http://127.0.0.1:<port>`. Ports are ephemeral and one proxy is started per
#   candidate IP, because a proxy pins its IP at startup and a dead IP then just returns 502.
#   Both paths finish with an ls-remote check that origin/<branch> == local HEAD: a zero exit
#   code has lied here before, so it is never the last word.
#
# ASCII-ONLY ON PURPOSE (same rule as start.ps1 / stop.ps1 / restart-echo.ps1): Windows
# PowerShell 5.1 reads a BOM-less .ps1 as ANSI/GBK, and non-ASCII text then breaks parsing.
# This file used to carry Chinese comments behind a UTF-8 BOM - one save without the BOM and
# it would fail to parse.
#
# USAGE
#   powershell -File scripts\gh-push.ps1                       # push what is already committed
#   powershell -File scripts\gh-push.ps1 -Message "feat: xxx"   # git add -A, commit, push
#   powershell -File scripts\gh-push.ps1 -Verify                # probe + ls-remote only
#   powershell -File scripts\gh-push.ps1 -Retries 5 -TopN 4     # more attempts / more IPs
param(
    [string]$Message = '',
    [switch]$Verify,
    [switch]$SkipCheck,
    [int]$Retries = 3,
    [int]$TopN = 3,
    [int]$LowSpeedSeconds = 90
)
$ErrorActionPreference = 'Stop'
$root = Split-Path $PSScriptRoot -Parent
Set-Location $root

$repoUrl = 'https://github.com/zhkq/echo-voice-assistant.git'

# GitHub front IPs (they rotate - always probe, never trust a remembered one).
# Ordered roughly by how good they historically are from CN networks, but the probe re-sorts.
$candidates = @(
    '20.27.177.113',
    '20.205.243.166',
    '20.205.243.168',
    '140.82.114.3',
    '140.82.112.3',
    '140.82.113.3',
    '140.82.121.3',
    '140.82.121.4',
    '140.82.122.4'
)

function Get-ProbedIPs {
    # Returns reachable IPs sorted by latency (fastest first).
    # 401 = receive-pack endpoint reachable and asking for credentials (normal for a private
    # repo); 200 is accepted too in case the repo ever becomes public.
    $alive = @()
    foreach ($ip in $candidates) {
        $out = & curl.exe -s -o NUL -w "%{http_code} %{time_total}" `
            --resolve "github.com:443:$ip" --connect-timeout 4 -m 8 `
            "$repoUrl/info/refs?service=git-receive-pack" 2>$null
        $parts = ("$out").Trim() -split '\s+'
        if ($parts.Count -ge 2 -and ($parts[0] -eq '401' -or $parts[0] -eq '200')) {
            $alive += [pscustomobject]@{ IP = $ip; Code = $parts[0]; Latency = [double]$parts[1] }
        }
    }
    return @($alive | Sort-Object Latency)
}

function Test-Reachable([string]$resolve) {
    & git -c http.sslBackend=schannel -c "http.curloptResolve=$resolve" ls-remote origin 2>&1 |
        Select-Object -First 2
}

function Invoke-Push([string]$resolve, [string]$branch) {
    $cfg = @(
        '-c', 'http.sslBackend=schannel',
        '-c', "http.curloptResolve=$resolve",
        '-c', 'http.postBuffer=524288000',
        '-c', 'http.version=HTTP/1.1',
        '-c', 'http.lowSpeedLimit=1000',
        '-c', "http.lowSpeedTime=$LowSpeedSeconds"
    )
    # Native git writes progress to stderr; with ErrorActionPreference=Stop PowerShell would
    # turn that into a terminating error and hide the real exit code.
    $prev = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        # git prints "branch 'x' set up to track ..." on stdout when -u is used. If that flows
        # into the pipeline it becomes part of this function's RETURN value, and the caller
        # then compares an array against 0 -> success reported as failure (bug found
        # 2026-09-12: a no-op push printed "Everything up-to-date" yet the script said failed).
        # So capture, echo to the host, and return ONLY the exit code.
        $lines = & git @cfg push -u origin $branch --progress 2>&1
        $code = $LASTEXITCODE
        foreach ($line in $lines) { Write-Host $line }
        return $code
    } finally {
        $ErrorActionPreference = $prev
    }
}

# ---- local CONNECT proxy fallback -------------------------------------------
# WHY: `http.curloptResolve` keeps SNI but needs git to connect STRAIGHT to the IP.
# On this network that path often half-works: the probe answers 401 and the real push
# still dies with "Failed to connect to github.com port 443" (hit repeatedly
# 2026-09-21). The proxy below tunnels raw TCP to a reachable front IP while SNI stays
# github.com, so TLS and the certificate are unchanged - only the TCP target moves.
function Get-PyExe {
    # Any Python works; the repo venv / runtime-core are the likely ones. ASCII only.
    $cands = @(
        (Join-Path $env:USERPROFILE '.echo-venv\Scripts\python.exe'),
        (Join-Path $root 'venv\Scripts\python.exe'),
        (Join-Path $root 'runtime-core\python.exe'),
        (Join-Path $root 'runtime-core\Scripts\python.exe')
    )
    foreach ($c in $cands) { if ($c -and (Test-Path $c)) { return $c } }
    foreach ($name in @('python', 'py')) {
        $cmd = Get-Command $name -ErrorAction SilentlyContinue
        if ($cmd) { return $cmd.Source }
    }
    return ''
}

function Get-FreePort {
    $listener = New-Object System.Net.Sockets.TcpListener([System.Net.IPAddress]::Loopback, 0)
    $listener.Start()
    $port = $listener.LocalEndpoint.Port
    $listener.Stop()
    return $port
}

function Start-GhProxy([string]$ip) {
    $py = Get-PyExe
    if (-not $py) { return $null }
    $proxyScript = Join-Path $PSScriptRoot 'gh-proxy.py'
    if (-not (Test-Path $proxyScript)) { return $null }
    $port = Get-FreePort
    $proc = Start-Process -FilePath $py -ArgumentList @($proxyScript, $port, $ip) `
        -PassThru -WindowStyle Hidden
    Start-Sleep -Milliseconds 900
    if ($proc.HasExited) { return $null }
    return @{ Proc = $proc; Port = $port }
}

function Invoke-PushViaProxy([int]$port, [string]$branch) {
    $cfg = @(
        '-c', "http.proxy=http://127.0.0.1:$port",
        '-c', 'http.sslBackend=schannel',
        '-c', 'http.postBuffer=524288000',
        '-c', 'http.version=HTTP/1.1',
        '-c', 'http.lowSpeedLimit=1000',
        '-c', "http.lowSpeedTime=$LowSpeedSeconds"
    )
    $prev = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $lines = & git @cfg push -u origin $branch --progress 2>&1
        $code = $LASTEXITCODE
        foreach ($line in $lines) { Write-Host $line }
        return $code
    } finally { $ErrorActionPreference = $prev }
}

function Stop-GhProxy($px) {
    # The process object we started is exact, but a stale listener on the port would make the
    # NEXT attempt fail oddly, so also clear whoever is listening there.
    try { Stop-Process -Id $px.Proc.Id -Force -ErrorAction SilentlyContinue } catch { }
    try {
        Get-NetTCPConnection -LocalPort $px.Port -State Listen -ErrorAction SilentlyContinue |
            ForEach-Object { Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue }
    } catch { }
}

function Get-RemoteHead([string]$branch, [string]$resolve = '', [int]$port = 0) {
    # Returns the remote SHA for $branch, or '' if the lookup itself failed.
    $cfg = @('-c', 'http.sslBackend=schannel')
    if ($port -gt 0) { $cfg += @('-c', "http.proxy=http://127.0.0.1:$port") }
    elseif ($resolve) { $cfg += @('-c', "http.curloptResolve=$resolve") }
    $prev = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $lines = & git @cfg ls-remote origin "refs/heads/$branch" 2>&1
        $code = $LASTEXITCODE
    } finally { $ErrorActionPreference = $prev }
    if ($code -ne 0) { return '' }
    foreach ($line in $lines) {
        if (("$line").Trim() -match '^([0-9a-f]{40})\s+refs/heads/') { return $Matches[1] }
    }
    return ''
}

function Confirm-Push([string]$branch, [string]$resolve = '', [int]$port = 0) {
    # A zero exit code means git accepted the push, but verify the remote actually moved -
    # "$LASTEXITCODE -eq 0 with nothing on the server" is exactly the class of bug this
    # script exists to avoid, so never report success on the exit code alone.
    $local = (git rev-parse HEAD).Trim()
    $remote = Get-RemoteHead $branch $resolve $port
    if (-not $remote) {
        Write-Host '    pushed (git exit 0), but ls-remote could not confirm the remote HEAD' -ForegroundColor Yellow
        return
    }
    if ($remote -eq $local) {
        Write-Host ("    verified: origin/{0} == {1}" -f $branch, $local.Substring(0, 8)) -ForegroundColor Green
    } else {
        Write-Host ("    WARNING: origin/{0} is {1} but local HEAD is {2}" -f `
            $branch, $remote.Substring(0, 8), $local.Substring(0, 8)) -ForegroundColor Red
    }
}

# ---- optional commit ----
if ($Message) {
    git add -A
    git commit -m $Message | Out-Null
    Write-Host "committed: $Message" -ForegroundColor Green
} elseif ((git status --short | Measure-Object -Line).Lines -gt 0) {
    Write-Host 'uncommitted changes present: pass -Message "..." to include them, or commit first.' -ForegroundColor Yellow
}

# ---- Windows smoke gate ----
# Same checks CI runs, but on this machine, right before anything leaves it. Skipped for
# -Verify (probe only) and for -SkipCheck (escape hatch: broken venv / offline work).
if (-not $SkipCheck -and -not $Verify) {
    $gate = Join-Path $PSScriptRoot 'check-windows.ps1'
    if (Test-Path $gate) {
        Write-Host '=== Windows gate: scripts\check-windows.ps1 ===' -ForegroundColor Cyan
        & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $gate
        if ($LASTEXITCODE -ne 0) {
            Write-Host ''
            Write-Host 'gate FAILED - nothing was pushed. Fix it, or re-run with -SkipCheck to override.' -ForegroundColor Red
            exit 1
        }
    } else {
        Write-Host 'check-windows.ps1 not found - gate skipped.' -ForegroundColor Yellow
    }
}

$branch = git symbolic-ref --short HEAD
$started = Get-Date

for ($attempt = 1; $attempt -le $Retries; $attempt++) {
    Write-Host "=== attempt $attempt/${Retries}: probing GitHub IPs ===" -ForegroundColor Cyan
    # [array] + @() are both required: with a SINGLE reachable IP, PowerShell unwraps the
    # one-element array into a scalar, so $probed.Count becomes $null, the rotation loop below
    # never runs, and the resolve string collapses to 'github.com:443:' (no address) -> git
    # fails with "Could not parse CURLOPT_RESOLVE entry". Seen 2026-09-13: three attempts in a row.
    [array]$probed = @(Get-ProbedIPs)
    if ($probed.Count -eq 0) {
        Write-Host 'no reachable IP: github.com is blocked right now (or the network is down). Retry later.' -ForegroundColor Red
        exit 1
    }
    $probed | ForEach-Object { Write-Host ("    {0,-16} {1}  {2:N2}s" -f $_.IP, $_.Code, $_.Latency) }

    # Rotate the sorted list per attempt so a retry does not start with the IP that just failed.
    $rotated = @()
    for ($i = 0; $i -lt $probed.Count; $i++) {
        $rotated += $probed[($i + $attempt - 1) % $probed.Count]
    }
    [array]$rotated = @($rotated)
    $pick = @($rotated | Select-Object -First ([Math]::Max(1, [Math]::Min($TopN, $rotated.Count))))
    $resolve = 'github.com:443:' + (($pick | ForEach-Object { $_.IP }) -join ',')
    Write-Host "    using: $resolve" -ForegroundColor DarkCyan
    if ($resolve -eq 'github.com:443:') {
        Write-Host 'internal error: no IP made it into the resolve string (probe/rotation bug)' -ForegroundColor Red
        exit 1
    }

    if ($Verify) {
        Write-Host '=== Verify only: ls-remote ===' -ForegroundColor Cyan
        Test-Reachable $resolve
        exit 0
    }

    Write-Host "=== pushing $branch -> origin (stall aborts after ${LowSpeedSeconds}s) ===" -ForegroundColor Cyan
    $code = Invoke-Push $resolve $branch
    if ($code -eq 0) {
        Confirm-Push $branch $resolve 0
        $secs = [int]((Get-Date) - $started).TotalSeconds
        Write-Host "push OK in ${secs}s" -ForegroundColor Green
        exit 0
    }
    Write-Host "direct attempt $attempt failed (git exit $code); trying the local CONNECT proxy" -ForegroundColor Yellow
    $proxyTried = $false
    foreach ($cand in $pick) {
        $px = Start-GhProxy $cand.IP
        if (-not $px) { Write-Host '    no usable python / gh-proxy.py - skipping the proxy path' -ForegroundColor DarkGray; break }
        $proxyTried = $true
        Write-Host ("    proxy 127.0.0.1:{0} -> {1}" -f $px.Port, $cand.IP)
        $code2 = Invoke-PushViaProxy $px.Port $branch
        if ($code2 -eq 0) {
            # Verify through the live proxy first, then tidy up.
            Confirm-Push $branch '' $px.Port
            Stop-GhProxy $px
            $secs = [int]((Get-Date) - $started).TotalSeconds
            Write-Host "push OK via proxy in ${secs}s" -ForegroundColor Green
            exit 0
        }
        Stop-GhProxy $px
        Write-Host ("    proxy attempt via {0} failed (git exit {1})" -f $cand.IP, $code2)
    }
    if ($proxyTried) { Write-Host 'attempt via proxy failed; will re-probe and retry' -ForegroundColor Yellow }
    else { Write-Host "attempt $attempt failed (git exit $code); will re-probe and retry" -ForegroundColor Yellow }
    Start-Sleep -Seconds 3
}

Write-Host "push failed after $Retries attempts. Network to GitHub may be closed right now; retry later." -ForegroundColor Red
exit 1
