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
# FALLBACK 2026-09-21 (+ reworked 2026-10-18): on this network `http.curloptResolve` often
#   half-works - the probe answers 401 and the real push still dies with "Failed to connect
#   to github.com port 443" / "CONNECT tunnel failed, response 502". So when a direct attempt
#   fails, this script re-probes a live IP right then and starts the local CONNECT proxy from
#   the `github-dns-bypass-push` skill (`node gh-proxy.js`). The proxy tunnels raw TCP to that
#   IP while SNI/Host stay github.com, then the push goes through it with
#   `-c http.proxy=http://127.0.0.1:<port>`.
#   WHY the node script from the skill: that is the copy that actually works on this network
#   (the old Python copy had drifted and pinned its IP at startup). WHY a NEW port every time:
#   a stale proxy may still own 8899 and keep a dead IP cached, so reusing 8899 turns every
#   retry into the same 502. Ports are allocated fresh per attempt and every proxy is stopped
#   in a `finally` - success or failure - because an orphan pins a dead IP and poisons the
#   next attempt on a brand-new port.
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
#   powershell -File scripts\gh-push.ps1 -QuickGate             # gate minus the full test suite
#   powershell -File scripts\gh-push.ps1 -Retries 5 -TopN 4     # more attempts / more IPs
param(
    [string]$Message = '',
    [switch]$Verify,
    [switch]$SkipCheck,
    # Quick gate: forward -Quick to check-windows.ps1, i.e. compile + import smoke + platform
    # contract + ruff, but SKIP the 927-case suite (~9 min -> ~3 s).
    # WHEN: docs/comment-only commits, or when you ALREADY ran the full suite yourself.
    # WHY NOT DEFAULT: it cannot catch behaviour regressions - that is what the suite is for.
    # Prefer this over -SkipCheck: same time saved, still gets syntax/import/lint coverage.
    [switch]$QuickGate,
    [int]$Retries = 3,
    [int]$TopN = 3,
    [int]$LowSpeedSeconds = 90
)
$ErrorActionPreference = 'Stop'
$root = Split-Path $PSScriptRoot -Parent
Set-Location $root

# Set when the pre-push hook rejects the push (identity allowlist, see .githooks/pre-push).
# That verdict is client-side and deterministic, so IP rotation and retries cannot change it.
$script:HookBlocked = $false

$repoUrl = 'https://github.com/zhkq/echo-voice-assistant.git'

# Command indirection. Defaults are the real tools; the ECHO_GH_PUSH_* variables exist so
# tests can substitute stubs and so a machine with git/curl/node elsewhere can point at it.
# WHY this matters for tests: no test may ever run a real `git push` against this repo.
$script:GitExe = if ($env:ECHO_GH_PUSH_GIT) { $env:ECHO_GH_PUSH_GIT } else { 'git' }
$script:CurlExe = if ($env:ECHO_GH_PUSH_CURL) { $env:ECHO_GH_PUSH_CURL } else { 'curl.exe' }
$script:LastPushText = ''
# Ports already handed to a proxy in this run. A fresh port is not enough on its own: the
# OS may hand back a just-released one, and that is exactly the "stale proxy on a reused
# port" trap this fallback must avoid.
$script:UsedProxyPorts = @()

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
    # ONLY 401 counts. WHY: an IP that answers a plain `GET /` or even 200 can still 502
    # when the proxy opens a CONNECT tunnel to the receive-pack endpoint - the endpoint is
    # the only thing git actually talks to. `--ssl-no-revoke` is required because this
    # machine's schannel curl answers 000 when it cannot reach the revocation list.
    # `--resolve` needs COLONS (host:443:ip); an `=` here yields a parse error whose
    # http_code is 000, i.e. it looks exactly like "network down" (seen 2026-09-24).
    $alive = @()
    foreach ($ip in $candidates) {
        $out = & $script:CurlExe -s -o NUL -w "%{http_code} %{time_total}" `
            --ssl-no-revoke --resolve "github.com:443:$ip" --connect-timeout 4 -m 8 `
            "$repoUrl/info/refs?service=git-receive-pack" 2>$null
        $parts = ("$out").Trim() -split '\s+'
        if ($parts.Count -ge 2 -and $parts[0] -eq '401') {
            $alive += [pscustomobject]@{ IP = $ip; Code = $parts[0]; Latency = [double]$parts[1] }
        }
    }
    return @($alive | Sort-Object Latency)
}

function Test-Reachable([string]$resolve) {
    & $script:GitExe -c http.sslBackend=schannel -c "http.curloptResolve=$resolve" ls-remote origin 2>&1 |
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
        $lines = & $script:GitExe @cfg push -u origin $branch --progress 2>&1
        $code = $LASTEXITCODE
        $script:LastPushText = ($lines -join "`n")
        foreach ($line in $lines) { Write-Host $line }
        if (("$lines") -match 'push blocked') { $script:HookBlocked = $true }
        return $code
    } finally {
        $ErrorActionPreference = $prev
    }
}

function Get-PushReason([string]$text) {
    # Pull the one line that actually says why out of git's wall of output. WHY: the old
    # script left a `fatal: ...` buried mid-stream and the user only saw "push failed".
    if (-not $text) { return '(git produced no output)' }
    $important = @()
    foreach ($line in ($text -split "`r?`n")) {
        $t = $line.Trim()
        if (-not $t) { continue }
        if ($t -match '(?i)fatal|error|failed|timed out|timeout|CONNECT tunnel|502|could not resolve|refused|denied') {
            $important += $t
        }
    }
    if ($important.Count -gt 0) { return $important[-1] }
    $all = @(($text -split "`r?`n") | Where-Object { $_.Trim() })
    if ($all.Count -gt 0) { return $all[-1].Trim() }
    return '(git produced no output)'
}

# ---- local CONNECT proxy fallback (node gh-proxy.js from the skill) ----------
# WHY: `http.curloptResolve` keeps SNI but needs git to connect STRAIGHT to the IP.
# On this network that path often half-works: the probe answers 401 and the real push
# still dies with "Failed to connect to github.com port 443" / "CONNECT tunnel failed,
# response 502" (hit repeatedly 2026-09-21 and again 2026-10-18). The proxy below tunnels
# raw TCP to a reachable front IP while SNI stays github.com, so TLS and the certificate
# are unchanged - only the TCP target moves.
#
# WHY the node script in the `github-dns-bypass-push` skill: that is the copy that has
# actually worked on this network, and it reads PORT/GH_IP from the environment, so every
# attempt can get a brand-new port. NEVER hard-code 8899: a stale proxy may still own it
# and keep its dead IP cached, which is why retrying on 8899 keeps returning 502.
# Every Start-GhProxy is paired with a Stop-GhProxy in a `finally`; an orphaned proxy
# pins a dead IP and poisons the next attempt even though it uses a new port.
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

function Get-NodeExe {
    # node: PATH first, then the managed copy used by the skill. The version directory
    # rotates (22.22.2 -> 22.22.2-3), so read `versions\current` instead of remembering it.
    if ($env:ECHO_GH_PUSH_NODE) {
        if (Test-Path -LiteralPath $env:ECHO_GH_PUSH_NODE) { return $env:ECHO_GH_PUSH_NODE }
        return ''
    }
    $cmd = Get-Command node -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    # Known install locations (the skill's managed copy plus the hermes one it names).
    $known = @(
        (Join-Path $env:LOCALAPPDATA 'hermes\node\node.exe'),
        (Join-Path $env:ProgramFiles 'nodejs\node.exe')
    )
    foreach ($k in $known) {
        if ($k -and (Test-Path -LiteralPath $k)) { return $k }
    }
    $vdir = Join-Path $env:USERPROFILE '.workbuddy\binaries\node\versions'
    $current = Join-Path $vdir 'current'
    if (Test-Path -LiteralPath $current) {
        $ver = (Get-Content -LiteralPath $current -TotalCount 1).Trim()
        if ($ver) {
            $exe = Join-Path (Join-Path $vdir $ver) 'node.exe'
            if (Test-Path -LiteralPath $exe) { return $exe }
        }
    }
    return ''
}

function Get-GhProxyScript {
    # The proxy lives in the skill, not in this repo: the skill is where it is maintained.
    if ($env:ECHO_GH_PUSH_PROXY_JS) {
        if (Test-Path -LiteralPath $env:ECHO_GH_PUSH_PROXY_JS) { return $env:ECHO_GH_PUSH_PROXY_JS }
        return ''
    }
    $cands = @(
        (Join-Path $env:USERPROFILE '.dsh\skills\github-dns-bypass-push\scripts\gh-proxy.js'),
        (Join-Path $root '.dsh\skills\github-dns-bypass-push\scripts\gh-proxy.js')
    )
    foreach ($c in $cands) {
        if (Test-Path -LiteralPath $c) { return $c }
    }
    return ''
}

function Get-NewProxyPort {
    # Fresh and never reused: see the comment at the top of this section.
    for ($i = 0; $i -lt 50; $i++) {
        $port = Get-FreePort
        if ($port -le 0 -or $port -eq 8899) { continue }
        if ($script:UsedProxyPorts -contains $port) { continue }
        $script:UsedProxyPorts += $port
        return $port
    }
    return 0
}

function Wait-ProxyReady([int]$port, $proc, [int]$timeoutMs = 4000) {
    $deadline = (Get-Date).AddMilliseconds($timeoutMs)
    while ((Get-Date) -lt $deadline) {
        if ($proc.HasExited) { return $false }
        try {
            $client = New-Object System.Net.Sockets.TcpClient
            $client.Connect('127.0.0.1', $port)
            $client.Close()
            return $true
        } catch {
            Start-Sleep -Milliseconds 120
        }
    }
    return $false
}

function Get-ProxyLogTail([string]$path) {
    if (-not $path -or -not (Test-Path -LiteralPath $path)) { return '' }
    try {
        $lines = @(Get-Content -LiteralPath $path -ErrorAction SilentlyContinue |
            Where-Object { $_.Trim() })
        if ($lines.Count -gt 0) { return $lines[-1].Trim() }
    } catch { }
    return ''
}

function Start-GhProxy([string]$ip) {
    # Returns @{ Ok = $true; Proc; Port; OutLog; ErrLog } or @{ Ok = $false; Reason }.
    # Returning the reason (instead of $null) is deliberate: "no node" must be SAID, not
    # silently skipped - the caller would otherwise retry direct forever with no clue.
    $node = Get-NodeExe
    if (-not $node) {
        return @{ Ok = $false; Reason = 'node was not found - the CONNECT proxy fallback needs node (install node or set ECHO_GH_PUSH_NODE)' }
    }
    $proxyScript = Get-GhProxyScript
    if (-not $proxyScript) {
        return @{ Ok = $false; Reason = 'gh-proxy.js was not found - expected it at <USERPROFILE>\.dsh\skills\github-dns-bypass-push\scripts\gh-proxy.js' }
    }
    $port = Get-NewProxyPort
    if ($port -le 0) {
        return @{ Ok = $false; Reason = 'could not allocate a fresh loopback port for the proxy' }
    }
    $outLog = Join-Path $env:TEMP ("echo-gh-proxy-{0}.out.log" -f $port)
    $errLog = Join-Path $env:TEMP ("echo-gh-proxy-{0}.err.log" -f $port)
    foreach ($f in @($outLog, $errLog)) {
        try { if (Test-Path -LiteralPath $f) { Remove-Item -LiteralPath $f -Force } } catch { }
    }
    # gh-proxy.js reads PORT/GH_IP from the environment. Set them only for the child, then
    # restore: leaking GH_IP into this shell would be invisible state for the next attempt.
    $oldPort = $env:PORT
    $oldIp = $env:GH_IP
    $proc = $null
    try {
        $env:PORT = "$port"
        $env:GH_IP = $ip
        $proc = Start-Process -FilePath $node -ArgumentList @($proxyScript) -PassThru `
            -WindowStyle Hidden -RedirectStandardOutput $outLog -RedirectStandardError $errLog
    } catch {
        return @{ Ok = $false; Reason = ("could not start node: " + $_.Exception.Message) }
    } finally {
        if ($null -eq $oldPort) { Remove-Item Env:PORT -ErrorAction SilentlyContinue }
        else { $env:PORT = $oldPort }
        if ($null -eq $oldIp) { Remove-Item Env:GH_IP -ErrorAction SilentlyContinue }
        else { $env:GH_IP = $oldIp }
    }
    if (-not (Wait-ProxyReady $port $proc 4000)) {
        $why = Get-ProxyLogTail $errLog
        if (-not $why) { $why = Get-ProxyLogTail $outLog }
        if (-not $why) { $why = 'the proxy never started listening on 127.0.0.1:' + $port }
        try { Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue } catch { }
        try { Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue |
            ForEach-Object { Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue } } catch { }
        foreach ($f in @($outLog, $errLog)) {
            try { if (Test-Path -LiteralPath $f) { Remove-Item -LiteralPath $f -Force } } catch { }
        }
        return @{ Ok = $false; Reason = $why }
    }
    return @{ Ok = $true; Proc = $proc; Port = $port; OutLog = $outLog; ErrLog = $errLog }
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
        $lines = & $script:GitExe @cfg push -u origin $branch --progress 2>&1
        $code = $LASTEXITCODE
        $script:LastPushText = ($lines -join "`n")
        foreach ($line in $lines) { Write-Host $line }
        if (("$lines") -match 'push blocked') { $script:HookBlocked = $true }
        return $code
    } finally { $ErrorActionPreference = $prev }
}

function Stop-GhProxy($px) {
    # Both success and failure come through here (the caller has a `finally`). The process
    # we started is exact, but also clear whoever listens on that port: a leftover listener
    # would make the NEXT attempt fail oddly even on a different port.
    try {
        if ($px.Proc -and -not $px.Proc.HasExited) {
            Stop-Process -Id $px.Proc.Id -Force -ErrorAction SilentlyContinue
        }
    } catch { }
    try {
        Get-NetTCPConnection -LocalPort $px.Port -State Listen -ErrorAction SilentlyContinue |
            ForEach-Object { Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue }
    } catch { }
    foreach ($f in @($px.OutLog, $px.ErrLog)) {
        try { if ($f -and (Test-Path -LiteralPath $f)) { Remove-Item -LiteralPath $f -Force } } catch { }
    }
}

function Get-RemoteHead([string]$branch, [string]$resolve = '', [int]$port = 0) {
    # Returns the remote SHA for $branch, or '' if the lookup itself failed.
    $cfg = @('-c', 'http.sslBackend=schannel')
    if ($port -gt 0) { $cfg += @('-c', "http.proxy=http://127.0.0.1:$port") }
    elseif ($resolve) { $cfg += @('-c', "http.curloptResolve=$resolve") }
    $prev = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $lines = & $script:GitExe @cfg ls-remote origin "refs/heads/$branch" 2>&1
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
    # Returns $true ONLY when origin/<branch> == local HEAD; an unconfirmed lookup is a
    # failure here, because "pushed but could not verify" has already lied once.
    $local = ("$(& $script:GitExe rev-parse HEAD)").Trim()
    if (-not $local) {
        Write-Host '    could not read local HEAD - cannot verify the push' -ForegroundColor Red
        return $false
    }
    $remote = Get-RemoteHead $branch $resolve $port
    if (-not $remote) {
        Write-Host '    pushed (git exit 0), but ls-remote could not confirm the remote HEAD' -ForegroundColor Yellow
        return $false
    }
    if ($remote -eq $local) {
        Write-Host ("    verified: origin/{0} == {1}" -f $branch, $local.Substring(0, 8)) -ForegroundColor Green
        return $true
    }
    Write-Host ("    WARNING: origin/{0} is {1} but local HEAD is {2}" -f `
        $branch, $remote.Substring(0, 8), $local.Substring(0, 8)) -ForegroundColor Red
    return $false
}

# ---- optional commit ----
if ($Message) {
    & $script:GitExe add -A
    & $script:GitExe commit -m $Message | Out-Null
    Write-Host "committed: $Message" -ForegroundColor Green
} elseif ((& $script:GitExe status --short | Measure-Object -Line).Lines -gt 0) {
    Write-Host 'uncommitted changes present: pass -Message "..." to include them, or commit first.' -ForegroundColor Yellow
}

# ---- Windows smoke gate ----
# Same checks CI runs, but on this machine, right before anything leaves it. Skipped for
# -Verify (probe only) and for -SkipCheck (escape hatch: broken venv / offline work).
# -QuickGate forwards -Quick to the gate: everything except the 927-case suite (~9 min -> ~3 s).
if (-not $SkipCheck -and -not $Verify) {
    $gate = Join-Path $PSScriptRoot 'check-windows.ps1'
    if (Test-Path $gate) {
        $gateArgs = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $gate)
        if ($QuickGate) {
            Write-Host '=== Windows gate: QUICK (full test suite skipped) ===' -ForegroundColor Cyan
            $gateArgs += '-Quick'
        } else {
            Write-Host '=== Windows gate: scripts\check-windows.ps1 ===' -ForegroundColor Cyan
        }
        & powershell.exe @gateArgs
        if ($LASTEXITCODE -ne 0) {
            Write-Host ''
            Write-Host 'gate FAILED - nothing was pushed. Fix it, or re-run with -SkipCheck to override.' -ForegroundColor Red
            exit 1
        }
    } else {
        Write-Host 'check-windows.ps1 not found - gate skipped.' -ForegroundColor Yellow
    }
}

# ---- delivery packages: is dist/ still current? ----
# WHY (2026-09-22): the kits in dist/ sat 9 hours behind the source, so a colleague was
# handed a package that predated the fixes (the local-harness-install helper was missing
# entirely). dist/ is gitignored, so a stale kit must NEVER block a code push - this only
# makes it impossible to miss. Rebuild with: python scripts\build_kit.py
if (-not $SkipCheck -and -not $Verify) {
    $kitScript = Join-Path $PSScriptRoot 'build_kit.py'
    $kitPy = Get-PyExe
    if ((Test-Path $kitScript) -and $kitPy) {
        Write-Host '=== delivery packages: dist\ vs current source ===' -ForegroundColor Cyan
        & $kitPy $kitScript --check
        if ($LASTEXITCODE -eq 2) {
            Write-Host ''
            Write-Host 'WARNING: a kit in dist\ is older than the source it was built from.' -ForegroundColor Yellow
            Write-Host '         Rebuild before handing anything out: python scripts\build_kit.py' -ForegroundColor Yellow
            Write-Host '         (not blocking this push - dist\ is not tracked by git)' -ForegroundColor DarkGray
        }
    } else {
        Write-Host 'scripts\build_kit.py or a python interpreter not found - package check skipped.' -ForegroundColor DarkGray
    }
}



$branch = ("$(& $script:GitExe symbolic-ref --short HEAD)").Trim()
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
    $directReason = Get-PushReason $script:LastPushText
    if ($code -eq 0) {
        if (Confirm-Push $branch $resolve 0) {
            $secs = [int]((Get-Date) - $started).TotalSeconds
            Write-Host "push OK in ${secs}s" -ForegroundColor Green
            exit 0
        }
        Write-Host '[FAIL] git exited 0, but origin/<branch> == local HEAD could not be verified - not reporting success.' -ForegroundColor Red
        exit 1
    }
    if ($script:HookBlocked) {
        # Nothing about the network is wrong - the repo itself refused the push, so stop
        # instead of burning every remaining IP and attempt on the same deterministic verdict.
        Write-Host ''
        Write-Host 'the local git hook rejected this push (see its message above) - the network is fine,' -ForegroundColor Red
        Write-Host 'so rotating IPs or retrying cannot help. Fix the commits, then re-run.' -ForegroundColor Red
        exit 1
    }
    Write-Host ("direct attempt {0} failed (git exit {1}): {2}" -f $attempt, $code, $directReason) -ForegroundColor Yellow

    # WHY re-probe here instead of reusing $pick: an IP that answered 401 during the direct
    # probe can blackhole the real upload a minute later. gh-proxy.js pins GH_IP at startup,
    # so handing it a stale address only produces "CONNECT tunnel failed, response 502".
    Write-Host '=== direct path failed: re-probing a live IP for the CONNECT proxy ===' -ForegroundColor Cyan
    [array]$fresh = @(Get-ProbedIPs)
    $lastReason = $directReason
    $proxyTried = $false
    $proxyOk = $false
    if ($fresh.Count -eq 0) {
        Write-Host '[FAIL] direct push failed, and no IP answered 401 at the receive-pack endpoint - the CONNECT proxy has nothing to tunnel to.' -ForegroundColor Red
    } else {
        $fresh | ForEach-Object { Write-Host ("    {0,-16} 401  {1:N2}s" -f $_.IP, $_.Latency) }
        [array]$proxyPick = @($fresh | Select-Object -First ([Math]::Max(1, [Math]::Min($TopN, $fresh.Count))))
        foreach ($cand in $proxyPick) {
            $px = Start-GhProxy $cand.IP
            if (-not $px.Ok) {
                # No node / no gh-proxy.js / proxy never listened: say exactly which, never skip silently.
                $lastReason = $px.Reason
                Write-Host ("    proxy could not start: " + $px.Reason) -ForegroundColor Red
                break
            }
            $proxyTried = $true
            Write-Host ("    proxy 127.0.0.1:{0} -> {1}" -f $px.Port, $cand.IP)
            try {
                $code2 = Invoke-PushViaProxy $px.Port $branch
                $lastReason = Get-PushReason $script:LastPushText
                if ($code2 -eq 0) {
                    # Verify through the live proxy first; a zero exit code is not proof.
                    if (Confirm-Push $branch '' $px.Port) {
                        $proxyOk = $true
                    } else {
                        $lastReason = 'git exited 0 but ls-remote did not confirm origin/<branch> == local HEAD'
                    }
                } else {
                    Write-Host ("    proxy push via {0} failed (git exit {1}): {2}" -f `
                        $cand.IP, $code2, $lastReason) -ForegroundColor Yellow
                }
            } finally {
                # WHY unconditionally: a proxy left behind keeps its dead IP cached and makes
                # the next attempt fail as a 502 even though it gets a brand-new port.
                Stop-GhProxy $px
            }
            if ($proxyOk) { break }
        }
        if ($proxyOk) {
            $secs = [int]((Get-Date) - $started).TotalSeconds
            Write-Host "push OK via proxy in ${secs}s" -ForegroundColor Green
            exit 0
        }
        if ($proxyTried) {
            Write-Host ("[FAIL] direct push failed; the CONNECT proxy also failed (last reason: {0})" -f $lastReason) -ForegroundColor Red
        } else {
            Write-Host ("[FAIL] direct push failed; the CONNECT proxy could not run ({0})" -f $lastReason) -ForegroundColor Red
        }
    }
    Write-Host "attempt $attempt failed; will re-probe and retry" -ForegroundColor Yellow
    Start-Sleep -Seconds 3
}

Write-Host "push failed after $Retries attempts. Network to GitHub may be closed right now; retry later." -ForegroundColor Red
exit 1
