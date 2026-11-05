#Requires -Version 5.1
<#
.SYNOPSIS
  Verify the three ECHO download packages before/after deployment.

.DESCRIPTION
  Reads site/assets/downloads.json (the single source of truth) and checks:
    * -Dir <dir>      the three files exist and their byte sizes match; -Full also
                      computes SHA-256 and compares it.
    * -BaseUrl <url>  HEADs each URL and compares Content-Length with the JSON.
  Missing files, size mismatches and SHA mismatches print expected vs actual and
  exit 1.  ASCII-only on purpose: Windows PowerShell 5.1 reads this file without
  an encoding/BOM dependency.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\verify-downloads.ps1 -Dir D:\ECHO-downloads -Full

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\verify-downloads.ps1 -BaseUrl https://echoserver.zhkq.online:8443/downloads
#>
[CmdletBinding()]
param(
    [string]$Dir = "",
    [string]$BaseUrl = "",
    [switch]$Full,
    [string]$JsonPath = ""
)

$ErrorActionPreference = "Stop"
try {
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
} catch {
    # Older .NET may not expose the enum member; Invoke-WebRequest will use its default.
}
$script:failures = 0

function Write-Fail([string]$message) {
    Write-Host "FAIL: $message" -ForegroundColor Red
    $script:failures++
}

function Write-Ok([string]$message) {
    Write-Host "OK:   $message" -ForegroundColor Green
}

if (-not $Dir -and -not $BaseUrl) {
    Write-Host "Usage: verify-downloads.ps1 -Dir <local-dir> [-Full]"
    Write-Host "   or: verify-downloads.ps1 -BaseUrl <https://host/downloads>"
    exit 2
}

$repoRoot = Split-Path -Parent $PSScriptRoot
if (-not $JsonPath) {
    $JsonPath = Join-Path $repoRoot (Join-Path "site" (Join-Path "assets" "downloads.json"))
}
if (-not (Test-Path -LiteralPath $JsonPath -PathType Leaf)) {
    Write-Fail "downloads.json not found: $JsonPath"
    exit 1
}

try {
    $data = Get-Content -LiteralPath $JsonPath -Raw -Encoding UTF8 | ConvertFrom-Json
} catch {
    Write-Fail "cannot parse $JsonPath : $($_.Exception.Message)"
    exit 1
}
if (-not $data.files -or $data.files.Count -ne 3) {
    Write-Fail "downloads.json must list exactly three files"
    exit 1
}

if ($Dir) {
    if (-not (Test-Path -LiteralPath $Dir -PathType Container)) {
        Write-Fail "directory not found: $Dir"
    } else {
        $root = (Resolve-Path -LiteralPath $Dir).Path
        foreach ($file in $data.files) {
            $name = [string]$file.name
            $expected = [int64]$file.size
            $path = Join-Path $root $name
            if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
                Write-Fail "$($file.platform): missing file '$name' under $root (expected $expected bytes)"
                continue
            }
            $actual = (Get-Item -LiteralPath $path).Length
            if ($actual -ne $expected) {
                Write-Fail "$($file.platform): size mismatch for '$name' expected=$expected actual=$actual"
                continue
            }
            if ($Full) {
                $expectedSha = ([string]$file.sha256).ToUpperInvariant()
                $actualSha = (Get-FileHash -Path $path -Algorithm SHA256).Hash.ToUpperInvariant()
                if ($actualSha -ne $expectedSha) {
                    Write-Fail "$($file.platform): SHA-256 mismatch for '$name' expected=$expectedSha actual=$actualSha"
                    continue
                }
            }
            Write-Ok "$($file.platform): $name size=$actual"
        }
    }
}

if ($BaseUrl) {
    $base = $BaseUrl.TrimEnd("/")
    foreach ($file in $data.files) {
        $name = [string]$file.name
        $expected = [int64]$file.size
        $url = $base + "/" + [uri]::EscapeDataString($name)
        try {
            $response = Invoke-WebRequest -Uri $url -Method Head -UseBasicParsing -TimeoutSec 60 -ErrorAction Stop
        } catch {
            Write-Fail "$($file.platform): HEAD $url failed: $($_.Exception.Message)"
            continue
        }
        if ([int]$response.StatusCode -ne 200) {
            Write-Fail "$($file.platform): $url returned HTTP $($response.StatusCode), expected 200"
            continue
        }
        $contentLength = $response.Headers["Content-Length"]
        if ($contentLength -is [array]) {
            $contentLength = $contentLength[0]
        }
        if ($null -eq $contentLength) {
            $contentLength = $response.RawContentLength
        }
        if ($null -eq $contentLength) {
            Write-Fail "$($file.platform): $url has no Content-Length header"
            continue
        }
        try {
            $actual = [int64]$contentLength
        } catch {
            Write-Fail "$($file.platform): $url has an invalid Content-Length '$contentLength'"
            continue
        }
        if ($actual -ne $expected) {
            Write-Fail "$($file.platform): size mismatch for $url expected=$expected actual=$actual"
            continue
        }
        Write-Ok "$($file.platform): $url length=$actual"
    }
}

if ($script:failures -gt 0) {
    Write-Host "FAILED: $script:failures check(s) failed." -ForegroundColor Red
    exit 1
}
Write-Host "All download checks passed."
exit 0
