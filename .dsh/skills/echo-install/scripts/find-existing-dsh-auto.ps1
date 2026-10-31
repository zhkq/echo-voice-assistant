# =====================================================================
# find-existing-dsh-auto.ps1 - try the usual places for @deepseek-ai/dsh
#
# Used after the user answers "yes, I already have DSH". We do NOT scan a
# whole drive: only common install roots + whatever the caller passes in
# -ExtraDir (the ECHO root being installed). Each candidate is delegated to
# find-existing-dsh.ps1, so path/shape acceptance has exactly one truth.
#
# Output: one JSON line, same fields as find-existing-dsh.ps1 plus "auto".
# Exit code is always 0; callers must look at .ok.
# =====================================================================
param(
    [string[]]$ExtraDir = @(),
    [string]$NodeExe = ''
)

$ErrorActionPreference = 'Stop'

$here = $PSScriptRoot
if (-not $here) { try { $here = Split-Path -Parent $PSCommandPath } catch { $here = '' } }
$probe = ''
if ($here) { $probe = Join-Path $here 'find-existing-dsh.ps1' }

$result = [ordered]@{
    ok      = $false
    input   = ''
    auto    = $true
    checked = 0
    entry   = ''
    node    = ''
    command = ''
    version = ''
    error   = ''
}

if (-not $probe -or -not (Test-Path -LiteralPath $probe)) {
    $result.error = 'missing find-existing-dsh.ps1'
    $result | ConvertTo-Json -Compress
    exit 0
}

$cands = New-Object System.Collections.Generic.List[string]
function Add-Cand([string]$p) {
    if ($p) { $cands.Add($p) }
}

# Caller's own install root (fresh ECHO) - may already contain dsh\app.
foreach ($d in @($ExtraDir)) { Add-Cand $d }

# ECHO roots (old/default layouts).
Add-Cand (Join-Path $env:USERPROFILE 'ECHO')
Add-Cand 'D:\ECHO'
Add-Cand 'C:\ECHO'

# Plain DSH installs people often choose.
foreach ($d in @('D:\dsh', 'C:\dsh', 'D:\DSH', 'C:\DSH',
                 (Join-Path $env:USERPROFILE 'dsh'))) {
    Add-Cand $d
}

# DSH Desktop install root (harmless if it has no standard npm package).
Add-Cand (Join-Path $env:ProgramFiles 'DeepSeek Harness')
Add-Cand (Join-Path $env:LOCALAPPDATA 'Programs\DeepSeek Harness')
Add-Cand (Join-Path $env:LOCALAPPDATA 'Programs\DeepSeek Harness\resources\app.asar\dsh')

# Official DeepSeek Harness desktop (2026-10-31): ships a headless dsh CLI at
# resources\runtime\cli\bin\dsh.cmd. Prefer the shim directly: it needs no
# system Node.js and is what `Prepare-Agent` writes into harnessCommand.
Add-Cand (Join-Path $env:LOCALAPPDATA 'Programs\DeepSeek Harness\resources\runtime\cli\bin\dsh.cmd')
Add-Cand (Join-Path $env:ProgramFiles 'DeepSeek Harness\resources\runtime\cli\bin\dsh.cmd')

# Global npm roots (node.exe may be bundled, so also the nodejs dir).
Add-Cand (Join-Path $env:APPDATA 'npm\node_modules\@deepseek-ai\dsh')
Add-Cand (Join-Path $env:LOCALAPPDATA 'npm\node_modules\@deepseek-ai\dsh')
Add-Cand (Join-Path $env:ProgramFiles 'nodejs\node_modules\@deepseek-ai\dsh')
Add-Cand (Join-Path $env:ProgramFiles 'nodejs')

$seen = @{}
foreach ($c in $cands) {
    if (-not $c) { continue }
    $key = $c.ToLowerInvariant()
    if ($seen.ContainsKey($key)) { continue }
    $seen[$key] = $true
    if (-not (Test-Path -LiteralPath $c)) { continue }
    $result.checked = [int]$result.checked + 1
    $out = @()
    try { $out = & $probe -Path $c -NodeExe $NodeExe 2>&1 } catch { continue }
    $line = @($out | ForEach-Object { "$_" } | Where-Object { $_.Trim().StartsWith('{') } |
              Select-Object -Last 1)
    if (-not $line) { continue }
    try { $obj = $line | ConvertFrom-Json } catch { continue }
    if ($obj.ok) {
        $obj | Add-Member -NotePropertyName auto -NotePropertyValue $true -Force
        $obj | ConvertTo-Json -Compress
        exit 0
    }
}

$result.error = 'not found in the usual places (specify a directory manually)'
$result | ConvertTo-Json -Compress
exit 0
