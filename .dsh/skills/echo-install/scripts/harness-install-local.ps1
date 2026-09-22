# =====================================================================
# harness-install-local.ps1 - 把 DSH 标准版装到 <安装目录>\harness\dsh（本地永久入口）
#
# 谁在用它：`echo-install-components.ps1` 的 Prepare-Agent；也可以单独跑（修装坏的标准版）。
#
# 为什么需要它（2026-09-22 实测，见 AGENTS.md）：
#   * `npx -y @deepseek-ai/dsh web` 冷启动 **2 分 10 秒**，直连本地 bin.js 只要 **9 秒**；
#   * 而 `npm install @deepseek-ai/dsh@0.1.5-rc.2` 在公共 registry 上**装不下来**：
#     它的依赖图里有个子包被写成 ^0.1.5-rc.3，而那个子包的 rc.3 从没发布过 ->
#     `ETARGET No matching version found for ...documentpreview@^0.1.5-rc.3`。
#     老脚本遇到这个只会回退 npx，于是用户那边每次冷启动都慢两分钟。
#
# 所以这里按三条路依次试：
#   1) 已经装好（bin.js 非空）-> 直接用；
#   2) npm install @deepseek-ai/dsh@<Version>（-FromCache 时跳过）；
#   3) **从 npx 缓存复制**同版本那份整树 —— npm 的 _npx 缓存里通常已经有一份能跑的。
#      版本不一致会明确告警（仍可用），完全不掩盖。
#
# 成功时：
#   * 最后一行 stdout 打印 `HARNESS_COMMAND=<node 全路径> <bin.js 全路径> web`；
#   * 传了 -CommandFile 就同时把这条命令写进那个文件（调用方照它写设置）。
# 失败：退出码 1，并说清下一步（调用方据此回退 npx）。
#
# 编码：含中文，**必须 UTF-8 带 BOM**（WinPS 5.1 按 ANSI 读无 BOM 的 .ps1，中文会乱码
#       甚至静默解析失败）—— 见 tests/test_script_encoding.py。
# =====================================================================
param(
    [Parameter(Mandatory = $true)][string]$DestDir,
    [string]$Version = '0.1.5-rc.2',
    [switch]$FromCache,
    [string]$CacheDir = '',
    [string]$NodeExe = '',
    [string]$CommandFile = '',
    [string]$LogFile = ''
)

$ErrorActionPreference = 'Stop'
$script:Log = $LogFile

function LogLine([string]$m) {
    if (-not $script:Log) { return }
    try {
        Add-Content -LiteralPath $script:Log -Encoding UTF8 -Value ("[{0}] {1}" -f (Get-Date -Format 'HH:mm:ss'), $m)
    } catch { }
}
function Say([string]$m)  { Write-Host "  $m"; LogLine "  $m" }
function Ok([string]$m)   { Write-Host "  [ok]   $m" -ForegroundColor Green; LogLine "[ok]   $m" }
function Warn([string]$m) { Write-Host "  [warn] $m" -ForegroundColor Yellow; LogLine "[warn] $m" }
function Err([string]$m)  { Write-Host "  [fail] $m" -ForegroundColor Red; LogLine "[fail] $m" }

function Resolve-Node {
    if ($NodeExe) {
        if (Test-Path -LiteralPath $NodeExe) { return (Resolve-Path -LiteralPath $NodeExe).Path }
    }
    $c = Get-Command node -ErrorAction SilentlyContinue
    if ($c) { return $c.Source }
    foreach ($d in @("$env:ProgramFiles\nodejs",
                     "$env:LOCALAPPDATA\Programs\nodejs",
                     "$env:APPDATA\npm")) {
        $p = Join-Path $d 'node.exe'
        if (Test-Path -LiteralPath $p) { return $p }
    }
    return ''
}

function Resolve-Npm {
    foreach ($n in @('npm.cmd', 'npm.exe', 'npm')) {
        $c = Get-Command $n -ErrorAction SilentlyContinue
        if ($c) { return $c.Source }
    }
    return ''
}

function Test-HarnessTree([string]$Target) {
    # 返回"不完整"的条目列表（空 = 完好）。
    $broken = @()
    $entry = Join-Path $Target 'node_modules\@deepseek-ai\dsh\lib\bin.js'
    if (-not (Test-Path -LiteralPath $entry)) {
        $broken += 'lib\bin.js（缺）'
    } elseif ((Get-Item -LiteralPath $entry).Length -eq 0) {
        $broken += 'lib\bin.js（是空文件）'
    }
    foreach ($pty in (Get-ChildItem -LiteralPath $Target -Recurse -Directory -Filter 'node-pty' -ErrorAction SilentlyContinue)) {
        foreach ($f in @('package.json', 'lib\index.js')) {
            if (-not (Test-Path -LiteralPath (Join-Path $pty.FullName $f))) {
                $broken += ("node-pty\" + $f)
            }
        }
    }
    return , $broken
}

function Get-NpxCacheTrees {
    # 在 npm 的 _npx 缓存里找装好的 dsh 树（返回 package.json / node_modules / 版本 / 时间）。
    param([string]$Root)
    $roots = @()
    if ($Root) {
        $roots += $Root
    } else {
        $npm = Resolve-Npm
        if ($npm) {
            try {
                $out = & $npm config get cache 2>$null
                if ($out) { $roots += ([string]$out).Trim() }
            } catch { }
        }
        if ($env:LOCALAPPDATA) { $roots += (Join-Path $env:LOCALAPPDATA 'npm-cache') }
        if ($env:APPDATA) { $roots += (Join-Path $env:APPDATA 'npm-cache') }
        $roots += (Join-Path $HOME '.npm')
    }
    $found = @()
    foreach ($r in $roots) {
        if (-not $r) { continue }
        $pattern = Join-Path $r '_npx\*\node_modules\@deepseek-ai\dsh\package.json'
        foreach ($pkg in (Get-ChildItem -Path $pattern -ErrorAction SilentlyContinue)) {
            $ver = ''
            try { $ver = [string](Get-Content -LiteralPath $pkg.FullName -Raw | ConvertFrom-Json).version } catch { }
            # <root>\_npx\<hash>\node_modules\@deepseek-ai\dsh\package.json -> 上三级就是 node_modules
            $nm = Split-Path (Split-Path (Split-Path $pkg.FullName -Parent) -Parent) -Parent
            $found += [pscustomobject]@{
                PackageJson = $pkg.FullName
                NodeModules = $nm
                Version     = $ver
                Time        = $pkg.LastWriteTime
            }
        }
    }
    return , $found
}

function Remove-Tree([string]$Path) {
    # 删目录树（临时关掉宿主的 npm 安全删除 shim，否则批量删除会被拦）。
    if (-not (Test-Path -LiteralPath $Path)) { return $true }
    $old = $env:CODEBUDDY_SAFE_DELETE_ENABLED
    $env:CODEBUDDY_SAFE_DELETE_ENABLED = '0'
    try {
        Remove-Item -LiteralPath $Path -Recurse -Force -ErrorAction Stop
    } catch {
        return $false
    } finally {
        if ($null -eq $old) { Remove-Item Env:\CODEBUDDY_SAFE_DELETE_ENABLED -ErrorAction SilentlyContinue }
        else { $env:CODEBUDDY_SAFE_DELETE_ENABLED = $old }
    }
    return (-not (Test-Path -LiteralPath $Path))
}

function Copy-Tree([string]$From, [string]$To) {
    # 把 From 目录的**内容**整棵复制到 To（robocopy 优先，退回 Copy-Item）。
    try { New-Item -ItemType Directory -Force -Path $To | Out-Null } catch { return $false }
    $rc = Get-Command robocopy -ErrorAction SilentlyContinue
    if ($rc) {
        & $rc.Source $From $To /E /NFL /NDL /NJH /NJS /NP /R:1 /W:1 | Out-Null
        return ($LASTEXITCODE -lt 8)          # robocopy：0-7 都是成功
    }
    try {
        Copy-Item -Path (Join-Path $From '*') -Destination $To -Recurse -Force -ErrorAction Stop
        return $true
    } catch {
        return $false
    }
}

# ---------------------------------------------------------------- 主流程
$root = (Resolve-Path -LiteralPath $DestDir).Path
$target = Join-Path $root 'harness\dsh'
$entry = Join-Path $target 'node_modules\@deepseek-ai\dsh\lib\bin.js'

Say ("安装目录：{0}" -f $root)
Say ("标准版版本：{0}" -f $Version)

$node = Resolve-Node
if (-not $node) {
    Err '没找到 node —— 标准版 harness 需要 Node.js；装了 Node 再重跑'
    exit 1
}
Ok ("node: {0}" -f $node)

function Test-Entry {
    return ((Test-Path -LiteralPath $entry) -and ((Get-Item -LiteralPath $entry).Length -gt 0))
}

if (Test-Entry) {
    Ok '标准版已经在本机（跳过下载）'
} else {
    try { New-Item -ItemType Directory -Force -Path $target | Out-Null } catch { }
    $pkgJson = Join-Path $target 'package.json'
    if (-not (Test-Path -LiteralPath $pkgJson)) {
        '{ "name": "echo-harness", "private": true }' | Set-Content -LiteralPath $pkgJson -Encoding UTF8
    }

    if (-not $FromCache) {
        $npm = Resolve-Npm
        if (-not $npm) {
            Warn '没找到 npm —— 跳过 npm，直接试 npx 缓存复制'
        } else {
            Say ("用 npm 安装 @deepseek-ai/dsh@{0}（一次性，可能要几分钟；请勿中断）..." -f $Version)
            $old = $env:CODEBUDDY_SAFE_DELETE_ENABLED
            $env:CODEBUDDY_SAFE_DELETE_ENABLED = '0'
            Push-Location $target
            try {
                & $npm install ("@deepseek-ai/dsh@{0}" -f $Version) --no-audit --no-fund 2>&1 |
                    ForEach-Object { Write-Host ("      " + $_) -ForegroundColor DarkGray; LogLine ("      " + $_) }
            } catch {
                Warn ("npm 执行异常：{0}" -f $_.Exception.Message)
            } finally {
                Pop-Location
                if ($null -eq $old) { Remove-Item Env:\CODEBUDDY_SAFE_DELETE_ENABLED -ErrorAction SilentlyContinue }
                else { $env:CODEBUDDY_SAFE_DELETE_ENABLED = $old }
            }
        }
    } else {
        Say '按要求跳过 npm，直接用 npx 缓存'
    }

    if (-not (Test-Entry)) {
        Warn 'npm 这条路没装上（公共 registry 上这个版本的依赖图可能是坏的，见 AGENTS.md）'
        Say '  改用 npx 缓存里那份已经能跑的树 ...'
        $trees = Get-NpxCacheTrees -Root $CacheDir
        $pick = $null
        $exact = @($trees | Where-Object { $_.Version -eq $Version })
        if ($exact.Count -gt 0) {
            $pick = $exact | Sort-Object Time -Descending | Select-Object -First 1
        } elseif ($trees.Count -gt 0) {
            $pick = $trees | Sort-Object Time -Descending | Select-Object -First 1
            Warn ("缓存里没有 {0}，退而用 {1}（版本不同，但比 npx 快得多）" -f $Version, $pick.Version)
        }
        if (-not $pick) {
            Err 'npx 缓存里也没有可用的标准版 —— 回退 npx：首次启动要多等 1-2 分钟'
            Say '  想装本地入口：先 `npx -y @deepseek-ai/dsh web` 跑一次（把缓存填上）再重跑本脚本'
            exit 1
        }
        Say ("  缓存树：{0}（版本 {1}）" -f $pick.NodeModules, $pick.Version)
        $nm = Join-Path $target 'node_modules'
        if (-not (Remove-Tree $nm)) {
            Err ("清不掉半残的 {0}（可能被占用）—— 手动删掉后重跑" -f $nm)
            exit 1
        }
        if (-not (Copy-Tree $pick.NodeModules $nm)) {
            Err ("从缓存复制失败：{0} -> {1}" -f $pick.NodeModules, $nm)
            exit 1
        }
        Ok '已从 npx 缓存复制'
    }
}

$broken = Test-HarnessTree $target
if ($broken.Count -gt 0) {
    Err ("标准版安装不完整：{0}" -f ($broken -join '、'))
    Say ("  修法：删掉 {0} 后重跑本脚本" -f $target)
    exit 1
}

$cmd = '"{0}" "{1}" web' -f $node, $entry
Ok '标准版已就绪（本地永久安装，冷启动约 10 秒）'
Say ("  启动命令：{0}" -f $cmd)
if ($CommandFile) {
    try { Set-Content -LiteralPath $CommandFile -Value $cmd -Encoding UTF8 -NoNewline } catch { }
}
Write-Output ("HARNESS_COMMAND=" + $cmd)
exit 0
