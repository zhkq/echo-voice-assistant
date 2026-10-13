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
    # 1) PATH 上有没有（装了官方 node 的机器都走这条）
    foreach ($n in @('npm.cmd', 'npm.exe', 'npm')) {
        $c = Get-Command $n -ErrorAction SilentlyContinue
        if ($c) { return $c.Source }
    }
    # 2) **去 node 旁边找一遍**（2026-10-11 实测：新机器为什么装成旧版）
    #    现场：机器上的 node 是打包版（`...\.workbuddy\binaries\node\versions\<v>\node.exe`），
    #    而 `npm.cmd` / `npx.cmd` **就在 node.exe 旁边**，只是不在 PATH 里 ——
    #    `Get-Command npm` 找不到 -> 判定"没找到 npm" -> 跳过 npm 安装 ->
    #    只能从 npx 缓存复制**旧版本**（实测：想要 0.2.0-rc.2，装成 0.1.5-rc.2）。
    #    `Resolve-Node` 已经知道 node 在哪，顺着它找就一定找得到。
    $node = Resolve-Node
    if ($node) {
        $dir = Split-Path -Parent $node
        foreach ($n in @('npm.cmd', 'npm.exe', 'npm')) {
            $p = Join-Path $dir $n
            if (Test-Path -LiteralPath $p) { return $p }
        }
    }
    return ''
}

#: 与 Resolve-Npm 同一套路：**npx 也可能只躺在 node 旁边**（打包版 node 就是这种，
#  2026-10-11 实测：新机器上 npx 不在 PATH 里 -> 填 npx 缓存那一步直接失败 ->
#  只能装缓存里的旧版本）。填缓存是"拿到 latest 版本"的关键一步，所以这里必须找得到。
function Resolve-Npx {
    foreach ($n in @('npx.cmd', 'npx.exe', 'npx')) {
        $c = Get-Command $n -ErrorAction SilentlyContinue
        if ($c) { return $c.Source }
    }
    $node = Resolve-Node
    if ($node) {
        $dir = Split-Path -Parent $node
        foreach ($n in @('npx.cmd', 'npx.exe', 'npx')) {
            $p = Join-Path $dir $n
            if (Test-Path -LiteralPath $p) { return $p }
        }
    }
    return ''
}

#: 国内镜像。**为什么要在装之前就把它拿出来**（2026-10-13 同事实测，见 AGENTS.md）：
#  官方 registry 在装 koffi 这类带原生模块的包时，若 `@koromix/koffi-<平台>` 预构建包
#  没就位，koffi 的 install 脚本会**退化成现场编译**（调 cmake/VS 工具链，普通机器没有）
#  -> npm install 直接失败。镜像源上预构建包齐全、且国内不超时，所以让 npm 优先走它。
#  ⚠️ 顺序是"先官方、镜像只做比较/兜底"：镜像同步官方有延迟，官方源有更新时不该被镜像拖住。
$script:MirrorRegistry = 'https://registry.npmmirror.com'
$script:LiveRegistry = 'https://registry.npmjs.org'

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

function Compare-VersionNumber([string]$A, [string]$B) {
    #: 比较两个版本号字符串（A > B 返回 1，A < B 返回 -1，相等返回 0）。
    #  用途：两个 registry 各报一个 latest 时，挑**更新的那个**装。
    #  为什么不复用自己的 Test-NewerVersion：那个是为"提示升级"写的、只在预发布号上做
    #  粗略排序；这里要的是严格比较，所以按 semver 的正牌算法来（预发布段 < 同版本正式版）。
    $pa = [regex]::Match([string]$A, '^(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.\-]+))?')
    $pb = [regex]::Match([string]$B, '^(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.\-]+))?')
    if (-not $pa.Success) { return -1 }
    if (-not $pb.Success) { return 1 }
    for ($i = 1; $i -le 3; $i++) {
        $x = [int]$pa.Groups[$i].Value; $y = [int]$pb.Groups[$i].Value
        if ($x -ne $y) { return $(if ($x -gt $y) { 1 } else { -1 }) }
    }
    $ra = $pa.Groups[4].Value
    $rb = $pb.Groups[4].Value
    if (-not $ra -and -not $rb) { return 0 }
    if (-not $ra) { return 1 }        # 无预发布段 = 正式版，比预发布版新
    if (-not $rb) { return -1 }
    $ida = $ra.Split('.')
    $idb = $rb.Split('.')
    for ($i = 0; $i -lt [Math]::Max($ida.Count, $idb.Count); $i++) {
        if ($i -ge $ida.Count) { return -1 }
        if ($i -ge $idb.Count) { return 1 }
        $sa = $ida[$i]; $sb = $idb[$i]
        $na = 0; $nb = 0
        $aNum = [int]::TryParse($sa, [ref]$na)
        $bNum = [int]::TryParse($sb, [ref]$nb)
        if ($aNum -and $bNum) { if ($na -ne $nb) { return $(if ($na -gt $nb) { 1 } else { -1 }) } continue }
        if ($aNum -ne $bNum) { return $(if ($aNum) { -1 } else { 1 }) }   # 数字标识符 < 字母标识符
        $c = [string]::CompareOrdinal($sa, $sb)
        if ($c -ne 0) { return $(if ($c -gt 0) { 1 } else { -1 }) }
    }
    return 0
}

function Resolve-InstallSource([string]$Npm, [string]$ForVersion, [switch]$Quiet) {
    <#
      决定这次 `npm install` 走哪个 registry，返回 @{ Version=...; Registry=... }。

      背景（2026-10-13 同事新机器实测，见 AGENTS.md）：npm install @0.2.0-rc.2 在官方
      registry 上装 koffi 时，它的 install 脚本试图**现场编译**原生模块，而子进程里
      `node` 不在 PATH（cmd 报 "'node' 不是内部或外部命令"）-> 整次安装失败 ->
      脚本回退到 npx 缓存复制。修 PATH 是治本，但**预构建包是不是就位**决定"要不要编译"，
      所以顺手把源也选对：官方源和镜像各查一次 latest，**谁新用谁**；官方查不到（超时/
      被墙）就用镜像；显式传了 -Version 时，只要该版本在镜像上有，就用镜像装（国内更快）。

      返回的 Registry 为 '' 表示"不传 --registry，按本机 npm 自己的配置来"（用户在
      内网源/自定义源上时，尊重他本机的配置，别硬塞公网源）。
    #>
    if (-not $Npm) { return [pscustomobject]@{ Version = ''; Registry = '' } }

    $live = ''
    $mirror = ''
    try {
        $raw = & $Npm 'view' '@deepseek-ai/dsh' 'dist-tags.latest' ("--registry={0}" -f $script:LiveRegistry) 2>$null
        $live = ([string]($raw | Select-Object -First 1)).Trim().Trim('"')
        if ($live -notmatch '^\d+\.\d+\.\d+') { $live = '' }
    } catch { $live = '' }
    try {
        $raw = & $Npm 'view' '@deepseek-ai/dsh' 'dist-tags.latest' ("--registry={0}" -f $script:MirrorRegistry) 2>$null
        $mirror = ([string]($raw | Select-Object -First 1)).Trim().Trim('"')
        if ($mirror -notmatch '^\d+\.\d+\.\d+') { $mirror = '' }
    } catch { $mirror = '' }

    $version = ''
    $registry = ''
    if ($ForVersion) {
        $version = $ForVersion
        if ($mirror) { $registry = $script:MirrorRegistry }
    } elseif ($live -and $mirror) {
        if ((Compare-VersionNumber $mirror $live) -gt 0) { $version = $mirror; $registry = $script:MirrorRegistry }
        else { $version = $live; $registry = $script:LiveRegistry }
    } elseif ($live) {
        $version = $live; $registry = $script:LiveRegistry
    } elseif ($mirror) {
        $version = $mirror; $registry = $script:MirrorRegistry
    }
    if (-not $Quiet -and $registry) {
        Say ("安装源：{0}（官方 latest={1}，镜像 latest={2}）" -f $registry, $(if ($live) { $live } else { '查不到' }), $(if ($mirror) { $mirror } else { '查不到' }))
    }
    return [pscustomobject]@{ Version = $version; Registry = $registry }
}

function Select-CachedTree($Trees, [string]$Version, [switch]$AllowMismatch) {
    <#
      从 npx 缓存里的若干棵树里挑一棵。
      能这样选就不会乱选：**同版本优先**，其次（只在允许时）拿最新的不同版本。
    #>
    $trees = @($Trees)
    if ($trees.Count -eq 0) { return $null }
    $exact = @($trees | Where-Object { $_.Version -eq $Version })
    if ($exact.Count -gt 0) {
        return ($exact | Sort-Object Time -Descending | Select-Object -First 1)
    }
    if (-not $AllowMismatch) { return $null }
    $pick = $trees | Sort-Object Time -Descending | Select-Object -First 1
    Warn ("缓存里没有 {0}，退而用 {1}（版本不同，但比 npx 快得多）" -f $Version, $pick.Version)
    return $pick
}

function Resolve-NativeCheck([string]$NodeBin) {
    <#
      返回 node-addon-check.js 的路径；没有就返回 ''（调用方跳过这道检查）。

      每一句都是为了不误伤：
        * 只在**本 skill 的 scripts\** 找：预置的脚本随 skill 走，找不到就说明这份安装
          是旧版 skill 铺的，跳过检查比瞎报错强（向后兼容，不破坏老机器的安装）；
        * $PSScriptRoot 可能为空（脚本被 dot-source 或按字符串跑），所以先判空再查。
    #>
    if (-not $PSScriptRoot) { return '' }
    $candidate = Join-Path $PSScriptRoot 'node-addon-check.js'
    if (Test-Path -LiteralPath $candidate) { return $candidate }
    return ''
}

function Get-CachedTree([string]$Version, [switch]$AllowMismatch) {
    # 「在缓存里找一棵可用的树」：先翻现有缓存，翻不到就主动填一次再翻。
    $trees = Get-NpxCacheTrees -Root $CacheDir
    $pick = Select-CachedTree $trees -Version $Version -AllowMismatch:$AllowMismatch
    if ($pick) { return $pick }
    if (Fill-NpxCache -Version $Version) {
        $trees = Get-NpxCacheTrees -Root $CacheDir
        $pick = Select-CachedTree $trees -Version $Version -AllowMismatch:$AllowMismatch
    }
    return $pick
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
    if (-not $npx) { $npx = Resolve-Npx }   # 2026-10-11: 先在 node 旁边找一遍
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

# ---- 版本 + 源：默认取 npm 上的 latest（2026-10-08 用户要求：别写死旧版本）----
#  三种情况都要**说清楚**，不要静默：
#    ① 没传 -Version → 官方/镜像各查一次 latest，**谁新用谁**；
#    ② 都查不到（断网/源没配/npm 不在）→ 退回内置 $FallbackVersion 并**告警**；
#    ③ 显式传了 -Version → 用它（这也是**升级/降级到指定版本**的入口）。
#  $UseRegistry 单独记"这次 npm install 要显式指定哪个源"：
#    为空 = 不传 --registry，按本机 npm 自己的配置走（尊重内网/自定义源）。
$latestInfo = Resolve-InstallSource -Npm (Resolve-Npm) -ForVersion $Version
$UseRegistry = $latestInfo.Registry
if (-not $Version -and $latestInfo.Version) {
    $Version = $latestInfo.Version
} elseif (-not $Version) {
    $Version = $script:FallbackVersion
    Warn ("查不到 npm registry 上的最新版，退回内置版本 {0}" -f $Version)
    Warn "  （想指定版本：-Version <版本号>；想升级：见下文「升级标准版」）"
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
      真正的"哪个是给用户的稳定版"由 npm 的 dist-tag 决定（见 `Resolve-InstallSource`）。
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
            # **本次修复的核心一行**（2026-10-13 同事新机器实测，见 AGENTS.md）：
            #   npm 的生命周期脚本是交给 `cmd.exe /d /s /c node ...` 执行的，而打包版 node 常常
            #   **不在 PATH 里**（`...\.workbuddy\binaries\node\versions\<v>\node.exe`）。于是
            #   koffi 的 install 脚本（`node ./cnoke.cjs ...`）一上来就 9009：
            #     'node' 不是内部或外部命令，也不是可运行的程序或批处理文件
            #   -> 原生模块没法就位 -> 整个 npm install 失败 -> 白退到 npx 缓存那条路。
            #   **必须改的是"传给子进程的环境"，不是本进程的**：即使宿主把环境重置过，
            #   只要 spawn 时带上这份 PATH，npm 起的 cmd 就一定找得到 node。
            #   同 Fill-NpxCache 里的做法（那里早就这么修了，npm 这处当初漏了）。
            $old = $env:CODEBUDDY_SAFE_DELETE_ENABLED
            $env:CODEBUDDY_SAFE_DELETE_ENABLED = '0'
            $oldPath = $env:PATH
            $nodeDir = Split-Path -Parent $node
            if ($nodeDir) { $env:PATH = $nodeDir + ';' + $env:PATH }
            # 源：优先镜像（预构建包齐全、不超时）。不传时按本机 npm 配置走。
            $npmArgs = @('install', ("@deepseek-ai/dsh@{0}" -f $Version), '--no-audit', '--no-fund', '--loglevel=error', '--save-exact')
            if ($UseRegistry) { $npmArgs += ("--registry={0}" -f $UseRegistry); Say ("  源：{0}" -f $UseRegistry) }
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
                    & $npm @npmArgs 2>&1 |
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
                $env:PATH = $oldPath
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
        # 版本闸门（2026-10-13 同事新机器实测）：用户要的是 latest，而缓存里**恰好**有别的
        # 版本时，原来的写法会静默拿最新的那棵不同版本的树装上 —— 于是"装最新版"变成"装缓存里
        # 随便哪个版本"，用户完全不知道。现在：只有缓存里**有这个版本**才自动用；
        # 没有就明确报错、并告诉用户出路（-FromCache 是"我知道版本可能不同"的显式授权）。
        $allowMismatch = [bool]$FromCache
        $pick = Get-CachedTree -Version $Version -AllowMismatch:$allowMismatch
        if (-not $pick) {
            $trees = @(Get-NpxCacheTrees -Root $CacheDir)
            Err ("npx 缓存里没有 {0}（缓存里有：{1}）" -f $Version, $(if ($trees.Count -gt 0) { (($trees | ForEach-Object { $_.Version }) -join '、') } else { '空' }))
            Say '  出路一：确认网络/源之后重跑本脚本（npm 那条路是首选）'
            Say '  出路二：就是要用缓存里那个别的版本 -> 加 -FromCache 重跑（会明确告警版本不同）'
            Say '  出路三：先 `npx -y @deepseek-ai/dsh web` 跑一次把缓存填上再重跑本脚本'
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

# ---- 原生模块闸门（2026-10-13 新增，见 AGENTS.md）----
# 为什么光有上面那道冒烟测试还不够：`node bin.js --version` 只走 CLI 的入口链，**碰不到**
# koffi —— 而 koffi 是 dsh-fs / dsh-subprocess-local / dsh-session-persistence-jsonl /
# dsh-sandbox-windows-acl 的原生依赖，正是**跑 pwsh、跑子进程**的必经之路。
# 场景：npm 装到一半在 koffi 上失败（cmake 缺、杀软拦、PATH 找不到 node），而 npx 缓存那条路
# 恰好补上一棵能跑的树 -> 旧脚本照样打印"已就绪"。必须**真 require 一次**才算数。
$nativeCheck = Resolve-NativeCheck $node
if ($nativeCheck) {
    $prevEap = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $ncOut = & $node $nativeCheck $target 2>&1
        $ncCode = $LASTEXITCODE
    } catch {
        $ncOut = @($_.Exception.Message); $ncCode = -1
    } finally { $ErrorActionPreference = $prevEap }
    if ($ncCode -ne 0) {
        Err '原生模块自检没过（koffi 之类加载不了 —— 典型原因：npm 装到一半在它上面失败）'
        foreach ($line in @($ncOut | Select-Object -Last 5)) { Say ("  " + $line) }
        Say ("  修法：删掉 {0} 后重跑本脚本（脚本会重走 npm install）" -f (Join-Path $target 'node_modules'))
        exit 1
    }
    Ok ("原生模块自检通过：{0}" -f (($ncOut | Select-Object -Last 1) -join ''))
} else {
    # 老机器上这份 skill 可能还没有 node-addon-check.js：跳过而不是报错，别把旧安装搞坏。
    LogLine '跳过原生模块自检（本 skill 里没有 node-addon-check.js）'
}

$cmd = '"{0}" "{1}" web' -f $node, $entry
Ok '标准版已就绪（本地永久安装，冷启动约 10 秒）'
Say ("  启动命令：{0}" -f $cmd)
if ($CommandFile) {
    try { Set-Content -LiteralPath $CommandFile -Value $cmd -Encoding UTF8 -NoNewline } catch { }
}
Write-Output ("HARNESS_COMMAND=" + $cmd)
exit 0
