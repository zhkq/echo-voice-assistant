# =====================================================================
# install.ps1 — ECHO 一键安装向导（同事版）
#
# 适用路线（两种交付包）
#   主包（D22，方向）：ECHO-main-<平台>-<版本>-<时间>.zip —— **仅代码 + 组件清单**，
#     不带运行时、不带模型。本脚本解压后按 manifest.json 准备 **runtime-core** 组件
#     （离线组件包优先，其次用 uv / py 在线创建），再拉起面板；其余组件由面板向导选装。
#   整包（legacy，pre-D22）：ECHO-internal-*.zip —— 自带 venv 与模型，解压即可用。
#     过渡期继续可用；两种包的取舍见 docs\REFACTOR-PLAN.md 的 D22/D23/D24。
#
# 场景 A（推荐，交付 zip）：把本脚本 + install.bat 与交付包放在同一文件夹，双击 install.bat：
#   自动发现 zip → 选择安装目录 → 解压 → （主包：准备 runtime-core）→ 建库 →
#   桌面快捷方式 → 开机自启（无窗口） → DSH 检测引导 → 完成。
#
# 场景 B（本机重装 / 目录已就位）：在 ECHO\scripts 里直接运行，检测到
#   venv 后跳过解压，只做初始化与收尾配置。
#
# 用法：
#   powershell -NoProfile -ExecutionPolicy Bypass -File install.ps1
#   可选：
#     -Zip <path>         指定交付包 zip（默认自动发现）
#     -DestDir <dir>      安装目录（默认 D:\ECHO，D 盘不存在则 C:\ECHO）
#     -ComponentDir <dir> 离线组件包所在目录（主包准备 runtime-core 时优先从这里找）
#     -Silent             无人值守：全默认值，不询问
#     -DryRun             只模拟：打印将执行的动作，不写任何系统位置
#     -SkipSetup          跳过初始化（整包：venv 修复建库；主包：等同 -SkipRuntime）
#     -SkipRuntime        不准备 runtime-core（主包专用，留给测试/CI）
#     -SkipStartupLnk     不装开机自启
#     -SkipDesktopLnk     不装桌面快捷方式
#     -SkipDshCheck       跳过 DSH Desktop 检测
#     -SkipStart          结束后不询问是否立即启动 ECHO
#
# 编码声明：本文件必须保持 UTF-8 带 BOM（WinPS 5.1 才能正确解析中文）。
# =====================================================================

param(
    [string]$Zip = '',
    [string]$DestDir = '',
    [string]$ComponentDir = '',
    [switch]$Silent,
    [switch]$DryRun,
    [switch]$SkipSetup,
    [switch]$SkipRuntime,
    [switch]$SkipStartupLnk,
    [switch]$SkipDesktopLnk,
    [switch]$SkipDshCheck,
    [switch]$SkipStart
)

$ErrorActionPreference = 'Stop'
$script:StepCount = 8
$script:LogPath = Join-Path $env:TEMP 'ECHO-install.log'
$script:ZipPath = ''
$script:ZipTop = 'ECHO'
$script:TargetDir = ''
$script:DestRoot = ''
$script:ExistingDir = ''
$script:IsMainPackage = $false
$script:DryRun = [bool]$DryRun
$script:Silent = [bool]$Silent

# ---------------------------------------------------------------- 输出
function Log {
    param([string]$Level, [string]$Msg)
    $line = "[{0}] [{1}] {2}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $Level, $Msg
    try { Add-Content -Path $script:LogPath -Value $line -Encoding UTF8 } catch { }
}
function Step {
    param([int]$N, [string]$Title)
    Write-Host ''
    Write-Host ("  [ {0}/{1} ]  {2}" -f $N, $script:StepCount, $Title) -ForegroundColor Cyan
    Log 'STEP' ("[{0}/{1}] {2}" -f $N, $script:StepCount, $Title)
}
function Ok   { param([string]$M) Write-Host ("    OK: {0}" -f $M) -ForegroundColor Green;  Log 'OK'   $M }
function Warn { param([string]$M) Write-Host ("    [!] {0}" -f $M) -ForegroundColor Yellow; Log 'WARN' $M }
function Err  { param([string]$M) Write-Host ("    [x] {0}" -f $M) -ForegroundColor Red;   Log 'ERR'  $M }
function Info { param([string]$M) Write-Host ("    *  {0}" -f $M) -ForegroundColor Gray }

# 询问（Silent 用默认，DryRun 只打印不读）
function Ask {
    param([string]$Question, [string]$Default = '')
    if ($script:Silent) { return $Default }
    if ($script:DryRun) { Info ("(模拟) 询问: {0}  默认: {1}" -f $Question, $Default); return $Default }
    $prompt = "    ? {0}" -f $Question
    if ($Default) { $prompt += "  [默认: {0}]" -f $Default }
    $prompt += ': '
    $ans = Read-Host $prompt
    if (-not $ans -and $Default) { return $Default }
    return $ans
}
function Ask-YesNo {
    param([string]$Question, [bool]$Default = $true)
    # Silent / DryRun：直接采用默认，不真交互
    if ($script:Silent -or $script:DryRun) {
        if ($script:DryRun) {
            $dlabel = if ($Default) { 'Y' } else { 'N' }
            Info ("(模拟) 询问: {0}  默认: {1}" -f $Question, $dlabel)
        }
        return $Default
    }
    $label = if ($Default) { 'Y/n' } else { 'y/N' }
    $ans = Ask $Question $label
    if ($ans -match '^(y|yes|是|1)$') { return $true }
    if ($ans -match '^(n|no|否|0)$')  { return $false }
    return $Default
}
function Ask-Continue {
    if ($script:Silent -or $script:DryRun) { return }
    Read-Host '    按回车继续...' | Out-Null
}

# ---------------------------------------------------------------- 工具
function Test-EnglishPath {
    param([string]$Path)
    return ($Path -notmatch '[^\x20-\x7E]')
}
function Get-RecommendedRoot {
    # 优先 D 盘；否则找剩余空间最大的固定盘
    $cands = @()
    if (Test-Path 'D:\') { $cands += 'D' }
    $cands += (Get-PSDrive -PSProvider FileSystem | Where-Object { $_.Used -ne $null } |
               Sort-Object Free -Descending | ForEach-Object { $_.Name })
    foreach ($letter in $cands) {
        $d = Get-PSDrive -Name $letter -ErrorAction SilentlyContinue
        if ($d -and $d.Free -ge 12GB) { return ($letter + ':\') }
    }
    return ''
}
function Invoke-Native {
    # 原生命令，吞掉 WinPS 5.1 的 NativeCommandError 包装，返回 @{code;out}
    param([string]$FilePath, [string[]]$ArgumentList)
    $prevEA = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $raw = (& $FilePath @ArgumentList 2>&1 | Out-String)
        $code = $LASTEXITCODE
        $keep = ($raw -split "`r?`n") | Where-Object {
            $_.Trim() -and $_ -notmatch '^(At |所在位置|\s*\+|.*CategoryInfo|.*FullyQualifiedErrorId)'
        }
        return @{ code = $code; out = (($keep -join ' ').Trim()) }
    } finally { $ErrorActionPreference = $prevEA }
}
function Find-DeliveryZip {
    $cands = @()
    $cands += Get-ChildItem $PSScriptRoot -Filter 'ECHO-*.zip' -ErrorAction SilentlyContinue
    $cands += Get-ChildItem (Join-Path $env:USERPROFILE 'Downloads') -Filter 'ECHO-*.zip' -ErrorAction SilentlyContinue
    $parent = Split-Path $PSScriptRoot -Parent
    if ($parent) {
        $cands += Get-ChildItem $parent -Filter 'ECHO-*.zip' -ErrorAction SilentlyContinue
    }
    if ($cands.Count -gt 0) {
        return ($cands | Sort-Object LastWriteTime -Descending | Select-Object -First 1).FullName
    }
    return ''
}

# ---------------------------------------------------------------- 各步骤
function Step01-Environment {
    Step 1 '环境检查（PowerShell / 磁盘）'
    if ($PSVersionTable.PSVersion.Major -lt 5) {
        Err '需要 Windows PowerShell 5.1+（Win10/11 自带）。'
        exit 1
    }
    Ok ("PowerShell {0}" -f $PSVersionTable.PSVersion.ToString())
    $script:DestRoot = Get-RecommendedRoot
    if (-not $script:DestRoot) {
        $biggest = Get-PSDrive -PSProvider FileSystem | Where-Object { $_.Used -ne $null } |
                   Sort-Object Free -Descending | Select-Object -First 1
        if ($biggest -and $biggest.Free -ge 4GB) {
            Warn ("没有盘剩余 >=12GB；最大 {0}: 剩余 {1:N1}GB" -f $biggest.Name, ($biggest.Free / 1GB))
            if (-not (Ask-YesNo '仍要继续吗？' $false)) { exit 1 }
            $script:DestRoot = $biggest.Name + ':\'
        } else {
            Err '未发现可用磁盘空间（<4GB），无法安装。'
            exit 1
        }
    }
    Ok ("推荐安装盘: {0}" -f $script:DestRoot)
}

function Step02-Source {
    Step 2 '确定 ECHO 来源'
    # 场景 B：脚本位于 ECHO\scripts\ 且已有 venv → 已存在目录
    # 但**显式给了 -Zip 时以交付包为准**：否则"把 install.bat + zip 放进已有 ECHO
    # 目录再双击"会被静默当成"重装已有目录"，你给的包根本没被解压（2026-09-20 实测）。
    $parentOfScripts = Split-Path $PSScriptRoot -Parent
    $hasVenv = Test-Path (Join-Path $parentOfScripts 'venv\Scripts\python.exe')
    if ($hasVenv -and -not $Zip) {
        $script:ExistingDir = $parentOfScripts
        Ok ("检测到已有 ECHO 目录: {0}" -f $script:ExistingDir)
        return
    }
    if ($hasVenv) {
        Info ("同目录已有 ECHO（{0}），但显式指定了 -Zip：按交付包安装" -f $parentOfScripts)
    }
    # 场景 A：定位交付 zip
    if ($Zip) {
        if (-not (Test-Path $Zip)) { Err "指定的 zip 不存在: {0}" -f $Zip; exit 1 }
        $script:ZipPath = $Zip
    } else {
        $found = Find-DeliveryZip
        if ($found) {
            $script:ZipPath = $found
        } else {
            if ($script:Silent) { Err 'Silent 模式下未自动找到交付包 zip。'; exit 1 }
            $u = Ask '未自动找到交付包，请手动输入 zip 完整路径' ''
            if (-not $u -or -not (Test-Path $u)) { Err '未提供有效 zip，退出。'; exit 1 }
            $script:ZipPath = $u
        }
    }
    $sizeGb = [math]::Round((Get-Item $script:ZipPath).Length / 1GB, 2)
    Ok ("交付包: {0}  ({1} GB)" -f $script:ZipPath, $sizeGb)
    # 探测 zip 顶层目录。
    # 不要用 `tar -tf` 的输出：Invoke-Native 会把多行压成一行（($keep -join ' ')），
    # 于是"取第一行 → 取第一个路径段"必然拿到排序最靠前的那一项；本包内 `.dsh/...`
    # 恰好排在最前，实测把默认安装目录算成了 D:\.dsh（2026-09-20）。
    # 改成读 zip 中央目录：快，且不依赖外部命令。
    $topDirs = @()
    $z = $null
    try {
        Add-Type -AssemblyName System.IO.Compression.FileSystem
        $z = [System.IO.Compression.ZipFile]::OpenRead($script:ZipPath)
        $topDirs = @($z.Entries |
            ForEach-Object { ($_.FullName -replace '\\', '/').Split('/')[0] } |
            Where-Object { $_ } |
            Sort-Object -Unique)
    } catch {
        Warn ("读取交付包目录失败: {0}" -f $_.Exception.Message)
    } finally { if ($z) { $z.Dispose() } }
    if ($topDirs -contains 'ECHO') {
        $script:ZipTop = 'ECHO'
        Info ("zip 顶层目录: ECHO（与它同级的还有 {0} 项）" -f ($topDirs.Count - 1))
    } elseif ($topDirs.Count -eq 1) {
        $script:ZipTop = $topDirs[0]
        Info ("zip 顶层目录: {0}" -f $script:ZipTop)
    } else {
        Info ("zip 顶层目录不唯一: {0}（按 ECHO 处理）" -f ($topDirs -join ', '))
    }
}

function Step03-Dest {
    Step 3 '确定安装目录'
    if ($script:ExistingDir) {
        $script:TargetDir = $script:ExistingDir
        Info ("安装目录（已有）: {0}" -f $script:TargetDir)
        return
    }
    if (-not $DestDir) {
        $def = Join-Path $script:DestRoot $script:ZipTop
        $script:TargetDir = Ask '安装到哪个目录？（建议纯英文路径）' $def
    } else {
        $script:TargetDir = $DestDir
    }
    if ($script:TargetDir -notmatch '^[A-Za-z]:[\\/]') {
        Err ("安装目录必须是盘符开头的绝对路径: {0}" -f $script:TargetDir)
        exit 1
    }
    $script:TargetDir = $script:TargetDir -replace '/', '\' -replace '\\+$', ''
    if ($script:TargetDir -match '[^\x00-\x7F]') {
        Warn '安装路径含中文/非 ASCII 字符！'
        Warn '  funasr/nagisa/dynet 无法读取中文路径，SenseVoice 转写会不可用。'
        if (-not (Ask-YesNo '强烈建议用纯英文路径，仍继续？' $false)) { exit 1 }
    }
    $driveLetter = $script:TargetDir.Substring(0, 1)
    $d = Get-PSDrive -Name $driveLetter -ErrorAction SilentlyContinue
    if ($d) {
        $need = 12GB
        if ($d.Free -lt $need) {
            Warn ("盘 {0}: 剩余 {1:N1}GB（建议 >=12GB）" -f $driveLetter, ($d.Free / 1GB))
            if (-not (Ask-YesNo '继续使用此盘？' $false)) { exit 1 }
        } else {
            Ok ("盘 {0}: 剩余 {1:N1}GB" -f $driveLetter, ($d.Free / 1GB))
        }
    }
    Info ("安装目录: {0}" -f $script:TargetDir)
}

function Step04-Extract {
    Step 4 '解压交付包'
    if ($script:ExistingDir) { Ok '已有目录模式，跳过解压'; return }
    if (Test-Path (Join-Path $script:TargetDir 'venv\Scripts\python.exe')) {
        Warn '目标目录已含 venv，视为已解压的 ECHO，跳过解压。'
        $script:ExistingDir = $script:TargetDir
        return
    }
    if (Test-Path $script:TargetDir) {
        $items = @(Get-ChildItem $script:TargetDir -Force -ErrorAction SilentlyContinue)
        if ($items.Count -gt 0) {
            if (-not (Ask-YesNo ("目标目录非空（{0} 项），继续解压？" -f $items.Count))) { exit 1 }
        }
    } else {
        New-Item -ItemType Directory -Path $script:TargetDir -Force | Out-Null
    }
    if ($script:DryRun) {
        Info ("(模拟) tar -xf {0} 到 {1}" -f $script:ZipPath, $script:TargetDir)
        $script:ExistingDir = $script:TargetDir
        return
    }
    # 解压到目标盘的临时目录再移入，避免 zip 顶层名与目标目录名不一致
    $parent = Split-Path $script:TargetDir -Parent
    $tmp = Join-Path $parent ('.echo-install-tmp-' + [guid]::NewGuid().ToString('N').Substring(0, 8))
    New-Item -ItemType Directory -Path $tmp -Force | Out-Null
    try {
        Info '正在解压（6 万个小文件，5–20 分钟，请耐心等待）...'
        $r = Invoke-Native 'tar' @('-xf', $script:ZipPath, '-C', $tmp)
        if ($r.code -ne 0) {
            Err ("解压失败: {0}" -f $r.out)
            exit 1
        }
        # 判断包结构：**唯一**顶层目录 = 包装型包（build-package 的 main / internal 档
        # 会裹一层 ECHO\）；否则是平铺包（public 档是平铺的）。
        # 注意 zip 根除了 ECHO\ 还有两个包元数据文件（BUILD-INFO.txt / SHA256SUMS.txt），
        # 所以不能要求"根目录零文件"——上一版按 1 目录 + 0 文件判定，真包被判成平铺包，
        # 结果整个树被装深一层（dest\ECHO\app\…，2026-09-20 真跑时发现）。
        # 旧代码更早的版本则无条件只搬"第一个目录"，平铺包会只剩 1 个目录、其余被删。
        $pkgMeta   = @('BUILD-INFO.txt', 'SHA256SUMS.txt')
        $rootDirs  = @(Get-ChildItem $tmp -Directory -Force -ErrorAction SilentlyContinue)
        $rootFiles = @(Get-ChildItem $tmp -File -Force -ErrorAction SilentlyContinue)
        $extraFiles = @($rootFiles | Where-Object { $pkgMeta -notcontains $_.Name })
        $wrapped = ($rootDirs.Count -eq 1 -and $extraFiles.Count -eq 0)
        $tItems = @(Get-ChildItem $script:TargetDir -Force -ErrorAction SilentlyContinue)
        if ($wrapped) {
            # 顶层目录放入目标：
            #   目标不存在 / 为空 → 整体改名移动（同盘 rename，瞬间完成）
            #   目标非空（用户已确认）→ 顶层内容逐项移入
            $topDir = $rootDirs[0]
            if ($tItems.Count -eq 0) {
                Remove-Item $script:TargetDir -Force -ErrorAction SilentlyContinue
                Move-Item $topDir.FullName $script:TargetDir -Force
            } else {
                Get-ChildItem $topDir.FullName -Force | Move-Item -Destination $script:TargetDir -Force
                Remove-Item $topDir.FullName -Force -Recurse -ErrorAction SilentlyContinue
            }
            # 包元数据跟着落到安装目录（留着对账用；它们描述的是这个包）
            if (-not (Test-Path $script:TargetDir)) {
                New-Item -ItemType Directory -Path $script:TargetDir -Force | Out-Null
            }
            foreach ($mf in $rootFiles) {
                if ($pkgMeta -contains $mf.Name) {
                    Move-Item $mf.FullName (Join-Path $script:TargetDir $mf.Name) -Force
                }
            }
        } else {
            Info ("平铺包：把 {0} 个顶层项逐一移入目标目录" -f ($rootDirs.Count + $rootFiles.Count))
            if (-not (Test-Path $script:TargetDir)) {
                New-Item -ItemType Directory -Path $script:TargetDir -Force | Out-Null
            }
            Get-ChildItem $tmp -Force | Move-Item -Destination $script:TargetDir -Force
        }
        Remove-Item $tmp -Recurse -Force -ErrorAction SilentlyContinue
    } catch {
        Remove-Item $tmp -Recurse -Force -ErrorAction SilentlyContinue
        throw
    }
    # 包的种类：整包自带 venv；主包只有代码 + manifest.json（D22），运行时随后由
    # runtime-core 组件补上 —— 以前这里只认 venv，主包会被误判成"包不完整"。
    if (Test-Path (Join-Path $script:TargetDir 'venv\Scripts\python.exe')) {
        Ok ("解压完成: {0}（整包：自带 venv）" -f $script:TargetDir)
    } elseif (Test-Path (Join-Path $script:TargetDir 'manifest.json')) {
        $script:IsMainPackage = $true
        Ok ("解压完成: {0}（主包：仅代码，运行时由 runtime-core 组件提供）" -f $script:TargetDir)
    } else {
        Err '解压完成但既没有 venv\Scripts\python.exe 也没有 manifest.json，包可能不完整。'
        exit 1
    }
    $script:ExistingDir = $script:TargetDir
}

# ------------------------------------------------------- runtime-core（D22/D23）
# 主包不含运行时，安装器得负责把 **runtime-core** 装上 —— 它是唯一必装组件，没有它
# 连面板都起不来。（已实测：把 torch/funasr/faster_whisper/sherpa_onnx/pyannote/
# edge_tts/modelscope 全部挡掉后，app.main/api/db 仍能导入 —— 引擎导入都是惰性的。）
# D24 要求：安装器里**没有任何选择界面**，能自动决定的就自动决定。
function Find-RuntimeCorePackage {
    $names = @('ECHO-组件-runtime-core-*.zip', 'ECHO-component-runtime-core-*.zip')
    $search = @()
    if ($ComponentDir) { $search += $ComponentDir }
    if ($script:ZipPath) { $search += (Split-Path $script:ZipPath -Parent) }
    $search += $PSScriptRoot
    foreach ($d in $search) {
        if (-not $d -or -not (Test-Path $d)) { continue }
        foreach ($n in $names) {
            $hit = Get-ChildItem $d -Filter $n -File -ErrorAction SilentlyContinue |
                   Sort-Object LastWriteTime -Descending | Select-Object -First 1
            if ($hit) { return $hit.FullName }
        }
        $dirHit = Join-Path $d 'runtime-core'
        if ((Get-RuntimeCorePython $dirHit)) { return $dirHit }
    }
    return ''
}

# runtime-core 有两种合法布局：可重定位 CPython（python.exe 在根）与 venv 式
# （Scripts\python.exe）。两种都要认 —— 否则"安装器建的"与"启动器找的"会对不上。
function Get-RuntimeCorePython([string]$dir) {
    foreach ($rel in @('python.exe', 'Scripts\python.exe')) {
        $p = Join-Path $dir $rel
        if (Test-Path $p) { return $p }
    }
    return ''
}

function Install-RuntimeCore {
    $rcDir = Join-Path $script:ExistingDir 'runtime-core'
    $rcPy = Get-RuntimeCorePython $rcDir
    if ($rcPy) { Ok ("runtime-core 已就绪: {0}" -f $rcPy); return }
    if ($script:DryRun) {
        Info ("(模拟) 准备 runtime-core → {0}（离线组件包优先，其次 uv / py 在线创建）" -f $rcDir)
        Info '(模拟) 装基础依赖 requirements-core.txt（约 100 MB）'
        return
    }
    # ① 离线组件包优先：无网场景的唯一出路
    $cand = Find-RuntimeCorePackage
    if ($cand) {
        Info ("从离线组件包准备 runtime-core: {0}" -f $cand)
        $tmp = ''
        if ($cand -like '*.zip') {
            $tmp = Join-Path $script:ExistingDir ('.rc-tmp-' + [guid]::NewGuid().ToString('N').Substring(0, 8))
            New-Item -ItemType Directory -Path $tmp -Force | Out-Null
            Invoke-Native 'tar' @('-xf', $cand, '-C', $tmp) | Out-Null
            $inner = @(Get-ChildItem $tmp -Directory -Force)
            $src = if ($inner.Count -eq 1) { $inner[0].FullName } else { $tmp }
        } else {
            $src = $cand
        }
        New-Item -ItemType Directory -Path $rcDir -Force | Out-Null
        Get-ChildItem $src -Force | Move-Item -Destination $rcDir -Force
        if ($tmp) { Remove-Item $tmp -Recurse -Force -ErrorAction SilentlyContinue }
        $rcPy = Get-RuntimeCorePython $rcDir
        if ($rcPy) { Ok ("runtime-core 已安装（离线包）: {0}" -f $rcPy); return }
        Err ("离线组件包里没有 python.exe: {0}" -f $cand)
        exit 1
    }
    # ② 在线创建：uv 优先，其次 py 启动器
    $uv = Get-Command uv -ErrorAction SilentlyContinue
    if ($uv) {
        Info '用 uv 创建 runtime-core（需要网络）...'
        & $uv.Source venv --python 3.11 $rcDir 2>&1 | ForEach-Object { Write-Host "      $_" -ForegroundColor Gray }
    } elseif (Get-Command py -ErrorAction SilentlyContinue) {
        Info '用 py -3.11 创建 runtime-core（需要网络）...'
        & py -3.11 -m venv $rcDir 2>&1 | ForEach-Object { Write-Host "      $_" -ForegroundColor Gray }
    } else {
        Err '没有 runtime-core，也没有 uv / py 可用 —— 主包自己跑不起来，必须先准备运行时。三选一：'
        Err '  1) 把 runtime-core 离线组件包放到交付包同目录（或用 -ComponentDir 指定）'
        Err '  2) 装 uv（https://docs.astral.sh/uv/）后重跑本向导'
        Err '  3) 装 Python 3.11.x（python.org，勾选 Add to PATH）后重跑'
        exit 1
    }
    $rcPy = Get-RuntimeCorePython $rcDir
    if (-not $rcPy) { Err ("创建 runtime-core 失败: {0}" -f $rcDir); exit 1 }
    $req = Join-Path $script:ExistingDir 'requirements-core.txt'
    if (Test-Path $req) {
        Info '安装 runtime-core 基础依赖（requirements-core.txt，约 100 MB）...'
        Invoke-Native $rcPy @('-m', 'pip', 'install', '--upgrade', 'pip') | Out-Null
        $r = Invoke-Native $rcPy @('-m', 'pip', 'install', '-r', $req)
        if ($r.code -ne 0) { Warn ("基础依赖安装返回非零（{0}）：{1}" -f $r.code, $r.out) }
    } else {
        Warn '包内没有 requirements-core.txt，跳过基础依赖安装'
    }
    Ok ("runtime-core 就绪: {0}" -f $rcPy)
}

function Step05-Init {
    if ($script:IsMainPackage) {
        Step 5 '运行时准备（runtime-core 组件，D23）'
        if ($SkipSetup -or $SkipRuntime) {
            Warn '已跳过 runtime-core 准备（-SkipSetup / -SkipRuntime）—— 此时面板起不来'
            return
        }
        Install-RuntimeCore
        return
    }
    Step 5 'venv 初始化与建库（new-machine-setup.ps1）'
    # 先看跳过标志，再查脚本：否则 -SkipSetup 遇到"包里没有 new-machine-setup.ps1"
    # 仍然会 exit 1，与它声明的用途（已初始化过的包，跳过）自相矛盾（2026-09-20 实测）。
    if ($SkipSetup) {
        Warn '已跳过初始化（-SkipSetup）'
        return
    }
    $setup = Join-Path $script:ExistingDir 'scripts\new-machine-setup.ps1'
    if (-not (Test-Path $setup)) {
        Err ("未找到初始化脚本: {0}" -f $setup)
        if ($script:DryRun) { return }
        exit 1
    }
    if ($script:DryRun) {
        Info "(模拟) 运行 new-machine-setup.ps1（修 pyvenv.cfg → 校验 → 补依赖 → 建库）"
        return
    }
    Info '运行 new-machine-setup.ps1，约需 2–10 分钟（首次补装依赖视网速而定）...'
    $prevEA = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        Push-Location $script:ExistingDir
        try {
            & powershell -NoProfile -ExecutionPolicy Bypass -File $setup
        } finally { Pop-Location }
        # new-machine-setup 内部错误时并非总是非零退出，用产物判断
        $dbOk = Test-Path (Join-Path $script:ExistingDir 'data\echo.db')
        $pyOk = Test-Path (Join-Path $script:ExistingDir 'venv\Scripts\python.exe')
        if ($dbOk -and $pyOk) { Ok '初始化完成（venv / 数据库就绪）' }
        else {
            Warn '初始化未完全成功（缺 data\echo.db 或 venv）'
            if (-not (Ask-YesNo '继续安装（稍后可手动补跑）？')) { exit 1 }
        }
    } finally { $ErrorActionPreference = $prevEA }
}

function Step06-SelfCheck {
    Step 6 '安装自检'
    $fail = 0
    # 解释器可能来自 runtime-core 组件（主包，D22）或自带的 venv（整包）
    $rcPy = Get-RuntimeCorePython (Join-Path $script:ExistingDir 'runtime-core')
    $venvPy = Join-Path $script:ExistingDir 'venv\Scripts\python.exe'
    if ($rcPy) { Ok ("runtime-core 就绪（D22 组件）: {0}" -f (Split-Path $rcPy -Leaf)) }
    elseif (Test-Path $venvPy) { Ok 'venv\Scripts\python.exe 存在（整包）' }
    else { Err '缺运行时（runtime-core 或 venv）'; $fail++ }
    $anyPy = if ($rcPy) { $rcPy } else { $venvPy }
    $pyw = $anyPy -replace 'python\.exe$', 'pythonw.exe'
    if (Test-Path $pyw) { Ok ("{0} 存在" -f (Split-Path $pyw -Leaf)) }
    else { Warn ("缺 {0}（启动将失败，建议重跑初始化）" -f (Split-Path $pyw -Leaf)) }
    if ($script:IsMainPackage) {
        $mani = Join-Path $script:ExistingDir 'manifest.json'
        if (Test-Path $mani) { Ok 'manifest.json 存在（主包）' } else { Warn '缺 manifest.json' }
    }
    $start = Join-Path $script:ExistingDir 'scripts\start.ps1'
    if (Test-Path $start) { Ok 'scripts\start.ps1 存在' } else { Err '缺 scripts\start.ps1'; $fail++ }
    $web = Join-Path $script:ExistingDir 'web\index.html'
    if (Test-Path $web) { Ok 'web 面板存在' } else { Warn '缺 web\index.html' }
    # 端口占用检查：端口由 data\echo-port.txt 决定（默认 8970）。
    # 写死 8970 时它既发现不了"真端口被占"，也认不出"已有实例在跑"（本机实际 18060）。
    $port = 8970
    $portFile = Join-Path $script:ExistingDir 'data\echo-port.txt'
    if (Test-Path $portFile) {
        $parsed = 0
        $raw = (Get-Content $portFile -ErrorAction SilentlyContinue |
                Where-Object { $_.Trim() } | Select-Object -First 1)
        if ($raw -and [int]::TryParse($raw.Trim(), [ref]$parsed) -and $parsed -gt 0) {
            $port = $parsed
            Info ("端口取自 data\echo-port.txt: {0}" -f $port)
        } else {
            Warn ("data\echo-port.txt 内容无法解析（{0}），按默认 {1} 检查" -f $raw, $port)
        }
    }
    $c = New-Object System.Net.Sockets.TcpClient
    try {
        $iar = $c.BeginConnect('127.0.0.1', $port, $null, $null)
        if ($iar.AsyncWaitHandle.WaitOne(800)) {
            $c.EndConnect($iar)
            Warn ("端口 {0} 已被占用（本机已有 ECHO 在运行，不影响安装）" -f $port)
        } else {
            Ok ("端口 {0} 空闲" -f $port)
        }
    } catch { } finally { $c.Dispose() }
    if ($fail -gt 0) {
        Err ("自检失败项: {0} 项，安装可能不可用" -f $fail)
        if ($script:DryRun) {
            Info '(模拟) 真实安装走到这里会中止（默认 N）——请先修好上面的失败项'
            return
        }
        if (-not (Ask-YesNo '自检未通过，仍要继续？' $false)) { exit 1 }
    }
}

function Step07-Shortcuts {
    Step 7 '创建快捷方式（桌面 + 开机自启）'
    # ---- 桌面快捷方式 ----
    $lnkInstaller = Join-Path $script:ExistingDir 'scripts\install-desktop-shortcut.ps1'
    if (-not $SkipDesktopLnk -and (Test-Path $lnkInstaller)) {
        if ($script:DryRun) {
            Info "(模拟) 运行 install-desktop-shortcut.ps1（创建桌面 ECHO 个人助理.lnk）"
        } else {
            & powershell -NoProfile -ExecutionPolicy Bypass -File $lnkInstaller 2>&1 | ForEach-Object { Write-Host "      $_" -ForegroundColor Gray }
            if ($LASTEXITCODE -eq 0) { Ok '桌面快捷方式已创建' } else { Warn '桌面快捷方式创建未成功' }
        }
    } else {
        if ($SkipDesktopLnk) { Warn '跳过桌面快捷方式（-SkipDesktopLnk）' }
        else { Warn ("未找到桌面快捷方式安装脚本，未创建: {0}" -f $lnkInstaller) }
    }
    # ---- 开机自启（vbs 隐藏方案：绕开组策略对 -WindowStyle Hidden 的拦截）----
    if ($SkipStartupLnk) {
        Warn '跳过开机自启'
        return
    }
    $root = $script:ExistingDir
    $vbsPath = Join-Path $root 'scripts\echo-startup.vbs'
    $startupFolder = [Environment]::GetFolderPath('Startup')
    $lnkPath = Join-Path $startupFolder 'ECHO startup.lnk'
    # vbs 内容：纯 ASCII 目标路径 + 非 wsh 变量名（本机 wsh 标识符与宿主冲突）
    $scriptPs1 = Join-Path $root 'scripts\startup.ps1'
    if (-not (Test-Path $scriptPs1)) {
        Err ("缺少启动器脚本 startup.ps1: {0}" -f $scriptPs1)
        if ($script:DryRun) { return }
        exit 1
    }
    # vbs 里**不写任何绝对路径**：由 vbs 自身推导所在目录（WScript.ScriptFullName），
    # 因此目录改名 / 整机搬迁后自启依然有效，也不会把用户名、路径等本机信息写进生成物。
    # 注意：不能用 cmd 的 %~dp0 ——它只在 cmd 把该文件当批处理执行时展开，
    # 内联 `cmd /c "...%~dp0..."` 会保持字面量（2026-09-14 实测）。
    # VBScript 内嵌引号必须双写转义。
    $vbsBody = (
        "' ECHO startup hidden launcher - runs startup.ps1 without any console window.`r`n" +
        "' Auto-generated by install.ps1. Self-locating: contains NO absolute path, so the`r`n" +
        "' folder may be renamed or moved without breaking autostart. Keep this file GBK/ANSI.`r`n" +
        "Set fso = CreateObject(`"Scripting.FileSystemObject`")`r`n" +
        "Set sh  = CreateObject(`"WScript.Shell`")`r`n" +
        "here = fso.GetParentFolderName(WScript.ScriptFullName)`r`n" +
        "sh.CurrentDirectory = here`r`n" +
        "target = fso.BuildPath(here, `"startup.ps1`")`r`n" +
        "sh.Run `"powershell -NoProfile -ExecutionPolicy Bypass -File `"`"`" & target & `"`"`"`", 0, False`r`n"
    )
    if ($script:DryRun) {
        Info ("(模拟) 生成 {0}" -f $vbsPath)
        Info ("(模拟) 创建自启 {0} → wscript.exe {1}" -f $lnkPath, $vbsPath)
        return
    }
    # 写入 vbs：固定用 GBK/ANSI 代码页（wscript 按系统 ANSI 读；纯 ASCII 内容双兼容）
    $ansi = [System.Text.Encoding]::GetEncoding([System.Globalization.CultureInfo]::CurrentCulture.TextInfo.ANSICodePage)
    [System.IO.File]::WriteAllText($vbsPath, $vbsBody, $ansi)
    Ok ("已生成: {0}" -f $vbsPath)
    # 快捷方式：wscript.exe + vbs（无窗口方案，绕开 powershell 隐藏被组策略拦截的问题）
    $ws = New-Object -ComObject WScript.Shell
    $lnk = $ws.CreateShortcut($lnkPath)
    $lnk.TargetPath = "$env:SystemRoot\System32\wscript.exe"
    $lnk.Arguments = "`"$vbsPath`""
    $lnk.WorkingDirectory = $root
    $lnk.Description = 'ECHO 开机自启（无窗口启动器）'
    $icon = Join-Path $root 'runtime-core\python.exe'
    if (-not (Test-Path $icon)) { $icon = Join-Path $root 'runtime-core\Scripts\python.exe' }
    if (-not (Test-Path $icon)) { $icon = Join-Path $root 'venv\Scripts\python.exe' }
    if (Test-Path $icon) { $lnk.IconLocation = "$icon,0" }
    else { $lnk.IconLocation = "$env:SystemRoot\System32\shell32.dll,220" }
    $lnk.Save()
    Ok ("已创建开机自启: {0}" -f $lnkPath)
}

function Step08-Finalize {
    Step 8 'DSH 检测与收尾'
    # ---- DSH Desktop 检测 ----
    if (-not $SkipDshCheck) {
        $dshOk = $false
        $dshProc = Get-Process -Name 'DSH Desktop' -ErrorAction SilentlyContinue
        if ($dshProc) { $dshOk = $true } else {
            $c = New-Object System.Net.Sockets.TcpClient
            try {
                $iar = $c.BeginConnect('127.0.0.1', 43120, $null, $null)
                if ($iar.AsyncWaitHandle.WaitOne(800)) { $c.EndConnect($iar); $dshOk = $true }
            } catch { } finally { $c.Dispose() }
        }
        if ($dshOk) {
            Ok 'DSH Desktop 已在运行（ECHO 执行引擎可接入）'
        } else {
            Warn '未检测到 DSH Desktop 运行。'
            if (-not $script:Silent) {
                Write-Host ''
                Write-Host '    下一步（重要）：'
                Write-Host '      1. 启动 DSH Desktop（桌面客户端）'
                Write-Host '      2. 设置 → 常规：「普通浏览器访问」打开（compatibility 模式）'
                Write-Host '      3. 面板 → 仪表盘 → DSH 执行引擎 → 检查'
                Write-Host ''
                if (-not $script:DryRun) { Ask-Continue }
            }
        }
    } else {
        Warn '已跳过 DSH 检测'
    }
    # ---- 面板配置提醒 ----
    Write-Host ''
    Write-Host '    ────────────────────────────────────────────────' -ForegroundColor DarkGray
    Write-Host '    安装完成！接下来：' -ForegroundColor Cyan
    Write-Host "      ECHO 目录 : $script:ExistingDir"
    if (-not $script:DryRun) {
        Write-Host "      启动 ECHO : powershell -NoProfile -ExecutionPolicy Bypass -File `"$script:ExistingDir\scripts\start.ps1`""
        Write-Host '      打开面板 : 见 data\echo-port.txt' 
        Write-Host '      建议立即设置：'
        Write-Host '        面板 → 设置 → 通用 → 计算设备：无 NVIDIA 显卡选 cpu'
        Write-Host '        面板 → 设置 → 会议 → 会议纪要工作区：可留空（默认 ECHO 根目录）'
        if ($script:IsMainPackage) {
            Write-Host '      主包不含模型与引擎：面板 → 设置 →「组件」按这台机器的环境选装'
        }
    }
    Write-Host '    ────────────────────────────────────────────────' -ForegroundColor DarkGray
    # ---- 询问是否启动 ----
    if (-not $SkipStart -and -not $script:DryRun) {
        if (Ask-YesNo '现在启动 ECHO 并打开控制面板？' $true) {
            $startScript = Join-Path $script:ExistingDir 'scripts\start.ps1'
            $launcher = Join-Path $script:ExistingDir 'scripts\launch-desktop.ps1'
            if (Test-Path $launcher) {
                Info '正在启动（launch-desktop.ps1：后台启动 ECHO + 打开面板）...'
                Start-Process powershell -ArgumentList @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', "`"$launcher`"") -WindowStyle Normal
            } elseif (Test-Path $startScript) {
                Info '正在后台启动 ECHO...'
                Start-Process powershell -ArgumentList @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', "`"$startScript`"", '-Background') -WindowStyle Normal
            }
        }
    }
    Log 'DONE' 'install finished'
    Write-Host ''
    Write-Host ("    安装日志: {0}" -f $script:LogPath) -ForegroundColor DarkGray
}

# ---------------------------------------------------------------- 入口
Log 'BEGIN' ("install.ps1 started, DryRun=$script:DryRun, Silent=$script:Silent")
Write-Host ''
Write-Host '  ============================================' -ForegroundColor Cyan
Write-Host '   ECHO 个人语音助理 - 一键安装向导' -ForegroundColor White
if ($script:DryRun) {
    Write-Host '   （模拟模式 -DryRun：只预览动作，不写系统）' -ForegroundColor Yellow
}
Write-Host '  ============================================' -ForegroundColor Cyan

try {
    Step01-Environment
    Step02-Source
    Step03-Dest
    Step04-Extract
    Step05-Init
    Step06-SelfCheck
    Step07-Shortcuts
    Step08-Finalize
} catch {
    Log 'FATAL' ("install failed: {0}`n{1}" -f $_.Exception.Message, $_.ScriptStackTrace)
    Write-Host ''
    Err ("安装中断: {0}" -f $_.Exception.Message)
    Write-Host ("    查看日志: {0}" -f $script:LogPath)
    exit 1
}
