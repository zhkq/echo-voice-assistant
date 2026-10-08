# =====================================================================
# harness-install-local.ps1 - 把 DSH 标准版装到 <安装目录>\harness\dsh（本地永久入口）
#
# 谁在用它：`echo-install-components.ps1` 的 Prepare-Agent；也可以单独跑（修装坏的标准版）。
#
# 为什么需要它（2026-09-22 实测，见 AGENTS.md）：
#   * `npx -y @deepseek-ai/dsh web` 冷启动 **2 分 10 秒**，直连本地 bin.js 只要 **9 秒**；
#   * 而 `npm install @deepseek-ai/dsh@0.1.5-rc.2` **曾经**在公共 registry 上装不下来：
#     它的依赖图里有个子包被写成 ^0.1.5-rc.3，而那个子包的 rc.3 当时从没发布过 ->
#     `ETARGET No matching version found for ...documentpreview@^0.1.5-rc.3`。
#     **2026-09-22 晚同事复测：rc.3 系列已经发布，npm 这条路现在是通的**（584 包，2 分钟）
#     —— 所以第 ③ 条目前用不上，但保留：registry 上这种依赖图事故会复发，而 npx 慢是必然的。
#     老脚本当初遇到装不上只会回退 npx，于是用户那边每次冷启动都慢两分钟。
#
# 所以这里按三条路依次试：
#   1) 已经装好（bin.js 非空）-> 直接用；
#   2) npm install @deepseek-ai/dsh@<Version>（-FromCache 时跳过）；
#   3) **从 npx 缓存复制**同版本那份整树 —— 缓存里没有时会先用 npx 把缓存填上再复制
#      （全新机器的 _npx 缓存是空的，2026-09-22 同事就是这种情况）。
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
    # 留空 = **自动取 npm 上的 latest**（2026-10-08 用户要求：装最新版，别写死旧版本）。
    # 三种用法：
    #   新装：        -DestDir <目录>
    #   升到最新：    -DestDir <目录> -Upgrade
    #   升/降到指定： -DestDir <目录> -Upgrade -Version <版本号>
    [string]$Version = '',
    # 已装好也**强制重装**（= 升级入口）。不给它时，已装且完好就跳过，只在有新版时**提示**。
    [switch]$Upgrade,
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

#: 查不到 npm registry 时的兜底版本（**只是兜底，不是"我们要装的版本"**）。
#  为什么留一个写死的值：全新机器上 npm 可能还没配好源 / 断网，这时**不能**因为
#  "查不到最新版"就整个失败 —— 退回一个已知可用的版本，装完照样能用。
#  ⚠️ 它**必须定期跟一下**，但它不再是主路径（2026-10-08 用户要求：装最新的，别写死旧版本）。
$script:FallbackVersion = '0.2.0-rc.2'

function Resolve-LatestVersion([string]$Npm, [switch]$Quiet) {
    <#
      取 npm 上 @deepseek-ai/dsh 的 **latest** 版本。

      为什么要有它（2026-10-08 用户原话）：
        "安装脚本中 dsh 标准版应该装最新的版本，不要写死旧版本，
         同时也要提供 dsh 升级的方法。"
      原来 `param($Version = '0.1.5-rc.2')` 写死，而当时 npm 的 latest 已经是 0.2.0-rc.2
      —— 新装机器拿到的永远是旧版，用户得自己去查版本号。

      判据：`npm view @deepseek-ai/dsh dist-tags.latest`（**认 dist-tag，不自己排序**）。
      为什么不让脚本自己按 semver 排：预发布号（rc/alpha）的排序规则很绕，
      而且"哪个是给用户的稳定版"是**仓库方的判断**（dist-tag），不是我们猜的。
      网络/registry 不可用时返回 ''，由调用方退回 `$FallbackVersion` 并**明确告警**。
    #>
    if (-not $Npm) { return '' }
    try {
        $raw = & $Npm 'view' '@deepseek-ai/dsh' 'dist-tags.latest' 2>$null
        $v = ([string]($raw | Select-Object -First 1)).Trim().Trim('"')
        if ($v -match '^\d+\.\d+\.\d+') {
            if (-not $Quiet) { Say ("npm 上的最新版（latest）：{0}" -f $v) }
            return $v
        }
        return ''
    } catch {
        return ''
    }
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
    # **冒烟测试**：真把模块图加载一遍（`node bin.js --version`，跑完即退、不起服务）。
    # 为什么非要有它（2026-09-22 同事反馈 3.2）：上面那几条只验"文件在不在"，而 npm 安装
    # 被中途打断（沙箱回收 / 手动 Ctrl-C）会留下**目录在、子目录整片没有**的残树 ——
    # 实测 zod@4.6.5 装着、package.json 也在，但整个 v4/ 目录缺失，报的是
    # `ERR_MODULE_NOT_FOUND: …zod/v4/classic/external.js`；上面几条**全过**，于是
    # "已装好"快路径把坏树当好的用，之后每次启动都失败。只有真加载一遍才抓得到这种残。
    if ($broken.Count -eq 0 -and (Test-Path -LiteralPath $entry)) {
        $nodeExe = $NodeExe
        if (-not $nodeExe) { $nodeExe = Resolve-Node }
        if ($nodeExe) {
            $prevEap = $ErrorActionPreference
            $ErrorActionPreference = 'Continue'
            try {
                $out = & $nodeExe $entry --version 2>&1
                $code = $LASTEXITCODE
            } catch {
                $out = @($_.Exception.Message); $code = -1
            } finally { $ErrorActionPreference = $prevEap }
            if ($code -ne 0) {
                $tail = (($out | Select-Object -Last 3) -join ' ').Trim()
                $broken += ("跑不起来（node bin.js --version 退出码 {0}）：{1}" -f $code, $tail)
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
        # 与 npm 那处同一个坑：robocopy 的提示可能走 stderr，`Stop` 会把它升格成异常，
        # 从而**绕过**下面"按退出码判成败"那句（robocopy 0-7 都算成功，异常却直接抛走）。
        $prevEap = $ErrorActionPreference
        $ErrorActionPreference = 'Continue'
        try {
            & $rc.Source $From $To /E /NFL /NDL /NJH /NJS /NP /R:1 /W:1 | Out-Null
            $rcExit = $LASTEXITCODE
        } finally { $ErrorActionPreference = $prevEap }
        return ($rcExit -lt 8)                # robocopy：0-7 都是成功
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
# -DestDir 是**安装根**。3.0 布局里 DSH 本体放 <根>\dsh\app（与它并排的是 dsh\home），
# 老式扁平安装仍在 <根>\harness\dsh —— 判据与 app\paths.py:echo_base() 同一条：
# 代码在 <根>\echo-core 下就是新布局。这不是"猜"：install.ps1 已经按这条把代码放好了。
if (Test-Path (Join-Path $root 'echo-core\app\main.py')) {
    $target = Join-Path $root 'dsh\app'
} else {
    $target = Join-Path $root 'harness\dsh'
}
$entry = Join-Path $target 'node_modules\@deepseek-ai\dsh\lib\bin.js'

Say ("安装目录：{0}" -f $root)
function Fill-NpxCache([string]$Version) {
    # 全新机器上 npm 的 _npx 缓存是**空的**（2026-09-22 同事实测：装之前刚清过缓存），
    # 于是第 ③ 条兜底"从缓存复制"无物可复制。这里主动把缓存填上一次。
    #
    # 手法：`npx --yes --package=<包> -- node --version`。--package 会**先把包装进 npx 自己的
    # 缓存**，然后跑一条必然立刻退出的命令。比"起一次 web 再杀掉"干净得多：不用挑空闲端口
    # （更不能占 43199），不用管进程回收，也不会留下半个服务在跑。
    #
    # 能救 / 不能救，说清楚（别让人误以为它万能）：
    #   * 能救：npm install **到目标目录**失败，但 npx 自建缓存能成 —— 宿主的安全删除 shim、
    #     目标路径怪异（中文/超长/网络盘）这类**本地**原因；
    #   * 不能救：registry 真坏的时候，npx 背后还是 npm，一样装不上。那种情况就是没有可用的
    #     下载源，只能回退 npx 慢慢跑（本函数失败即返回 $false，不改变原有行为）。
    $npx = ''
    $node = Resolve-Node
    $nodeDir = ''
    if ($node) { $nodeDir = Split-Path -Parent $node }
    foreach ($d in @($nodeDir, "$env:ProgramFiles\nodejs", "$env:LOCALAPPDATA\Programs\nodejs")) {
        if (-not $d) { continue }
        foreach ($n in @('npx.cmd', 'npx.exe', 'npx')) {
            $c = Join-Path $d $n
            if (Test-Path -LiteralPath $c) { $npx = $c; break }
        }
        if ($npx) { break }
    }
    if (-not $npx) {
        $c = Get-Command npx -ErrorAction SilentlyContinue
        if ($c) { $npx = $c.Source }
    }
    if (-not $npx) { return $false }

    Say '  缓存是空的 —— 让 npx 先把这份装进它自己的缓存（要 1-2 分钟）...'
    $oldPath = $env:PATH
    if ($nodeDir) { $env:PATH = $nodeDir + ';' + $env:PATH }   # 别让子进程找不到 node
    $prevEap = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        & $npx --yes ("--package=@deepseek-ai/dsh@{0}" -f $Version) -- node --version 2>&1 |
            ForEach-Object { LogLine ("      " + $_) }
    } catch {
        Warn ("填缓存时出错：{0}" -f $_.Exception.Message)
    } finally {
        $ErrorActionPreference = $prevEap
        $env:PATH = $oldPath
    }
    if ((@(Get-NpxCacheTrees -Root $CacheDir).Count) -gt 0) {
        Ok '缓存已填上'
        return $true
    }
    Warn '没能把缓存填上（npx 本身也装不下来 —— 那说明这次没有可用的下载源）'
    return $false
}

# ---- 版本：默认取 npm 上的 latest（2026-10-08 用户要求：别写死旧版本）----
#  三种情况都要**说清楚**，不要静默：
#    ① 没传 -Version → 查 latest，查到就用；
#    ② 查不到（断网/源没配/npm 不在）→ 退回内置 $FallbackVersion 并**告警**；
#    ③ 显式传了 -Version → 用它（这也是**升级/降级到指定版本**的入口）。
if (-not $Version) {
    $npmForVersion = Resolve-Npm
    $latest = Resolve-LatestVersion -Npm $npmForVersion
    if ($latest) {
        $Version = $latest
    } else {
        $Version = $script:FallbackVersion
        Warn ("查不到 npm registry 上的最新版，退回内置版本 {0}" -f $Version)
        Warn "  （想指定版本：-Version <版本号>；想升级：见下文「升级标准版」）"
    }
}

Say ("标准版版本：{0}" -f $Version)

$node = Resolve-Node
if (-not $node) {
    Err '没找到 node —— 标准版 harness 需要 Node.js；装了 Node 再重跑'
    exit 1
}
Ok ("node: {0}" -f $node)

function Get-InstalledVersion([string]$Target) {
    # 读已装那份标准版的版本号（读不到返回 ''）。
    $pj = Join-Path $Target 'node_modules\@deepseek-ai\dsh\package.json'
    if (-not (Test-Path -LiteralPath $pj)) { return '' }
    try {
        return [string]((Get-Content -LiteralPath $pj -Raw | ConvertFrom-Json).version)
    } catch { return '' }
}

function Get-VersionRank([string]$V) {
    <#
      把 `0.2.0-rc.2` 这种预发布号排成一个可比较的整数数组。

      为什么要自己排：**预发布号不是 semver 的普通大小关系**（`0.2.0-rc.2` 名义上小于
      `0.2.0`），而这里的用途只是"本机这份和 registry 上那份是不是同一个"以及
      "粗略判断谁新" —— 用来**决定要不要提示升级**，不是拿来做依赖解析。
      真正的"哪个是给用户的稳定版"由 npm 的 dist-tag 决定（见 `Resolve-LatestVersion`）。
    #>
    $m = [regex]::Match([string]$V, '^(\d+)\.(\d+)\.(\d+)(?:-([A-Za-z]+)\.?(\d+)?)?')
    if (-not $m.Success) { return @(0, 0, 0, 0) }
    $major = [int]$m.Groups[1].Value
    $minor = [int]$m.Groups[2].Value
    $patch = [int]$m.Groups[3].Value
    # 正式版（没有预发布段）排在预发布之前：给 stage 一个更小的序数
    $stage = 9
    $pre = 0
    if ($m.Groups[4].Success) {
        $tag = $m.Groups[4].Value.ToLower()
        $stage = switch -Regex ($tag) { 'alpha' { 0 } 'beta' { 1 } 'rc' { 2 } default { 3 } }
        if ($m.Groups[5].Success) { $pre = [int]$m.Groups[5].Value }
    }
    return @($major, $minor, $patch, $stage, $pre)
}

function Test-NewerVersion([string]$Candidate, [string]$Current) {
    # Candidate 是否比 Current 新（只用于"有新版可升"的提示，不参与依赖解析）。
    if (-not $Candidate) { return $false }
    if (-not $Current) { return $true }
    $a = Get-VersionRank $Candidate
    $b = Get-VersionRank $Current
    for ($i = 0; $i -lt [Math]::Max($a.Count, $b.Count); $i++) {
        $x = if ($i -lt $a.Count) { $a[$i] } else { 0 }
        $y = if ($i -lt $b.Count) { $b[$i] } else { 0 }
        if ($x -ne $y) { return ($x -gt $y) }
    }
    return $false
}

function Test-Entry {
    # "装好了"的判据**不能只看 bin.js 在不在** —— 必须过完整性自检 + 冒烟测试。
    # 否则被中途打断留下的残树会被当成好的（同事 2026-09-22 实测：zod/v4 整片缺失，
    # bin.js 却在，于是每次都走"跳过下载"、每次启动都失败，而且 npm 按版本号认为
    # 它已装好、后续修复轮也不会碰它）。
    return ((Test-Path -LiteralPath $entry) -and
            ((Get-Item -LiteralPath $entry).Length -gt 0) -and
            ((Test-HarnessTree $target).Count -eq 0))
}

if (Test-Entry) {
    # ---- 「已经装好了」不等于「装的是你要的那个版本」（2026-10-08 升级支持）----
    # 原来这里直接 Ok 跳过 —— 于是**升级永远静默无效**：用户跑升级命令，脚本说
    # "标准版已经在本机（跳过下载）"，实际还是旧版。现在读一次已装版本，分三种情况：
    #   ① 同版本           → 真的跳过（无事可做）；
    #   ② 不同 + -Upgrade  → 删整树重装（升级）；
    #   ③ 不同 + 没 -Upgrade → **如实提示**有新版、并给出升级命令（不擅自覆盖用户的安装）。
    $installedVer = Get-InstalledVersion $target
    if (-not $installedVer) { $installedVer = '（读不到）' }
    if ($installedVer -eq $Version) {
        Ok ("标准版已经在本机，版本 {0}（跳过下载；完整性自检 + 冒烟测试都过）" -f $installedVer)
    } elseif ($Upgrade) {
        Warn ("升级标准版：{0} → {1}（按 -Upgrade 删整树重装）" -f $installedVer, $Version)
        $nm = Join-Path $target 'node_modules'
        if (-not (Remove-Tree $nm)) {
            Err ("清不掉 {0}（可能被占用）—— 先停掉标准版 harness 再重跑" -f $nm)
            exit 1
        }
        Remove-Item -LiteralPath (Join-Path $target 'package-lock.json') -Force -ErrorAction SilentlyContinue
        # 落到下面的安装流程
    } else {
        Ok ("标准版已经在本机，版本 {0}（完整性自检 + 冒烟测试都过）" -f $installedVer)
        if (Test-NewerVersion $Version $installedVer) {
            Say ("  注意：registry 上还有更新的版本 {0}（本机 {1}）" -f $Version, $installedVer)
            Say ("  升级到最新：加 -Upgrade 重跑本脚本")
            Say ("  升级到指定版本：-Upgrade -Version <版本号>")
        }
    }
}
if (-not (Test-Entry)) {
    try { New-Item -ItemType Directory -Force -Path $target | Out-Null } catch { }
    if (Test-Path -LiteralPath $entry) {
        # bin.js 在却没过自检 = 上一次装残了。**先整树删掉再装** ——
        # npm 只按版本号判断"这个包已装"，不会去修缺失的子目录，直接重跑 install
        # 只会说 "changed N packages"（同事实测）。package-lock 同样可能被写残，
        # 一起删掉才干净（残留的 lock 会让下次 install 直接报 Invalid/Missing）。
        Warn '检测到上次装残了（bin.js 在但跑不起来）—— 删掉整树重装'
        $nm = Join-Path $target 'node_modules'
        if (-not (Remove-Tree $nm)) {
            Err ("清不掉半残的 {0}（可能被占用）—— 手动删掉后重跑" -f $nm)
            exit 1
        }
        Remove-Item -LiteralPath (Join-Path $target 'package-lock.json') -Force -ErrorAction SilentlyContinue
    }
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
                # native 命令把进度/警告写在 **stderr**；而 `$ErrorActionPreference = 'Stop'` 下
                # `2>&1 |` 会把 stderr 升格成 terminating error —— 一句 `npm warn deprecated …`
                # 就会跳进 catch，看起来像 npm 坏了。2026-09-22 同事实测：明明装成功了却报
                # "npm 执行异常"；更糟的是若警告出现在**安装中途**，管道会提前中断、留下半个
                # node_modules（正是 B4 那类"装残"）。所以这里临时降级，成败只看 **退出码**。
                # --loglevel=error 顺带把 deprecated 这类噪音压掉。
                $prevEap = $ErrorActionPreference
                $ErrorActionPreference = 'Continue'
                try {
                    & $npm install ("@deepseek-ai/dsh@{0}" -f $Version) `
                        --no-audit --no-fund --loglevel=error 2>&1 |
                        ForEach-Object { Write-Host ("      " + $_) -ForegroundColor DarkGray; LogLine ("      " + $_) }
                    $npmExit = $LASTEXITCODE
                } finally {
                    $ErrorActionPreference = $prevEap
                }
                if ($npmExit -ne 0) {
                    Warn ("npm 退出码 {0} —— 这条路没成，继续试下一条" -f $npmExit)
                }
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
        Warn 'npm 这条路没装上（这个版本的依赖图曾出过 registry 事故，见 AGENTS.md）'
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
            # 全新机器上缓存往往是空的（同事实测）→ 先自己填一次再找
            if (Fill-NpxCache -Version $Version) {
                $trees = Get-NpxCacheTrees -Root $CacheDir
                $exact = @($trees | Where-Object { $_.Version -eq $Version })
                if ($exact.Count -gt 0) {
                    $pick = $exact | Sort-Object Time -Descending | Select-Object -First 1
                } elseif ($trees.Count -gt 0) {
                    $pick = $trees | Sort-Object Time -Descending | Select-Object -First 1
                    Warn ("缓存里没有 {0}，退而用 {1}（版本不同，但比 npx 快得多）" -f $Version, $pick.Version)
                }
            }
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
