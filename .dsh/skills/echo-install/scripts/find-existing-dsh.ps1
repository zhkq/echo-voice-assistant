# =====================================================================
# find-existing-dsh.ps1 - probe one directory for an installed @deepseek-ai/dsh
#
# Used by echo-install-components.ps1 when the user says "DSH is already
# installed" and types a folder. ASCII-only on purpose (no BOM games).
#
# Input:
#   -Path    what the user typed: may be quoted (Explorer "Copy as path"),
#            may end with a backslash, may use %VAR% or ~.
#   -NodeExe optional node.exe to prefer (the component installer usually
#            already resolved one).
#
# Output (stdout, one JSON line):
#   { ok, input, normalized, entry, node, command, version, error }
#   command is the value ECHO should store in harnessCommand.
#   It quotes both absolute paths; Windows paths cannot contain a double
#   quote, so no fragile escaping is needed.
# Exit code is always 0; callers must look at .ok.
# =====================================================================
param(
    [Parameter(Mandatory = $true)][string]$Path,
    [string]$NodeExe = ''
)

$ErrorActionPreference = 'Stop'

function Normalize-UserPath([string]$raw) {
    $p = [string]$raw
    if ($null -eq $p) { return '' }
    $p = $p.Trim()

    # Explorer's "Copy as path" gives a quoted path. Strip ONE matching
    # pair of quotes only; internal quotes are never legal in Windows paths.
    if ($p.Length -ge 2) {
        $first = $p.Substring(0, 1)
        $last = $p.Substring($p.Length - 1, 1)
        if (($first -eq '"' -and $last -eq '"') -or
            ($first -eq "'" -and $last -eq "'")) {
            $p = $p.Substring(1, $p.Length - 2).Trim()
        }
    }

    $p = [Environment]::ExpandEnvironmentVariables($p)
    if ($p -eq '~') {
        $p = [string]$env:USERPROFILE
    } elseif ($p.StartsWith('~\') -or $p.StartsWith('~/')) {
        $p = Join-Path ([string]$env:USERPROFILE) $p.Substring(2)
    }

    # The user may paste forward slashes. Normalise them for Test-Path and
    # for the command string (node accepts both, but one shape is easier to
    # reason about).
    $p = $p -replace '/', '\'

    # Keep a drive root as-is; remove trailing separators from everything
    # else (a trailing backslash would escape the closing quote of a quoted
    # argument on the next command line).
    if ($p -match '^[A-Za-z]:\\$') { return $p }
    $p = $p -replace '\\+$', ''
    return $p
}

function Quote-WinArg([string]$s) {
    # Windows file names cannot contain '"'. Escape defensively anyway; the
    # important part is that backslashes are left untouched.
    return '"' + ($s -replace '"', '\"') + '"'
}

function Resolve-NodePath([string]$preferred) {
    if ($preferred) {
        try {
            if (Test-Path -LiteralPath $preferred -PathType Leaf) {
                return (Resolve-Path -LiteralPath $preferred).Path
            }
        } catch { }
    }
    $cmd = Get-Command node -ErrorAction SilentlyContinue
    if ($cmd -and $cmd.Source) { return $cmd.Source }
    foreach ($p in @("$env:ProgramFiles\nodejs\node.exe",
                     "$env:LOCALAPPDATA\Programs\nodejs\node.exe",
                     "$env:APPDATA\npm\node.exe")) {
        if ($p -and (Test-Path -LiteralPath $p)) { return (Resolve-Path -LiteralPath $p).Path }
    }
    return ''
}

$result = [ordered]@{
    ok         = $false
    input      = $Path
    normalized = ''
    entry      = ''
    node       = ''
    command    = ''
    version    = ''
    error      = ''
}

try {
    $p = Normalize-UserPath $Path
    $result.normalized = $p
    if (-not $p) { throw 'path is empty' }
    if (-not (Test-Path -LiteralPath $p)) { throw ('path not found: ' + $p) }

    $item = Get-Item -LiteralPath $p -Force
    $entry = ''
    $isCli = $false

    if ($item.PSIsContainer) {
        # The same tree can be handed to us in several shapes:
        #   D:\DSH                                   (contains node_modules)
        #   D:\DSH\node_modules                      (contains @deepseek-ai)
        #   D:\DSH\node_modules\@deepseek-ai\dsh     (the package itself)
        #   D:\ECHO                                  (3.0 layout: dsh\app\node_modules)
        #   D:\ECHO\dsh\app                          (contains node_modules)
        #   D:\ECHO\harness\dsh                      (old ECHO layout)
        $roots = New-Object System.Collections.Generic.List[string]
        $roots.Add($p)
        $roots.Add((Join-Path $p 'node_modules'))
        $roots.Add((Join-Path $p 'dsh\app\node_modules'))
        $roots.Add((Join-Path $p 'dsh\app'))
        $roots.Add((Join-Path $p 'harness\dsh\node_modules'))
        $roots.Add((Join-Path $p 'harness\dsh'))
        $roots.Add((Join-Path $p 'echo-core\dsh\app\node_modules'))
        $roots.Add((Join-Path $p 'resources\app.asar\dsh'))

        $candidates = New-Object System.Collections.Generic.List[string]
        foreach ($r in $roots) {
            $candidates.Add((Join-Path $r '@deepseek-ai\dsh\lib\bin.js'))
            $candidates.Add((Join-Path $r 'node_modules\@deepseek-ai\dsh\lib\bin.js'))
            # Official DeepSeek Harness desktop (2026-10-31): the app ships a
            # headless CLI shim instead of a plain node + bin.js pair.
            $candidates.Add((Join-Path $r 'resources\runtime\cli\bin\dsh.cmd'))
            $candidates.Add((Join-Path $r 'cli\bin\dsh.cmd'))
        }
        # Inputs that point straight at a package-parent layer.
        $candidates.Add((Join-Path $p 'lib\bin.js'))
        $candidates.Add((Join-Path $p 'dsh\lib\bin.js'))
        $candidates.Add((Join-Path $p '@deepseek-ai\dsh\lib\bin.js'))
        $candidates.Add((Join-Path $p 'node_modules\@deepseek-ai\dsh\lib\bin.js'))
        $candidates.Add((Join-Path $p 'resources\app.asar\dsh\lib\bin.js'))
        $candidates.Add((Join-Path $p 'resources\runtime\cli\bin\dsh.cmd'))

        foreach ($c in $candidates) {
            if (Test-Path -LiteralPath $c -PathType Leaf) {
                $entry = (Resolve-Path -LiteralPath $c).Path
                $isCli = ($entry -like '*.cmd' -or $entry -like '*.exe')
                break
            }
        }
    } else {
        $leaf = [System.IO.Path]::GetFileName($p)
        if ($leaf -ieq 'bin.js') {
            $entry = (Resolve-Path -LiteralPath $p).Path
        } elseif ($leaf -ieq 'dsh.cmd' -or $leaf -ieq 'dsh.exe') {
            # Direct CLI shim (official desktop: ...\resources\runtime\cli\bin\dsh.cmd).
            $entry = (Resolve-Path -LiteralPath $p).Path
            $isCli = $true
        } else {
            throw ('not a directory, not a bin.js, and not a dsh CLI: ' + $p)
        }
    }

    if (-not $entry) {
        throw 'no @deepseek-ai/dsh/lib/bin.js or dsh.cmd found under this path'
    }

    if ($isCli) {
        $result.ok = $true
        $result.entry = $entry
        $result.node = ''
        $result.command = (Quote-WinArg $entry) + ' web'
        try {
            $ver = @(& $entry --version 2>&1 | ForEach-Object { "$_".Trim() } |
                     Where-Object { $_ -match '^\d+\.\d+' } | Select-Object -Last 1)
            if ($ver.Count -gt 0) { $result.version = [string]$ver[-1] }
        } catch { }
        $result | ConvertTo-Json -Compress
        exit 0
    }

    $node = Resolve-NodePath $NodeExe
    if (-not $node) {
        throw 'node.exe not found (install Node.js, or pass -NodeExe)'
    }

    $result.ok = $true
    $result.entry = $entry
    $result.node = $node
    $result.command = (Quote-WinArg $node) + ' ' + (Quote-WinArg $entry) + ' web'

    # package.json is at <package>\package.json; entry is <package>\lib\bin.js
    $pkgRoot = Split-Path -Parent (Split-Path -Parent $entry)
    $pkgJson = Join-Path $pkgRoot 'package.json'
    if (Test-Path -LiteralPath $pkgJson) {
        try {
            $result.version = [string]((Get-Content -LiteralPath $pkgJson -Raw | ConvertFrom-Json).version)
        } catch { }
    }
} catch {
    $result.error = $_.Exception.Message
}

$result | ConvertTo-Json -Compress
exit 0
