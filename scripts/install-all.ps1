# =====================================================================
# install-all.ps1 - ECHO 一键安装【快路】：一条命令走完，不绕 agent
#
# 为什么要有它
# ------------
# 原来的安装是「把 echo-install 技能目录交给 AI agent，agent 照 SKILL.md 逐步做」：
# 每一步都要联网（pip 装依赖、下模型），而且每一步都要 agent 复述/确认 ——
# 慢就慢在这两处（等网络 + 等人确认），不是慢在机器上。
#
# 这个脚本把 SKILL.md 的步骤固化成一条命令：
#     就位（解包/复制） → 建运行时 → 装核心依赖 → 装组件（依赖/模型/设置/自检） → 打印面板地址
#
# 它**不替代**技能那条路：技能继续独立可用（agent 路径原样保留，见 SKILL.md）。
# 两条路装出来的东西**一样** —— 组件那一步两边都调同一个
# `.dsh/skills/echo-install/scripts/echo-install-components.ps1`（不重复实现一份）。
#
# 用法
# ----
#   # 在线快路（默认档：runtime-core + 依赖；客户端无本地 STT，转写走能力后端）
#   powershell -NoProfile -ExecutionPolicy Bypass -File <kit>\echo-core\scripts\install-all.ps1 -Yes
#
#   # 离线包（bundle\wheels + bundle\models 随包，全程不联网）
#   powershell -NoProfile -ExecutionPolicy Bypass -File <kit>\echo-core\scripts\install-all.ps1 `
#       -Offline -Yes -Agent none
#
#   # 2026-10-31 起只有一个客户端档：runtime-core + 依赖（无本地 STT 模型），
#   # 指令/回顾/会议转写全部走能力后端（Qwen3-ASR）。
#   ... -Agent harness -Root D:\ECHO
#
# 同事那一份 kit 根上还有个 **双击入口**（`装我.cmd`，由 build_kit.py 从
# delivery\kit-install.cmd 放进去）：它只问一句"装到哪个目录"，然后调本脚本
# （`-Profile minimal -Agent harness -Yes`，发现同目录有 bundle\ 就自动加 -Offline）。
#
# 参数
# ----
#   -Profile minimal|main    **已废弃**：客户端只剩一个默认档（没有本地 STT 引擎/模型），
#                            两个取值行为相同；保留参数只为兼容老脚本。
#   -Root <安装根>           数据/模型/运行时都在它下面，代码进 <根>\echo-core。
#                            默认 D:\ECHO（没有 D 盘就 C:\ECHO）
#   -Agent none|harness|dsh  智能体。**离线默认 none**：离线包里不带 DSH/harness
#                            （@deepseek-ai/dsh 是私有 npm 包，许可上不随包分发）
#   -HarnessSource auto|desktop|npm
#                            harness 从哪来（默认 auto）：
#                            auto=先用已装的官方 DeepSeek Harness 桌面端，没有就联网
#                            下载官方安装包静默安装，再失败才回退本地 npm；
#                            desktop=没有就下载官方桌面端；npm=只走旧的 node/npm 本地安装。
#   -DesktopInstallDir <目录> 官方桌面端安装目录（留空=官方默认位置）
#   -DshDir <目录>           已有 DSH 的安装目录（含 node_modules\@deepseek-ai\dsh\lib\bin.js
#                            的那一层）。给了就不装 npm 包，只探测 + 写 harnessCommand。
#   -AskDsh                 允许交互问"已有 DSH 吗"；用户选有则循环输入目录并探测。
#                            kit 的全新安装会传；无人值守/agent 默认不传（行为不变）。
#   -Offline                 离线模式：依赖只从 bundle\wheels 装（pip 带
#                            --no-index --find-links）；客户端没有模型，bundle\models 可以不存在
#   -Yes                     无人值守：不提问、全默认
#   -KitRoot <目录>          资料夹根（里面有 echo-core\（老 kit 是 ECHO\）、echo-install\、
#                            bundle\）；默认按本脚本位置推（<kit>\echo-core\scripts\install-all.ps1）
#   -BundleDir <目录>        离线载荷目录（默认 <KitRoot>\bundle）
#   -Engines <列表>          已废弃：客户端不再装本地 STT 引擎（传了也只警告、不生效）
#   -Wake / -NoWake          已废弃：语音唤醒取消（传了也只警告、不生效）
#   -Diarize                 已废弃：说话人分离在能力后端做（传了也只警告、不生效）
#   -PipIndex <url>          在线模式换国内 pip 镜像
#   -NotesDir <目录>         笔记库（填了会开纪要归档）
#   -NoShortcuts             不建桌面快捷方式与开机自启（测试机常用）
#   -SkipStart               只装不启动（默认会拉起来并打印面板地址）
#
# 退出码
# ------
#   0  全部就绪；  1  安装中断（日志里能看到是哪一步）
#
# 编码：含中文，**必须 UTF-8 带 BOM**（WinPS 5.1 否则按 ANSI 解析，中文乱码甚至
#       静默解析失败）。tests/test_script_encoding.py 盯着这一条。
# =====================================================================

param(
    [ValidateSet('minimal', 'main')][string]$Profile = 'minimal',
    [Alias('DestDir')][string]$Root = '',
    [ValidateSet('none', 'harness', 'dsh')][string]$Agent = '',
    [ValidateSet('auto', 'desktop', 'npm')][string]$HarnessSource = 'auto',
    [string]$DesktopInstallDir = '',
    [string]$DshDir = '',
    [switch]$AskDsh,
    [switch]$Offline,
    [switch]$Yes,
    [string]$KitRoot = '',
    [string]$BundleDir = '',
    [string[]]$Engines = @(),
    [switch]$Wake,
    [switch]$NoWake,
    [switch]$Diarize,
    [string]$PipIndex = '',
    [string]$NotesDir = '',
    [switch]$NoShortcuts,
    [switch]$SkipStart,
    # ---- 后端（会议转写那台 GPU 服务）怎么来：用户 2026-10-01 要求安装流程后面多问这一句
    [ValidateSet('ask', 'pair', 'local', 'skip')][string]$Backend = 'ask',
    [string]$BackendPair = '',
    [string]$BackendDir = ''
)

$ErrorActionPreference = 'Stop'
$script:T0 = Get-Date
$script:Phases = New-Object System.Collections.Generic.List[object]
$script:LogPath = Join-Path $env:TEMP 'ECHO-install-all.log'
$script:KitRoot = ''
$script:Tree = ''           # 主程序目录（含 manifest.json / app\main.py）
$script:Bundle = ''
$script:SkillDir = ''
$script:TargetRoot = ''     # 安装根
$script:CoreDir = ''        # 代码目录
$script:StepNo = 0
$script:TotalSteps = 7
#: 这次是"升级"吗（已有安装）。由 `Invoke-InstallModeProbe` 在入口处判定后写这里。
#: 升级只更新**核心代码**：数据/设置/模型权重/后端与手表配置/智能体配置一律不动
#: （用户 2026-10-23 口径："升级主要是核心代码更新……dsh 可以询问是否升级，
#:  但是 agent 相关的配置别碰"）。
$script:IsUpgrade = $false
#: 升级前后的版本（只用于收尾那一句话；不参与任何判断）
$script:UpgradeFromVersion = ''
$script:UpgradeToVersion = ''
#: 升级前的数据库备份路径（收尾里如实告诉用户）
$script:UpgradeDbBackup = ''
#: 后端那一步的结论（`Invoke-BackendStep` 写，`Show-Result` 读）—— 收尾摘要里如实带一行。
$script:BackendSummary = ''

# ---------------------------------------------------------------- 输出
function Log {
    param([string]$Level, [string]$Msg)
    $line = "[{0}] [{1}] {2}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $Level, $Msg
    try { Add-Content -LiteralPath $script:LogPath -Value $line -Encoding UTF8 } catch { }
}
function Step {
    param([string]$Title)
    $script:StepNo++
    $script:PhaseStart = Get-Date
    Write-Host ''
    Write-Host ("  [ {0}/{1} ]  {2}" -f $script:StepNo, $script:TotalSteps, $Title) -ForegroundColor Cyan
    Log 'STEP' ("[{0}/{1}] {2}" -f $script:StepNo, $script:TotalSteps, $Title)
}
function EndStep {
    param([string]$Name)
    $sec = [Math]::Round(((Get-Date) - $script:PhaseStart).TotalSeconds, 1)
    $script:Phases.Add([pscustomobject]@{ Name = $Name; Seconds = $sec })
    Write-Host ("          （{0} 秒）" -f $sec) -ForegroundColor DarkGray
}
function Ok   { param([string]$M) Write-Host ("    OK: {0}" -f $M) -ForegroundColor Green;  Log 'OK'   $M }
function Warn { param([string]$M) Write-Host ("    [!] {0}" -f $M) -ForegroundColor Yellow; Log 'WARN' $M }
function Err  { param([string]$M) Write-Host ("    [x] {0}" -f $M) -ForegroundColor Red;    Log 'ERR'  $M }
function Info { param([string]$M) Write-Host ("    *  {0}" -f $M) -ForegroundColor Gray }

function Invoke-Native {
    # 原生命令：吞掉 WinPS 5.1 的 NativeCommandError 包装，返回 @{code;out}。
    # （脚本顶部是 EAP=Stop，直接 `& exe 2>&1` 会把 stderr 变成**终止性错误** ——
    #   uv / npm / pip 的进度条都写 stderr，install.ps1 就是在这上面栽过。）
    param([string]$FilePath, [string[]]$ArgumentList)
    $prevEA = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $raw = (& $FilePath @ArgumentList 2>&1 | Out-String)
        $code = $LASTEXITCODE
        $keep = ($raw -split "`r?`n") | Where-Object {
            $_.Trim() -and $_ -notmatch '^(At |所在位置|\s*\+|.*CategoryInfo|.*FullyQualifiedErrorId)'
        }
        return @{ code = $code; out = (($keep -join "`n").Trim()) }
    } finally { $ErrorActionPreference = $prevEA }
}

# ---------------------------------------------------------------- 定位：资料夹 / 安装根
function Resolve-KitLayout {
    Step '定位资料夹与安装根'
    $here = $PSScriptRoot
    if (-not $here) { try { $here = Split-Path -Parent $PSCommandPath } catch { $here = (Get-Location).Path } }

    if ($KitRoot) {
        $script:KitRoot = (Resolve-Path -LiteralPath $KitRoot).Path
        # 代码目录名两个都认：3.0 起 kit 里叫 echo-core（与 <安装根>\echo-core 同名），
        # 老 kit 叫 ECHO（主包 zip 自己裹的那一层）。判据只有这一处。
        foreach ($name in @('echo-core', 'ECHO')) {
            $cand = Join-Path $script:KitRoot $name
            if (Test-Path (Join-Path $cand 'app\main.py')) { $script:Tree = $cand; break }
        }
        if (-not $script:Tree) {
            # 兼容 -KitRoot 直接指到主程序目录（有人把 -KitRoot 当成 -Tree 用）
            if (Test-Path (Join-Path $script:KitRoot 'app\main.py')) {
                $script:Tree = $script:KitRoot
                $script:KitRoot = Split-Path $script:Tree -Parent
            } else {
                Err ("-KitRoot 里既没有 echo-core\ / ECHO\ 也没有 app\main.py：{0}" -f $KitRoot); exit 1
            }
        }
    } else {
        # 本脚本在 <kit>\echo-core\scripts\（3.0）或 <kit>\ECHO\scripts\（老包）里
        # （主包白名单带 scripts/），父目录就是主程序目录
        $cand = Split-Path $here -Parent                     # <kit>\echo-core 或仓库根
        if (Test-Path (Join-Path $cand 'app\main.py')) {
            $script:Tree = $cand
            $up = Split-Path $cand -Parent
            $leaf = Split-Path $cand -Leaf
            if ($leaf -ieq 'echo-core' -or $leaf -ieq 'ECHO') { $script:KitRoot = $up }
        }
    }
    if (-not $script:Tree) {
        $cand = Split-Path $here -Parent
        Err ("没找到 ECHO 主程序（找过 {0}\echo-core\app\main.py 与 {0}\ECHO\app\main.py）。用 -KitRoot <资料夹> 指定。" -f $cand)
        exit 1
    }
    Ok ("主程序: {0}" -f $script:Tree)
    if ($script:KitRoot) { Info ("资料夹: {0}" -f $script:KitRoot) }

    # 技能目录：离线包里在 <kit>\echo-install\；本机仓库里在 <repo>\.dsh\skills\echo-install\
    if ($script:KitRoot) {
        $s = Join-Path $script:KitRoot 'echo-install'
        if (Test-Path (Join-Path $s 'scripts\echo-install-components.ps1')) { $script:SkillDir = $s }
    }
    if (-not $script:SkillDir) {
        $s = Join-Path (Split-Path $script:Tree -Parent) '.dsh\skills\echo-install'
        if (Test-Path (Join-Path $s 'scripts\echo-install-components.ps1')) { $script:SkillDir = $s }
    }
    if ($script:SkillDir) { Ok ("安装技能: {0}" -f $script:SkillDir) }
    else { Warn '没找到 echo-install 技能目录 —— 组件那一步没法跑（技能是它的唯一实现）' }

    # 离线载荷
    if ($BundleDir) {
        if (Test-Path $BundleDir) { $script:Bundle = (Resolve-Path -LiteralPath $BundleDir).Path }
        else { Warn ("-BundleDir 不存在：{0}" -f $BundleDir) }
    } elseif ($script:KitRoot) {
        $b = Join-Path $script:KitRoot 'bundle'
        if (Test-Path $b) { $script:Bundle = (Resolve-Path -LiteralPath $b).Path }
    }
    if ($script:Bundle) { Ok ("离线载荷: {0}" -f $script:Bundle) }
    if ($Offline) {
        if (-not $script:Bundle) { Err '-Offline 但没有离线载荷（bundle\）：用 -BundleDir 指定，或改用在线模式'; exit 1 }
        if (-not (Test-Path (Join-Path $script:Bundle 'wheels'))) {
            Err ("离线载荷里没有 wheels\：{0}" -f $script:Bundle); exit 1
        }
    }

    # 安装根。判据与 install.ps1 的 Resolve-CoreDir / app\paths.py:echo_base() 同一条：
    #   全新安装 -> <根>\echo-core 是代码，六个兄弟目录是数据/模型/DSH/会议/指令/运行时
    #   已有扁平安装（根下就有 app\main.py）-> 代码就在根下，绝不搬家
    $def = if (Test-Path 'D:\') { 'D:\ECHO' } else { 'C:\ECHO' }
    if (-not $Root) {
        $interactive = $true
        try { if ([Console]::IsInputRedirected) { $interactive = $false } } catch { $interactive = $false }
        if (-not $Yes -and $interactive) {
            $ans = ''
            try { $ans = Read-Host ("    安装到哪个目录？（回车用默认）[{0}]" -f $def) } catch { }
            if ($ans) { $Root = $ans } else { $Root = $def }
        } else { $Root = $def }
    }
    # 用户粘进来的往往是 Explorer「复制为路径」那种带引号、带尾反斜杠的形状；
    # 直接进参数会变成 `"C:\ECHO\"`（反斜杠把收尾引号转义掉，下一项被吞进值里）。
    # 归一化只做三件事：去首尾空白 → 去引号 → 去尾反斜杠。
    $Root = ([string]$Root).Trim().Trim('"').Trim()
    $r = $Root -replace '/', '\' -replace '\\+$', ''
    if (-not $r) { Err '安装根是空的 —— 给一个盘符开头的绝对路径，例如 D:\ECHO'; exit 1 }
    if ($r -notmatch '^[A-Za-z]:[\\/]') {
        Err ("安装根必须是盘符开头的绝对路径：{0}（形如 D:\ECHO；不要带引号或结尾反斜杠）" -f $r)
        exit 1
    }
    if ($r -match '[^\x00-\x7F]') {
        Warn ("安装路径含非 ASCII 字符：{0}" -f $r)
        Warn '  转写引擎（funasr/nagisa/dynetk 等）读中文路径会出问题 —— 强烈建议纯英文路径'
    }
    if ((Split-Path $r -Leaf) -ieq 'echo-core') {
        # 目标被传成了代码目录（常见：从 <根>\echo-core\scripts 里重跑）→ 上提一层
        Info ("目标传的是代码目录，安装根上提为 {0}" -f (Split-Path $r -Parent))
        $r = Split-Path $r -Parent
    }
    $script:TargetRoot = $r
    # 早一点把"这个目录到底能不能写"问清楚：C:\ 根目录在受管电脑上通常要管理员，
    # 等到装运行时/写 data 再报"拒绝访问"就太晚了，而且现象像"安装包坏了"。
    try {
        New-Item -ItemType Directory -Force -Path $script:TargetRoot -ErrorAction Stop | Out-Null
    } catch {
        Err ("创建不了安装目录：{0}（{1}）—— 换一个有写权限的目录（例如 D:\ECHO）" -f
             $script:TargetRoot, $_.Exception.Message)
        exit 1
    }
    if (Test-Path (Join-Path $r 'app\main.py')) {
        $script:CoreDir = $r
        Info '检测到代码在安装根下（老式扁平安装）：原地升级，不移动代码'
    } else {
        $script:CoreDir = Join-Path $r 'echo-core'
    }
    Ok ("安装根: {0}" -f $script:TargetRoot)
    Info ("代码目录: {0}" -f $script:CoreDir)
    $drive = Get-PSDrive -Name $r.Substring(0, 1) -ErrorAction SilentlyContinue
    if ($drive) {
        $freeGb = [Math]::Round($drive.Free / 1GB, 1)
        if ($drive.Free -lt 3GB) { Warn ("{0}: 只剩 {1} GB —— 最小档要 ~0.5 GB，可能不够" -f $r.Substring(0, 1), $freeGb) }
        else { Info ("{0}: 剩余 {1} GB" -f $r.Substring(0, 1), $freeGb) }
    }
    try {
        $logDir = Join-Path $script:TargetRoot 'data\logs'
        New-Item -ItemType Directory -Force -Path $logDir | Out-Null
        $script:LogPath = Join-Path $logDir 'install-all.log'
    } catch { }
    EndStep '定位'
}

# ---------------------------------------------------------------- 离线的三个硬动作
function Test-RuntimePython([string]$RcDir) {
    foreach ($rel in @('python.exe', 'Scripts\python.exe')) {
        $p = Join-Path $RcDir $rel
        if (Test-Path $p) { return $p }
    }
    return ''
}

function Assert-Pip([string]$Py) {
    # 有没有 pip，以 **-m pip --version 的退出码**为准（uv venv 默认不带 pip）。
    # 补 pip 用 ensurepip：它在 CPython 自带 wheel，**不需要网络**。
    $r = Invoke-Native $Py @('-m', 'pip', '--version')
    if ($r.code -eq 0) { return $true }
    Info '这个运行时没有 pip（uv 建的 venv 默认不带）—— 用 ensurepip 补（离线可用）'
    $e = Invoke-Native $Py @('-m', 'ensurepip', '--upgrade', '--default-pip')
    if ($e.code -ne 0 -and $e.out) { Info ("ensurepip: {0}" -f $e.out) }
    $r2 = Invoke-Native $Py @('-m', 'pip', '--version')
    return ($r2.code -eq 0)
}

function Install-EmbeddedFromBundle([string]$RcDir) {
    # ③ 兜底：包里的 python.org 嵌入包 + get-pip.py（**全部离线**）。
    # 为什么要这一级：目标机可能既没有 uv、也没有 Python 3.11 —— 而"离线包能装"
    # 不该依赖目标机上恰好有个 Python。get-pip.py 本身是个 zipapp，带
    # `--no-index --find-links` 就能只用本地 wheel 把 pip 装上（实测可行，见 docs）。
    $zip = Join-Path $script:Bundle 'runtime\python-3.11.9-embed-amd64.zip'
    $getpip = Join-Path $script:Bundle 'runtime\get-pip.py'
    if (-not (Test-Path $zip) -or -not (Test-Path $getpip)) {
        Info ("包里没有 runtime\{0} —— 这一级跳过" -f 'python-3.11.9-embed-amd64.zip')
        return ''
    }
    $wheels = Join-Path $script:Bundle 'wheels'
    New-Item -ItemType Directory -Force -Path $RcDir | Out-Null
    $t = Invoke-Native 'tar' @('-xf', $zip, '-C', $RcDir)
    if ($t.code -ne 0) { Warn ("解压嵌入包失败：{0}" -f $t.out); return '' }
    $py = Join-Path $RcDir 'python.exe'
    if (-not (Test-Path $py)) { Warn '嵌入包里没有 python.exe'; return '' }
    # ._pth：嵌入包是 **isolated 模式**（cwd 与 PYTHONPATH 都不算数）——
    #   * `import site` 不开，pip 装的包 import 不到；
    #   * 代码目录必须显式写进去，否则服务起不来报 No module named 'app'。
    # 相对项按"安装根 + 代码目录名"算（与 install.ps1 的 Install-EmbeddedPython 同一条）。
    $coreRel = ''
    if ($script:CoreDir -and $script:TargetRoot -and
        $script:CoreDir.StartsWith($script:TargetRoot, [StringComparison]::OrdinalIgnoreCase)) {
        $coreRel = $script:CoreDir.Substring($script:TargetRoot.Length).TrimStart('\', '/')
    }
    $codeEntry = if ($coreRel) { '..\' + $coreRel } else { '..' }
    $pth = Get-ChildItem (Join-Path $RcDir '*._pth') -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($pth) {
        $keep = @(Get-Content $pth.FullName | Where-Object {
            $_ -notmatch '^\s*#?\s*import site\s*$' -and $_.Trim() -ne '..' -and
            $_.Trim() -ne 'Lib\site-packages' -and $_.Trim() -ne $codeEntry })
        Set-Content -Path $pth.FullName -Value ($keep + @('import site', 'Lib\site-packages', $codeEntry)) -Encoding ASCII
        Ok ("嵌入包 sys.path 已写入代码目录（{0}）" -f $codeEntry)
    }
    Info '离线装 pip（get-pip.py --no-index --find-links bundle\wheels）...'
    $gp = Invoke-Native $py @($getpip, '--no-index', '--find-links', $wheels, '--no-warn-script-location')
    if ($gp.code -ne 0) {
        Warn ("离线装 pip 失败（返回 {0}）：{1}" -f $gp.code, $gp.out)
        return ''
    }
    if (-not (Assert-Pip $py)) { Warn '嵌入包里 pip 仍不可用'; return '' }
    Ok '嵌入包运行时已就绪（python.exe / pythonw.exe / pip，全程离线）'
    return $py
}

# ---------------------------------------------------------------- 运行时 ABI 闸
# 离线包里的**二进制轮子**是给固定 ABI 编的：`cp311` + `win_amd64`（出包时按目标解释器定死，
# 见 scripts/build_offline_pack.py 的 wheelhouse）。而 **pip 对 ABI 不匹配的轮子是静默忽略的** ——
# 于是"运行时不是 3.11"这件事会表现成「bundle\wheels 里缺 wheel：No matching distribution
# found for PyYAML」，报错还把人往"缺 wheel"上引（**红的地方不是坏的地方**）。
#
# 2026-10-09 真机事故（同事的 kit 卡在 [3/9] 装核心依赖）根因就是这一层没判 ABI：
#   ① 复用了目标机上**已有的**非 3.11 runtime-core；
#   ② 候选里 `python` / `python3` **没有版本约束**，本机 python 是 3.12/3.13 时照样被采信，
#      于是包里那份 3.11.9 嵌入包**永远轮不到**。
# 判据只有一条：解释器必须是 CPython 3.11（64 位）。下面两个函数是**唯一**判据。
$script:WantPyMajor = 3
$script:WantPyMinor = 11
# 探针**故意不含引号、不含空格**：WinPS 5.1 把参数交给原生命令时会吃掉内嵌引号
# （实测：`print("x")` 传过去变成 `print(x)` → SyntaxError）——那样这个闸会**永远判失败**，
# 比没有闸更糟。所以用 chr() 拼字符：46='.'、80='P'。输出形如 `3.11.15.64`（主.次.微.位宽）。
$script:RuntimeAbiProbe = 'import sys,struct;print(chr(46).join([str(sys.version_info[0]),str(sys.version_info[1]),str(sys.version_info[2]),str(struct.calcsize(chr(80))*8)]))'

function Get-RuntimeAbi([string]$Py) {
    # 人能读的 ABI 描述（判不出来回空串）。用 -c 探，不依赖 pip。
    if (-not $Py) { return '' }
    $r = Invoke-Native $Py @('-c', $script:RuntimeAbiProbe)
    if ($r.code -ne 0) { return '' }
    $out = ("$($r.out)").Trim()
    if ($out -match '^(\d+)\.(\d+)\.(\d+)\.(\d+)$') {
        return ("CPython {0}.{1}.{2}（{3} 位）" -f $Matches[1], $Matches[2], $Matches[3], $Matches[4])
    }
    return ''
}

function Test-RuntimeAbi([string]$Py) {
    # 唯一判据：CPython 3.11 + 64 位。位宽也判 —— win_amd64 轮子在 32 位解释器上一样被忽略。
    if (-not $Py) { return $false }
    $r = Invoke-Native $Py @('-c', $script:RuntimeAbiProbe)
    if ($r.code -ne 0) { return $false }
    $out = ("$($r.out)").Trim()
    if ($out -notmatch '^(\d+)\.(\d+)\.(\d+)\.(\d+)$') { return $false }
    return ([int]$Matches[1] -eq $script:WantPyMajor -and
            [int]$Matches[2] -eq $script:WantPyMinor -and
            [int]$Matches[4] -eq 64)
}

function Move-BadRuntimeAside([string]$RcDir) {
    # ABI 不对的 runtime-core：**搬开而不是删掉**（留证据；也免得下一次又被复用到）。
    # 搬不动就删（它是派生产物，重建不要紧）。
    $bad = $RcDir + '.incompatible-' + (Get-Date -Format 'yyyyMMddHHmmss')
    try { Move-Item $RcDir $bad -Force; return $bad }
    catch {
        Remove-Item $RcDir -Recurse -Force -ErrorAction SilentlyContinue
        return '(已删除)'
    }
}

function Build-OfflineRuntime {
    # 离线建运行时：**每一级都不联网**。顺序就是判据（2026-10-09 事故后重排）：
    #   ① 已经就绪（重跑）—— **必须过 ABI 闸**（CPython 3.11 + 64 位）
    #   ② 版本钉死的两条：uv 的缓存 CPython 3.11 / `py -3.11`
    #   ③ **包里的 python.org 3.11.9 嵌入包 + get-pip.py** —— 目标机**没有 Python**、
    #      或只有**低版本**（3.9/3.10/2.7）时走的就是这条；它**永远是对的**
    #   ④ 兜底：本机 `python` / `python3`（**没有版本约束**，必须过 ABI 闸；
    #      只在包里缺嵌入包时才轮到 —— 排在 ③ 之后就不会让低版本机器白建 venv）
    Step '建运行时（离线：只看本机与包里的载荷）'
    $rcDir = Join-Path $script:TargetRoot 'runtime-core'
    $py = Test-RuntimePython $rcDir
    if ($py) {
        if (Test-RuntimeAbi $py) {
            Ok ("runtime-core 已就绪: {0}（{1}）" -f $py, (Get-RuntimeAbi $py))
            if (-not (Assert-Pip $py)) { Err '这个运行时没有可用的 pip'; exit 1 }
            EndStep '运行时'
            return $py
        }
        # **ABI 不对就别复用**（2026-10-09 事故的头一条）：复用它的结果是 pip 静默忽略
        # 所有 cp311 轮子，最后报"缺 wheel"。搬开它、重建一个 —— 别让用户去猜。
        $abi = Get-RuntimeAbi $py
        if (-not $abi) { $abi = '判不出 ABI 的解释器' }
        Warn ("已有的 runtime-core 是 {0}，但离线包的二进制轮子只认 CPython {1}.{2}（win_amd64）" -f
              $abi, $script:WantPyMajor, $script:WantPyMinor)
        $moved = Move-BadRuntimeAside $rcDir
        Warn ("  复用它会得到「bundle\wheels 里缺 wheel」这种**指错地方**的报错；已把它搬到 {0}，现在重建" -f $moved)
        $py = ''
    }
    $made = ''
    $uv = Get-Command uv -ErrorAction SilentlyContinue
    if ($uv) {
        Info '① 用 uv 建 venv（命中 uv 的 CPython 缓存，不联网）...'
        $r = Invoke-Native $uv.Source @('venv', '--python', '3.11', $rcDir)
        if ($r.out) { Write-Host ("      " + $r.out) -ForegroundColor DarkGray }
        $made = Test-RuntimePython $rcDir
        if ($made) { Ok 'uv 这条路成了' } else { Warn 'uv 没成（缓存里没有 CPython 3.11）—— 下一级' }
    }
    if (-not $made) {
        # 这一级只试**钉死版本**的那个（`py -3.11`）。**没有版本约束的 `python` / `python3`
        # 已挪到嵌入包之后**（2026-10-09）：它们正是那次事故的来源（本机 python 是 3.12/3.13 时
        # 建出 ABI 不对的运行时却被直接采信，于是包里那份 3.11.9 **永远轮不到**）。
        # 放到后面的另一个好处：**低版本机器不会白建** —— 3.9/3.10 上 `python -m venv` 要跑十来秒、
        # 还必然被闸枪毙；Microsoft Store 那个 python 桩甚至会弹出应用商店。
        foreach ($cand in @(@('py', @('-3.11')))) {
            $exe = Get-Command $cand[0] -ErrorAction SilentlyContinue
            if (-not $exe) { continue }
            Info ("② 用 {0} 建 venv（本机 Python，不联网）..." -f $cand[0])
            if (Test-Path $rcDir) { Remove-Item $rcDir -Recurse -Force -ErrorAction SilentlyContinue }
            $r = Invoke-Native $exe.Source ($cand[1] + @('-m', 'venv', $rcDir))
            if ($r.out) { Write-Host ("      " + $r.out) -ForegroundColor DarkGray }
            $made = Test-RuntimePython $rcDir
            if ($made -and -not (Test-RuntimeAbi $made)) {
                Warn ("{0} 建出来的运行时是 {1}，不是 CPython {2}.{3} —— 换下一级" -f
                      $cand[0], (Get-RuntimeAbi $made), $script:WantPyMajor, $script:WantPyMinor)
                Remove-Item $rcDir -Recurse -Force -ErrorAction SilentlyContinue
                $made = ''
            }
            if ($made) { break }
        }
    }
    if (-not $made) {
        Info '③ 用包里的 python.org 嵌入包（不联网）...'
        $made = Install-EmbeddedFromBundle $rcDir
    }
    if (-not $made) {
        # **④ 最后才试"没有版本约束"的本机 python**：走到这里说明包里**没有**嵌入包
        # （薄包 / 被裁过的包），只剩这条路 —— 所以仍然要过 ABI 闸；不合格就让它明确失败，
        # 别变成"装完却 import 不到"或者"缺 wheel"那种指错地方的报错。
        foreach ($cand in @(@('python', @()), @('python3', @()))) {
            $exe = Get-Command $cand[0] -ErrorAction SilentlyContinue
            if (-not $exe) { continue }
            Info ("④ 用 {0} 建 venv（本机 Python，**没有版本约束**）..." -f $cand[0])
            if (Test-Path $rcDir) { Remove-Item $rcDir -Recurse -Force -ErrorAction SilentlyContinue }
            $r = Invoke-Native $exe.Source ($cand[1] + @('-m', 'venv', $rcDir))
            if ($r.out) { Write-Host ("      " + $r.out) -ForegroundColor DarkGray }
            $made = Test-RuntimePython $rcDir
            if ($made -and -not (Test-RuntimeAbi $made)) {
                Warn ("{0} 建出来的运行时是 {1}，不是 CPython {2}.{3} —— 跳过" -f
                      $cand[0], (Get-RuntimeAbi $made), $script:WantPyMajor, $script:WantPyMinor)
                Remove-Item $rcDir -Recurse -Force -ErrorAction SilentlyContinue
                $made = ''
            }
            if ($made) { break }
        }
    }
    if (-not $made) {
        Err '离线建不出运行时：本机没有 uv 缓存、没有 Python 3.11（本机 python/python3 也不是 3.11），包里也没有 runtime\python-3.11.9-embed-amd64.zip'
        Err '  补救：① 用带 bundle\runtime 的完整离线包（build_min_kit.py 出的那个）；或'
        Err '        ② 本机装一个 Python 3.11（python.org，勾 Add to PATH）后重跑；或'
        Err '        ③ 改用在线模式（去掉 -Offline）'
        exit 1
    }
    if (-not (Assert-Pip $made)) { Err '运行时建好了但没有可用的 pip'; exit 1 }
    Ok ("runtime-core 就绪: {0}" -f $made)
    EndStep '运行时'
    return $made
}

function Install-CoreDepsOffline([string]$Py) {
    Step '装核心依赖（离线：pip --no-index --find-links bundle\wheels）'
    $req = Join-Path $script:Tree 'requirements-core.txt'
    if (-not (Test-Path $req)) { Err ("包里没有 requirements-core.txt：{0}" -f $req); exit 1 }
    $wheels = Join-Path $script:Bundle 'wheels'
    Info ("pip install --no-index --find-links `"{0}`" -r `"{1}`"" -f $wheels, $req)
    Log 'EXEC' ("{0} -m pip install --no-index --find-links {1} -r {2}" -f $Py, $wheels, $req)
    $prevEA = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        & $Py -m pip install --no-index --find-links $wheels --no-warn-script-location -r $req 2>&1 |
            ForEach-Object { Write-Host "      $_" -ForegroundColor DarkGray }
        $code = $LASTEXITCODE
    } finally { $ErrorActionPreference = $prevEA }
    if ($code -ne 0) {
        Err ("核心依赖安装失败（pip 返回 {0}）—— 这一步失败就是它，别往下猜" -f $code)
        # **先判 ABI，再怪 wheel**（2026-10-09 事故）：pip 对 ABI 不匹配的轮子是静默忽略的，
        # 于是"运行时不是 3.11"会表现成"缺 wheel" —— 那句提示曾把人引去查错地方。
        if (-not (Test-RuntimeAbi $Py)) {
            $abi = Get-RuntimeAbi $Py
            if (-not $abi) { $abi = '判不出 ABI 的解释器' }
            Err ("  真正的原因多半在这里：这个运行时是 {0}，而 bundle\wheels 里的二进制轮子只认 CPython {1}.{2}（win_amd64）" -f
                 $abi, $script:WantPyMajor, $script:WantPyMinor)
            Err '  pip 会**静默忽略**那些轮子，于是第一个二进制依赖（PyYAML）报 "from versions: none"。'
            Err ("  补救：删掉 {0} 后重跑（会用包里自带的 python-3.11.9 嵌入包），或本机装 Python 3.11（勾 Add to PATH）后重跑" -f
                 (Join-Path $script:TargetRoot 'runtime-core'))
        } else {
            Err ("  运行时的 ABI 是对的（{0}）—— 那就真是 bundle\wheels 里缺 wheel：看上面 `"No matching distribution`" 是哪一行" -f
                 (Get-RuntimeAbi $Py))
        }
        exit 1
    }
    $im = Invoke-Native $Py @('-c', 'import fastapi, uvicorn')
    if ($im.code -ne 0) { Err ("核心依赖装完却 import 不了：{0}" -f $im.out); exit 1 }
    Ok '核心依赖就绪（fastapi / uvicorn 可导入）'
    EndStep '核心依赖'
}

function Copy-BundleModels {
    Step '准备模型（离线：直接用包里的）'
    $src = Join-Path $script:Bundle 'models'
    if (-not (Test-Path $src)) { Info '客户端不装本地模型（转写全部走能力后端）——bundle 里没有 models\ 是正常的'; EndStep '模型'; return }
    $dst = Join-Path $script:TargetRoot 'models'
    New-Item -ItemType Directory -Force -Path $dst | Out-Null
    $items = @(Get-ChildItem $src -Force)
    foreach ($it in $items) {
        $to = Join-Path $dst $it.Name
        if ($it.PSIsContainer) { Copy-Item -LiteralPath $it.FullName -Destination $dst -Recurse -Force }
        else { Copy-Item -LiteralPath $it.FullName -Destination $to -Force }
        Info ("{0} -> {1}" -f $it.Name, $dst)
    }
    $mb = [Math]::Round((Get-ChildItem $src -Recurse -File | Measure-Object Length -Sum).Sum / 1MB, 1)
    Ok ("模型已就位（{0} 项 / {1} MB，零下载）" -f $items.Count, $mb)
    EndStep '模型'
}

# ---------------------------------------------------------------- 主程序（复用 install.ps1）
function Install-TreeAndShortcuts {
    Step '就位主程序 + 运行时 + 快捷方式（复用 scripts\install.ps1）'
    $ps1 = Join-Path $script:Tree 'scripts\install.ps1'
    if (-not (Test-Path $ps1)) { Err ("主程序里没有 scripts\install.ps1：{0}" -f $ps1); exit 1 }
    $argList = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $ps1,
                 '-DestDir', $script:TargetRoot, '-Silent', '-SkipStart')
    if ($Offline) {
        # 离线时运行时和核心依赖我们已经装好了 —— 让 install.ps1 别再去碰它们
        $argList += '-SkipRuntime'
    } elseif ($PipIndex) {
        $argList += @('-PipIndex', $PipIndex)
    }
    # 升级不动桌面/开机自启（那是用户自己的环境，不属于"核心代码"）
    if ($NoShortcuts -or $script:IsUpgrade) { $argList += @('-SkipDesktopLnk', '-SkipStartupLnk') }
    Info ("powershell {0}" -f ($argList -join ' '))
    # 离线模式：把 pip 也钉死在本地（万一 install.ps1 里还有别的 pip 调用）
    $saved = @{}
    if ($Offline) {
        foreach ($k in @('PIP_NO_INDEX', 'PIP_FIND_LINKS', 'PIP_DISABLE_PIP_VERSION_CHECK')) {
            $saved[$k] = [Environment]::GetEnvironmentVariable($k, 'Process')
        }
        $env:PIP_NO_INDEX = '1'
        $env:PIP_FIND_LINKS = (Join-Path $script:Bundle 'wheels')
        $env:PIP_DISABLE_PIP_VERSION_CHECK = '1'
    }
    try {
        $r = Invoke-Native 'powershell' $argList
        if ($r.out) { Write-Host ($r.out -split "`n" | ForEach-Object { "      $_" } | Out-String).TrimEnd() -ForegroundColor DarkGray }
        if ($r.code -ne 0) {
            Err ("install.ps1 退出码 {0} —— 安装没成（日志：{1}\ECHO-install.log）" -f $r.code, $env:TEMP)
            exit 1
        }
    } finally {
        if ($Offline) {
            foreach ($k in $saved.Keys) {
                if ($null -eq $saved[$k]) { Remove-Item ("env:" + $k) -ErrorAction SilentlyContinue }
                else { Set-Item ("env:" + $k) $saved[$k] }
            }
        }
    }
    Ok '主程序就位（含快捷方式）'
    EndStep '就位'
}

# ---------------------------------------------------------------- 我们自己的服务与端口
function Test-EchoApi([int]$Port, [string]$Path = '/api/status', [int]$TimeoutSec = 5) {
    try {
        $resp = Invoke-WebRequest -Uri ("http://127.0.0.1:{0}{1}" -f $Port, $Path) `
                                  -UseBasicParsing -TimeoutSec $TimeoutSec
        return ($resp.StatusCode -eq 200)
    } catch { return $false }
}

function Start-EchoOwnService {
    # 把**我们自己的**实例拉起来，并把端口钉死给后面的组件脚本。
    #
    # 为什么这件事必须由本脚本做（2026-09-25 离线实测踩到）：
    # 组件脚本的 `Get-EchoPort` 依次看 `$env:ECHO_PORT` → `<安装根>\data\echo-port.txt` → 8970。
    # 于是只要环境里带着 ECHO_PORT（开发机就设着 18060）、或者 8970 上恰好有**别的** ECHO，
    # 组件脚本就会连到别人的实例上去写设置、登记安装 —— 第一次离线实测就把
    # `sttModel` / `meetingSttModel` 写进了开发机上那个实例（还好值没变）。
    # 这里改成：只认我们自己的端口文件；服务没起来就自己拉起来；然后把 $env:ECHO_PORT
    # **覆盖**成我们的端口，让组件脚本没有别的选择。
    Step '启动 ECHO 服务（只用我们自己的实例与端口）'
    $portFile = Join-Path $script:TargetRoot 'data\echo-port.txt'
    $port = 0
    if (Test-Path $portFile) {
        try { $port = [int]((Get-Content $portFile -Raw).Trim()) } catch { $port = 0 }
    }
    if ($port -gt 0 -and (Test-EchoApi $port)) {
        Ok ("本实例已在运行（端口 {0}）" -f $port)
    } else {
        if ($port -gt 0) { Info ("端口文件里的 {0} 没有响应（上次装的残留）—— 重新启动" -f $port) }
        $port = 0
        # **先清掉继承来的 ECHO_PORT**：开发机/CI 上它常常指着别人的实例
        if (Test-Path env:ECHO_PORT) {
            Warn ("环境里的 ECHO_PORT={0} 指向别的实例，已清除（本次要装的是 {1}）" -f $env:ECHO_PORT, $script:TargetRoot)
            Remove-Item env:ECHO_PORT -ErrorAction SilentlyContinue
        }
        $start = Join-Path $script:CoreDir 'scripts\start.ps1'
        if (-not (Test-Path $start)) { Err ("找不到 {0} —— 主程序没就位" -f $start); exit 1 }
        Info ("后台启动：{0} -Background" -f $start)
        try {
            Start-Process powershell -ArgumentList @('-NoProfile', '-ExecutionPolicy', 'Bypass',
                '-File', "`"$start`"", '-Background') -WindowStyle Hidden -ErrorAction Stop
        } catch {
            # 没有控制台的环境里拉起 console 程序会失败（ERROR_NO_DATA）—— 不算安装失败
            Warn ("自动启动没成功：{0}" -f $_.Exception.Message)
        }
        for ($i = 0; $i -lt 45; $i++) {
            Start-Sleep -Seconds 2
            if (Test-Path $portFile) {
                try { $port = [int]((Get-Content $portFile -Raw).Trim()) } catch { $port = 0 }
                if ($port -gt 0 -and (Test-EchoApi $port)) { break }
            }
        }
    }
    if ($port -le 0 -or -not (Test-EchoApi $port)) {
        # 到这里说明我们的实例没起来。**不能**让组件脚本自己去猜端口 ——
        # 猜错就是"设置写进了别人的 ECHO"。如实停下，把下一步写清楚。
        Err '本实例没起来（端口文件没出现或没响应）—— 停在这里，避免把设置写进别的 ECHO 实例'
        Err ("  看日志：{0}\data\logs\echo-server.log" -f $script:TargetRoot)
        Err ("  手动起来：powershell -NoProfile -ExecutionPolicy Bypass -File `"{0}\scripts\start.ps1`"" -f $script:CoreDir)
        Err '  起来之后重跑本命令即可（已装好的会跳过）'
        EndStep '服务'
        exit 1
    }
    $env:ECHO_PORT = "$port"
    Ok ("服务端口已钉死：ECHO_PORT={0}（组件那一步只认它）" -f $port)
    Log 'EXEC' ("ECHO_PORT={0}（本实例端口，服务于 {1}）" -f $port, $script:TargetRoot)
    $script:OwnPort = $port
    EndStep '服务'
    return $port
}

# ---------------------------------------------------------------- 组件（技能脚本，唯一实现）
function Invoke-ComponentsScript {
    param([string[]]$EngineList,
          # 升级专用：不改任何设置、不碰依赖/模型；DSH 升级只在用户同意时传 UpgradeDsh
          [switch]$KeepSettings, [switch]$SkipPip, [switch]$SkipModels, [switch]$UpgradeDsh)
    Step '装组件（依赖 / 模型 / 设置 / 自检，调技能里的同一个脚本）'
    if (-not $script:SkillDir) { Err '没有 echo-install 技能目录 —— 组件这一步跑不了'; exit 1 }
    $comp = Join-Path $script:SkillDir 'scripts\echo-install-components.ps1'
    if (-not (Test-Path $comp)) { Err ("技能里没有 echo-install-components.ps1：{0}" -f $comp); exit 1 }
    # 调用方式：**在当前进程里 `&` 调**，而不是 powershell -File / -Command。三条实测结论：
    #   * `-File 脚本.ps1 -Engines a,b` 会把 "a,b" 当成**一个**字符串（-File 不做数组解析）
    #     → 引擎表查不到 → 设置被静默跳过（技能文档里记着这个坑）；
    #   * `-Command "& 脚本"` 里被调脚本的 `exit N` **不会**变成宿主进程的退出码（实测恒为 1）；
    #   * `& 脚本` 在当前进程里：脚本的 `exit N` 只结束那个脚本、调用方继续，`$LASTEXITCODE`
    #     就是 N（实测 5.1 与 7 都是这样）—— 数组参数也照常按数组传。
    # 2026-10-31：客户端不再装本地 STT 引擎/唤醒/分离 —— 只传空 Engines。
    $argv = @{ DestDir = $script:TargetRoot; Engines = @(); Agent = $Agent }
    if ($NotesDir) { $argv['NotesDir'] = $NotesDir }
    if ($PipIndex -and -not $Offline) { $argv['PipIndex'] = $PipIndex }
    if ($KeepSettings) { $argv['KeepSettings'] = $true }
    if ($SkipPip) { $argv['SkipPip'] = $true }
    if ($SkipModels) { $argv['SkipModels'] = $true }
    if ($UpgradeDsh) { $argv['UpgradeDsh'] = $true }
    if ($DshDir) { $argv['DshDir'] = $DshDir }
    if ($AskDsh) { $argv['AskDsh'] = $true }
    if ($HarnessSource) { $argv['HarnessSource'] = $HarnessSource }
    if ($DesktopInstallDir) { $argv['DesktopInstallDir'] = $DesktopInstallDir }
    if ($Offline) { $argv['Offline'] = $true }
    Info ("& {0} -DestDir {1} -Agent {2}" -f $comp, $script:TargetRoot, $Agent)
    Log 'EXEC' ("& {0} -DestDir {1} -Agent {2} (no local STT engines)" -f `
                $comp, $script:TargetRoot, $Agent)
    $code = 1
    try {
        & $comp @argv
        $code = $LASTEXITCODE
        if ($null -eq $code) { $code = 0 }
    } catch {
        Err ("组件脚本抛异常：{0}" -f $_.Exception.Message)
        $code = 1
    }
    EndStep '组件'
    return $code
}

# ---------------------------------------------------------------- 收尾：面板地址 + 自检
function Get-EchoPort {
    $f = Join-Path $script:TargetRoot 'data\echo-port.txt'
    if (Test-Path $f) {
        try { $v = [int]((Get-Content $f -Raw).Trim()); if ($v -gt 0) { return $v } } catch { }
    }
    return 8970
}

function Show-Result {
    param([int]$ComponentsExit)
    Step '收尾：面板地址与状态'
    # 端口以**我们自己**那份端口文件为准（Start-EchoOwnService 已经确认过它可用）
    $port = if ($script:OwnPort) { $script:OwnPort } else { Get-EchoPort }
    $url = "http://127.0.0.1:$port/"
    $status = $null
    if (-not $SkipStart) {
        for ($i = 0; $i -lt 10; $i++) {
            try {
                $resp = Invoke-WebRequest -Uri ("http://127.0.0.1:{0}/api/status" -f $port) `
                                          -UseBasicParsing -TimeoutSec 5
                $bytes = if ($resp.RawContentStream) { $resp.RawContentStream.ToArray() } else { @() }
                $status = ([System.Text.Encoding]::UTF8.GetString($bytes) | ConvertFrom-Json)
                break
            } catch { Start-Sleep -Seconds 2 }
        }
    }
    Write-Host ''
    Write-Host '  ────────────────────────────────────────────────' -ForegroundColor DarkGray
    if ($status) {
        Ok ("服务在跑，面板地址: {0}" -f $url)
        $comp = @($status.components | Where-Object { $_.name -in @('stt', 'harness', 'dsh') })
        foreach ($c in $comp) { Info ("{0,-8} {1}" -f $c.name, $c.status) }
        try {
            # 2026-10-31：客户端不再装本地模型 —— /api/models 的 items 为空是正常的。
        } catch { }
    } else {
        Warn ("服务没起来（或 -SkipStart）—— 面板地址（起来后）: {0}" -f $url)
        Info ("手动启动：powershell -NoProfile -ExecutionPolicy Bypass -File `"{0}\scripts\start.ps1`"" -f $script:CoreDir)
    }
    if ($script:BackendSummary) { Info $script:BackendSummary }
    if ($script:IsUpgrade) {
        Info ("升级完成：核心代码 {0} → {1}" -f $script:UpgradeFromVersion, $script:UpgradeToVersion)
        Info '数据 / 设置 / 模型权重 / 后端与手表配置 / 智能体配置：一律未改动'
        if ($script:UpgradeDbBackup) { Info ("升级前数据库备份：{0}" -f $script:UpgradeDbBackup) }
    }
    Write-Host '  ────────────────────────────────────────────────' -ForegroundColor DarkGray
    Write-Host ''
    EndStep '收尾'
    Write-Host '  分阶段耗时：' -ForegroundColor White
    $total = 0
    foreach ($p in $script:Phases) { $total += $p.Seconds; Info ("{0,-10} {1,6} s" -f $p.Name, $p.Seconds) }
    Info ("{0,-10} {1,6} s" -f '合计', [Math]::Round($total, 1))
    Info ("{0,-10} {1,6} s" -f '总耗时', [Math]::Round(((Get-Date) - $script:T0).TotalSeconds, 1))
    Write-Host ("  日志：{0}" -f $script:LogPath) -ForegroundColor DarkGray
    Write-Host ''
    if ($ComponentsExit -eq 0) {
        if ($script:IsUpgrade) { Write-Host '  升级完成（只更新了核心代码）。' -ForegroundColor Green }
        else { Write-Host '  全部就绪。' -ForegroundColor Green }
    } else {
        Write-Host ("  装完了，但组件那一步有没就绪的项（退出码 {0}）—— 上面列了是哪几项。" -f $ComponentsExit) -ForegroundColor Yellow
    }
}

# ---------------------------------------------------------------- 后端：会议转写那台 GPU 服务
# 用户 2026-10-01 定的：安装流程**后面**要多问一句"后端怎么来"。三种答法：
#   ① 用别人给的后端 → 贴配对串（`echo://pair?host=…&code=…`）→ 当场配对（POST /api/capability/pair）；
#   ② 本机跑 → 把交付目录里的后端包解开到 `<安装根>\backend`：**有离线包就直接复制启用
#      （零下载）**，只有薄包就把它放好，然后**触发下载**（POST /api/capability/backend/start，
#      它会在后台按国内源装运行时，面板「能力」里看得见进度）；
#   ③ 先不配 → 以后在面板里弄。
#
# 三条纪律（都有代价，别改成"看着更省事"的写法）：
#   1. **这一步永远不让安装失败**：后端是"会议转写往哪走"的可选项，装客户端本身是好的 ——
#      报错要响亮，但退出码照旧由组件那一步决定（`Show-Result` 里如实写一行）。
#   2. **不静默改用户的配对**：本机跑这条路只**准备文件**，起后端/配对交给 `backend/start`
#      那个入口（它自己会判断"已配对到别的后端 → 不覆盖"）。配对串那条路是用户当场给的，
#      属于明确意图，可以直接配。
#   3. **不做第二份"找包/认包"实现**：搜落点、认内容、离线包优先这几条判据都在
#      `app/backend_fetch.py` 里（含用例）。这里只做"用户指的那几个 zip 就在这个目录里"。

function ConvertFrom-EchoPairString {
    # 与面板同一个解析器（`web/app.js::parsePairString`）：`echo://pair?host=…&code=…&fp=…`。
    # 认三个键（host|url、code、fp|fingerprint），`+` 当空格（form 编码那一套），未知键忽略。
    # 没带 scheme 时**把整串当地址**（有人只贴 `http://10.100.0.24:8900`）。
    param([string]$Text)
    $out = @{ Url = ''; Code = ''; Fp = '' }
    # 从聊天/文档里复制过来时常常带引号或反引号（`echo://pair?...`）、或被 HTML 转义成
    # `&amp;` —— 一并规整，否则它会连同这些字符被当成地址/码传给配对接口，
    # 报错还看不出是复制的问题。
    $t = ('{0}' -f $Text).Replace('&amp;', '&').Trim()
    $t = $t.Trim().Trim('"').Trim("'").Trim('`').Trim()
    if (-not $t) { return $out }
    $m = [regex]::Match($t, '^echo://pair\b[^?]*\?(.*)$', 'IgnoreCase')
    if (-not $m.Success) { $out.Url = $t; return $out }
    foreach ($kv in $m.Groups[1].Value.Split('&')) {
        $i = $kv.IndexOf('=')
        if ($i -lt 0) { continue }
        $k = [System.Uri]::UnescapeDataString($kv.Substring(0, $i)).Trim().ToLower()
        $v = [System.Uri]::UnescapeDataString($kv.Substring($i + 1).Replace('+', ' ')).Trim()
        switch ($k) {
            'host'        { $out.Url = $v }
            'url'         { $out.Url = $v }
            'code'        { $out.Code = $v }
            'fp'          { $out.Fp = $v }
            'fingerprint' { $out.Fp = $v }
        }
    }
    return $out
}

function Find-BackendZip {
    # 在**用户指的那个目录**（交付目录）里找一个后端包。名字只用来粗筛，**内容判据在 Python 侧**；
    # 但"名字一个都对不上"时不该静默跳过，所以这里也认那几种人工改名（汇总目录里那份被改成了
    # `3-本机GPU后端包-20MB.zip`）。
    param([string]$Dir, [string[]]$Patterns)
    if (-not $Dir -or -not (Test-Path -LiteralPath $Dir)) { return '' }
    foreach ($p in $Patterns) {
        $hit = Get-ChildItem -LiteralPath $Dir -Filter $p -File -ErrorAction SilentlyContinue |
               Sort-Object Name | Select-Object -First 1
        if ($hit) { return $hit.FullName }
    }
    return ''
}

function Expand-BackendPackage {
    # 把后端包解开到 `<安装根>\backend`。**只搬白名单里的顶层项**，与
    # `app/backend_fetch.py::PACKAGE_DIR_ITEMS` 同一条口径 —— 那个目录里同时住着
    # `server.yaml`（我们生成的配置）、`state/`（鉴权库与本机配对文件）、`logs/`：
    # 整包倒进去迟早拿包里的模板覆盖掉用户的配置。
    # 解压走系统自带 `tar`（与 install.ps1 同一条路：受限语言模式下 Add-Type 会被拦）。
    param([string]$Zip, [string]$Backend)
    $tmp = Join-Path $Backend ('tmp\pkg-' + (Get-Date -Format 'HHmmss') + '-' + (Get-Random -Maximum 9999))
    New-Item -ItemType Directory -Force -Path $tmp | Out-Null
    try {
        $r = Invoke-Native 'tar' @('-xf', $Zip, '-C', $tmp)
        if ($r.code -ne 0) { return (($false), ("解不开（tar 退出码 {0}）：{1}" -f $r.code, $r.out)) }
        # 载荷根 = zip 里那层顶层目录（认 `<名>\server\requirements.txt`）；没有就用 tmp 本身
        $payload = ''
        if (Test-Path (Join-Path $tmp 'server\requirements.txt')) { $payload = $tmp }
        else {
            foreach ($sub in @(Get-ChildItem -LiteralPath $tmp -Directory -ErrorAction SilentlyContinue)) {
                if (Test-Path (Join-Path $sub.FullName 'server\requirements.txt')) {
                    $payload = $sub.FullName; break
                }
            }
        }
        if (-not $payload) { return (($false), '这个 zip 里没有后端包（找不到 server\requirements.txt）') }
        foreach ($name in @('app', 'server', 'runtime', 'wheels', 'scripts', 'models')) {
            $src = Join-Path $payload $name
            if (-not (Test-Path -LiteralPath $src)) { continue }
            $dst = Join-Path $Backend $name
            New-Item -ItemType Directory -Force -Path $dst | Out-Null
            # 用 robocopy 合并（与 build-package.ps1 同一个工具）：`Copy-Item -Recurse` 在目标
            # 已存在时会把整个目录**套进去**（`runtime\runtime`），而这里是"合并进已有目录"。
            # robocopy 的退出码 0~7 都算成功（1 = 有文件被复制），>=8 才是真失败。
            $rc = Invoke-Native 'robocopy' @($src, $dst, '/E', '/NFL', '/NDL', '/NJH', '/NJS', '/NP', '/R:1', '/W:1')
            if ($rc.code -ge 8) { return (($false), ("合并失败（robocopy 退出码 {0}）：{1}" -f $rc.code, $rc.out)) }
        }
        foreach ($name in @('server.yaml.tmpl', 'sources.json', 'manifest.json', 'MODELS-INCLUDED.txt')) {
            $src = Join-Path $payload $name
            if (Test-Path -LiteralPath $src) { Copy-Item -LiteralPath $src -Destination $Backend -Force }
        }
    } finally {
        Remove-Item -LiteralPath $tmp -Recurse -Force -ErrorAction SilentlyContinue
    }
    return (($true), 'OK')
}

function Test-BackendRuntimeUsable {
    # 与 `app/backend_env.check_server_deps` **同一条判据**：解释器在 **且** fastapi/uvicorn
    # 都能 import。这是"运行时到底能不能用"的唯一口径 —— 只看 `runtime\python.exe` 在不在
    # 会把"只装了半个运行时"报成就绪（2026-10-01 真机就是这么坑的）。
    param([string]$Backend)
    foreach ($rel in @('runtime\python.exe', 'runtime\Scripts\python.exe', 'runtime\bin\python3',
                       'runtime\bin\python')) {
        $py = Join-Path $Backend $rel
        if (-not (Test-Path -LiteralPath $py)) { continue }
        $r = Invoke-Native $py @('-c', 'import fastapi, uvicorn')
        if ($r.code -eq 0) { return (($true), $py) }
        return (($false), ("{0}（import 失败：{1}）" -f $py, $r.out))
    }
    return (($false), 'runtime\ 里没有解释器')
}

function Get-EchoOwnPort {
    # 端口以**我们自己**那份为准（`Start-EchoOwnService` 已经把它写进 $env:ECHO_PORT）
    if ($script:OwnPort) { return $script:OwnPort }
    if ($env:ECHO_PORT) { try { return [int]$env:ECHO_PORT } catch { } }
    return (Get-EchoPort)
}

function Start-LocalBackendNow {
    <#
      触发「起本机后端」并**等它就绪** —— 离线包那条分支原来只解包不启动，于是
      "装完就转写"必然撞「没有可用的后端」（2026-10-01 真机：用户装完就转写，失败）。
      走的是面板同一个入口（`POST /api/capability/backend/start` → 取运行时 → configure →
      起 → 配对 → 三层自测），跑在服务端的后台任务里。
    #>
    param([string]$Why = '')
    $port = Get-EchoOwnPort
    try {
        $null = Invoke-RestMethod -Uri ("http://127.0.0.1:{0}/api/capability/backend/start" -f $port) `
                -Method Post -ContentType 'application/json' -Body '{}' -TimeoutSec 30
    } catch {
        Warn ("触发「起本机后端」没成功：{0}" -f $_.Exception.Message)
        Info ("装完在面板点一下就行：http://127.0.0.1:{0}/ →「能力 → 起本机后端」" -f $port)
        return $false
    }
    if ($Why) { Info ("已触发「起本机后端」（{0}）—— 等它就绪…" -f $Why) }
    # 等任务结束（**最多 8 分钟**：模型加载要时间；超时就如实说"还在起"，不假装成功）
    $deadline = (Get-Date).AddSeconds(480)
    while ((Get-Date) -lt $deadline) {
        Start-Sleep -Seconds 5
        $st = $null
        try { $st = Invoke-RestMethod -Uri ("http://127.0.0.1:{0}/api/capability/backend" -f $port) -TimeoutSec 20 } catch { continue }
        if (-not $st.job.running) {
            if ($st.job.ok) { Ok ("后端就绪：{0}" -f $st.job.message); return $true }
            Warn ("后端没能就绪：{0}" -f $st.job.message)
            Info ("看进度/重试：http://127.0.0.1:{0}/ →「能力」页签" -f $port)
            return $false
        }
    }
    Warn '后端还在装/起（超过 8 分钟）—— 面板「能力」页签能看到进度'
    Info ("稍等一下再转写：http://127.0.0.1:{0}/" -f $port)
    return $false
}

function Get-HttpErrorInfo {
    # `Invoke-RestMethod` 的错误体（FastAPI 的 `{"detail": "一句人话"}`）在 PS 5.1 上
    # 藏在两个地方：`ErrorDetails.Message`（有时为空）与 `Exception.Response` 流。
    # 少了这一步，用户只看到 `HTTP 400: {...}` 或 `远程服务器返回错误: (400)`，
    # 而真正的原因（码过期 / 指纹不匹配 / 证书不对）全被吞掉 —— 2026-10-21 现场
    # 同事只能报"配对失败"，我们连是哪种失败都分不出来。
    param($Rec)
    $status = 0
    try { if ($Rec.Exception.Response) { $status = [int]$Rec.Exception.Response.StatusCode } } catch { }
    $raw = ''
    try { $raw = [string]$Rec.ErrorDetails.Message } catch { }
    if (-not $raw) {
        try {
            $stream = $Rec.Exception.Response.GetResponseStream()
            if ($stream) {
                $sr = New-Object System.IO.StreamReader($stream)
                $raw = $sr.ReadToEnd()
                $sr.Close()
            }
        } catch { }
    }
    $msg = ''
    if ($raw) {
        try {
            $j = $raw | ConvertFrom-Json
            if ($j.detail) { $msg = [string]$j.detail }
            elseif ($j.message) { $msg = [string]$j.message }
        } catch { $msg = $raw.Trim() }
    }
    if (-not $msg) { $msg = [string]$Rec.Exception.Message }
    return @{ Status = $status; Message = $msg }
}

function Invoke-EchoPair {
    # 只做一次配对 POST，返回 `@{ ok; message; status }` —— 不抛、不决定重试。
    param([string]$Url, [string]$Code, [string]$Fp)
    $port = Get-EchoOwnPort
    $body = @{ base_url = $Url; code = $Code; fingerprint = $Fp } | ConvertTo-Json
    try {
        $resp = Invoke-RestMethod -Uri ("http://127.0.0.1:{0}/api/capability/pair" -f $port) `
                                  -Method Post -ContentType 'application/json; charset=utf-8' `
                                  -Body ([System.Text.Encoding]::UTF8.GetBytes($body)) -TimeoutSec 60
        return @{ ok = $true; message = ("{0}" -f $resp.message); status = 200 }
    } catch {
        $err = Get-HttpErrorInfo $_
        Log 'PAIR' ("{0} -> HTTP {1}: {2}" -f $Url, $err.Status, $err.Message)
        return @{ ok = $false; message = $err.Message; status = $err.Status }
    }
}

function Test-DockerBridgeAddress {
    # 容器里自动探测到的是 Docker 网桥（实测 172.18.0.2），同事那台**必然连不上**。
    # 只用来给"连不上"补一句能落地的话，不参与配对判断。
    param([string]$Url)
    $m = [regex]::Match($Url, '^https?://(?<host>[^/:]+)')
    if (-not $m.Success) { return $false }
    return ($m.Groups['host'].Value -match '^172\.(1[7-9]|2[0-9]|3[01])\.')
}

function Get-PairFailureHint {
    param([string]$Url, [string]$Message, [int]$Status)
    if ($Status -eq 429) {
        return '后端在限流（同一来源 5 分钟内失败 5 次）—— 等 5 分钟再试，或让管理员看一下后端日志'
    }
    if ($Status -eq 401 -or $Status -eq 403) {
        return '配对码无效 / 已被使用 / 已过期 —— 配对码是一次性的，让管理员**重发一张**再试'
    }
    if ($Message -match '证书|指纹|fingerprint|SSL|TLS') {
        return '证书或配对串里的 fp= 对不上 —— 让管理员重新生成一张码（串里的 fp 要与现在这张证书一致）'
    }
    if (Test-DockerBridgeAddress $Url) {
        return ('这个地址看起来是 Docker 网桥（{0}），同事那台连不上 —— 让管理员在管理面把「对外公布地址」填成局域网/公网可达的 IP 或域名' -f $Url)
    }
    if ($Message -match '连不上|超时|timed out|拒绝|unreachable|无法连接') {
        return ('先确认这台机器能访问 {0}（同网段 / 防火墙 / 端口），再贴一次' -f $Url)
    }
    return '面板 →「能力」里可以重贴一次；配对串是一次性的，过期就让管理员重发'
}

function Invoke-BackendStep {
    param([string]$Dir)
    Step '后端：会议转写用的 GPU 服务怎么来'
    $script:BackendSummary = ''
    $mode = $Backend
    $interactive = $true
    try { if ([Console]::IsInputRedirected) { $interactive = $false } } catch { $interactive = $false }
    if ($SkipStart) {
        Warn '-SkipStart：没起服务，这一步跳过（起来之后在面板「能力 → 起本机后端」里选）'
        $mode = 'skip'
    }
    if ($mode -eq 'ask' -and -not $interactive) {
        # 脚本化/CI：没人能贴配对串，**静默改用户的配对才是真错**（模块头第 2 条纪律）
        Info '控制台不是交互的 → 这一步跳过（要它就用 -Backend pair|local 明确给值）'
        $mode = 'skip'
    }
    if ($mode -eq 'ask') {
        Write-Host ''
        Write-Host '    会议转写要一台 GPU 机器。这台机器怎么用后端？' -ForegroundColor White
        Info '1) 用别人给的后端 —— 我有配对串（echo://pair?...）'
        Info '2) 本机自己跑 —— 从交付目录解压后端包（有这个包的 zip 才选它）'
        Info '3) 先不配 —— 以后在面板「能力」里弄'
        $ans = ''
        try { $ans = Read-Host '    选 1 / 2 / 3（回车 = 3）' } catch { }
        switch (('{0}' -f $ans).Trim()) {
            '1' { $mode = 'pair' }
            '2' { $mode = 'local' }
            default { $mode = 'skip' }
        }
    }
    if ($mode -eq 'pair' -and -not $BackendPair) {
        try { $BackendPair = Read-Host '    贴入配对串（echo://pair?host=…&code=…）' } catch { }
    }
    Log 'BACKEND' ("mode={0} dir={1} pair={2}" -f $mode, $Dir,
                   $(if ($BackendPair) { '有' } else { '无' }))

    if ($mode -eq 'pair') {
        $p = ConvertFrom-EchoPairString $BackendPair
        if (-not $p.Url) {
            Warn '没读到地址（配对串里应当有 host=…）—— 这一步跳过，去面板里弄'
            $mode = 'skip'
        } elseif (-not $p.Code) {
            # 只贴了一个 8 位码（`7K2M9QX4`）时最容易被当成"地址已读到、码没读到"——
            # 说清楚它缺的是**地址**，否则人会一直重贴同一张码。
            if ($p.Url -match '^[2-9A-HJ-NP-Z]{8}$') {
                Warn '你贴的是一张**配对码**，但还缺后端地址 —— 让管理员发**整串**（echo://pair?host=…&code=…），或在面板「能力」里分开填地址 + 码'
            } else {
                Warn '配对串里没有 code=… —— 让管理员重发一张（整串形如 echo://pair?host=…&code=…）'
            }
            $mode = 'skip'
        } else {
            $port = Get-EchoOwnPort
            $r = Invoke-EchoPair -Url $p.Url -Code $p.Code -Fp $p.Fp
            # 管理员那张串写的是 http、实际却是 HTTPS 反代（2026-10-21 现场）时，
            # 同一个地址再试一次 https —— 只升不降，401/403/429 不重试（码已用掉/被限流）。
            if (-not $r.ok -and $p.Url -match '^http://' -and $r.status -notin @(401, 403, 429)) {
                $alt = 'https://' + $p.Url.Substring(7)
                Info ("http 没成（{0}），同一个地址再试一次 https…" -f $r.Message)
                $r2 = Invoke-EchoPair -Url $alt -Code $p.Code -Fp $p.Fp
                if ($r2.ok) {
                    $p.Url = $alt
                    $r = $r2
                    Info '已按 https 配对成功 —— 管理员那张串写的是 http，实际是 HTTPS 反代（可以让他在管理面把对外地址写成 https://…）'
                } else {
                    # 两条都失败：把两边的真实原因都留下（只留一条会丢掉"到底是哪个协议不对"）。
                    $r = @{ ok = $false; status = $r2.status
                            message = ("http: {0}；https: {1}" -f $r.Message, $r2.Message) }
                }
            }
            if ($r.ok) {
                Ok ("已配对：{0}" -f $r.message)
                $script:BackendSummary = ("后端：已配对到 {0}" -f $p.Url)
                # 「允许音频去哪」是 none 时，**远端后端会被自己的许可挡下**（会议侧记
                # blocked-by-privacy，面板显示成"连不上"）。向导里也是这么办的：改成内网并说清。
                try {
                    $st = Invoke-RestMethod -Uri ("http://127.0.0.1:{0}/api/settings" -f $port) -TimeoutSec 20
                    # `GET /api/settings` 的形状是 `{settings: [{key,value,…}]}`，**不是扁平字典**。
                    # 以前这里读 `$st.capabilityPrivacy` 恒为 null —— 于是"改成内网"从来没生效过。
                    $row = @($st.settings | Where-Object { $_.key -eq 'capabilityPrivacy' }) | Select-Object -First 1
                    if ($row -and ('{0}' -f $row.value) -eq 'none') {
                        # PUT 必须包一层 `values`（`SettingsIn`），否则是 422。
                        $put = @{ values = @{ capabilityPrivacy = 'lan' } } | ConvertTo-Json -Depth 4
                        $null = Invoke-RestMethod -Uri ("http://127.0.0.1:{0}/api/settings" -f $port) `
                                -Method Put -ContentType 'application/json; charset=utf-8' `
                                -Body ([System.Text.Encoding]::UTF8.GetBytes($put)) -TimeoutSec 30
                        Info '「允许音频去哪」原来是「不出机」，已改成「内网」—— 不然刚配的远端后端会被挡下'
                    }
                } catch { Info ("（没读到/没写成「允许音频去哪」：{0}）" -f $_.Exception.Message) }
            } else {
                Warn ("配对没成功：{0}" -f $r.message)
                $hint = Get-PairFailureHint -Url $p.Url -Message $r.message -Status $r.status
                if ($hint) { Info $hint }
                $script:BackendSummary = ("后端：配对没成功（{0}）" -f $r.message)
            }
        }
    }

    if ($mode -eq 'local') {
        # 交付目录：显式给的 > 脚本自己那一层（松的"装我.cmd + 几个 zip"就是这个形态）
        $dirs = @()
        if ($Dir) { $dirs += $Dir }
        if ($PSScriptRoot) { $dirs += (Split-Path $PSScriptRoot -Parent) }
        $dirs += $script:KitRoot
        $dirs = @($dirs | Where-Object { $_ } | Select-Object -Unique)
        $thinPat = @('ECHO-backend-portable-*.zip', 'ECHO-backend-portable*.zip',
                     '*本机GPU后端包*.zip', '*后端包*.zip')
        $offPat = @('ECHO-backend-offline-*.zip', 'ECHO-backend-offline*.zip',
                    '*后端离线包*.zip', '*backend-offline*.zip')
        $off = ''
        foreach ($d in $dirs) { $off = Find-BackendZip -Dir $d -Patterns $offPat; if ($off) { break } }
        $thin = ''
        foreach ($d in $dirs) { $thin = Find-BackendZip -Dir $d -Patterns $thinPat; if ($thin) { break } }
        $backend = Join-Path $script:TargetRoot 'backend'
        New-Item -ItemType Directory -Force -Path $backend | Out-Null
        if ($off) {
            Info ("离线包：{0}" -f $off)
            $okz, $why = Expand-BackendPackage -Zip $off -Backend $backend
            if (-not $okz) { Warn ("离线包没能启用（{0}）" -f $why) }
            else {
                $usable, $py = Test-BackendRuntimeUsable -Backend $backend
                if ($usable) {
                    Ok ("后端运行时已就位（**零下载**）：{0}" -f $py)
                    # **解包≠能用**：还得把它起起来并等就绪 —— 否则用户装完立刻转写会撞
                    # 「没有可用的后端」（离线包这条路原来就缺这一下）。
                    if (Start-LocalBackendNow '离线包已启用，零下载') {
                        $script:BackendSummary = '后端：本机跑（离线包已启用，已起来并就绪）'
                    } else {
                        $script:BackendSummary = '后端：本机跑（离线包已启用，起来时没就绪 —— 面板看进度）'
                    }
                } else {
                    Warn ("离线包解开了，但运行时还不能用：{0}" -f $py)
                    # 运行时不可用有两类：包里缺东西 / 本机还差点依赖。**都让它自己补**：
                    # 服务端那一步会先试本机离线包、再按国内源补（`ensure_runtime`）。
                    if (Start-LocalBackendNow '离线包已解开，缺的由服务端补齐') {
                        $script:BackendSummary = '后端：本机跑（离线包 + 服务端补齐后已就绪）'
                    } else {
                        $script:BackendSummary = '后端：本机跑（离线包解开了，依赖还要补一次）'
                    }
                }
            }
        } elseif ($thin) {
            Info ("薄包：{0}" -f $thin)
            $okz, $why = Expand-BackendPackage -Zip $thin -Backend $backend
            if (-not $okz) { Warn ("薄包没能解开（{0}）" -f $why) }
            else {
                Ok '薄包已就位（自带解释器；依赖还没装）'
                # **触发下载**：这一下就是"没有离线包时才下载"（同一个入口，见 `Start-LocalBackendNow`）。
                if (Start-LocalBackendNow "薄包已就位，缺的运行时按国内源装（约 3 GB）") {
                    $script:BackendSummary = '后端：本机跑（已按国内源装好并起来）'
                } else {
                    $script:BackendSummary = '后端：本机跑（薄包已就位，运行时要面板点一下才装完）'
                }
            }
        } else {
            Warn ("交付目录里没有后端包（找过：{0}）" -f ($dirs -join '、'))
            Info '要给这台机器跑后端，把 `ECHO-backend-portable-*.zip`（薄包）或'
            Info '`ECHO-backend-offline-*.zip`（离线包，零下载）放在装我.cmd 旁边再跑一次'
            Info '（或者以后在面板「能力 → 起本机后端」里点，它会自己在本机找/按设置里的地址下）'
            $script:BackendSummary = '后端：没配（交付目录里没有后端包）'
        }
    }

    if ($mode -eq 'skip') {
        Info '后端这一步跳过 —— 以后随时可以在面板「能力」页签里配'
        if (-not $script:BackendSummary) { $script:BackendSummary = '后端：没配（面板里随时可配）' }
    }
    EndStep '后端'
}

# ---------------------------------------------------------------- 升级模式（只更新核心代码）
# 用户 2026-10-23 的口径：
#   * 升级 = **核心代码更新**（界面 + 核心功能）；
#   * 后端 / 手表配置、已有模型权重、智能体相关配置**都不要碰**；
#   * DSH 可以**问一句**要不要升级；不问就不动。
# 所以升级这条路**不写任何设置项**、不下载/替换模型、不碰后端与后端配对，
# 也不改任何 agent 配置（agentBackend / agentHarnessEnabled / harnessCommand）。

function Get-AppVersion([string]$Dir) {
    $mf = Join-Path $Dir 'manifest.json'
    if (-not (Test-Path -LiteralPath $mf)) { return '' }
    try {
        $j = Get-Content -LiteralPath $mf -Raw -Encoding UTF8 | ConvertFrom-Json
        return [string]$j.appVersion
    } catch { return '' }
}

function Show-UpgradePlan {
    Step '升级：只更新核心代码（数据 / 配置 / 模型 / 后端都不动）'
    $old = Get-AppVersion $script:CoreDir
    $new = Get-AppVersion $script:Tree
    if (-not $old) { $old = '未知' }
    if (-not $new) { $new = '未知' }
    $script:UpgradeFromVersion = $old
    $script:UpgradeToVersion = $new
    Info ("代码目录：{0}" -f $script:CoreDir)
    Info ("版本：{0} → {1}" -f $old, $new)
    if ($old -ne '未知' -and $new -ne '未知' -and $old -eq $new) {
        Info '版本相同：仍会重新覆盖代码（可用于修复被改坏的安装）'
    }
    Info '会更新：app / web / sidebar / scripts 等程序代码（界面与核心功能）'
    Info '会保留：数据库与全部设置、会议与文字稿、已有模型权重、后端地址与配对、手机/手表配置、智能体配置'
    Info '不会做：不下载/不替换模型、不重新配对后端、不写任何设置项、不改桌面与开机自启'
    EndStep '升级计划'
}

function Stop-EchoForUpgrade {
    Step '升级：先停 ECHO 与边条（否则代码文件被占用）'
    # 只认**这棵树自己的**端口文件：`Get-EchoPort` 在文件缺失时会兜底 8970，
    # 那可能正好是别的树的实例 —— 升级绝不能顺手停别人的服务。
    $portFile = Join-Path $script:TargetRoot 'data\echo-port.txt'
    $port = 0
    if (Test-Path -LiteralPath $portFile) {
        try { $port = [int]((Get-Content -LiteralPath $portFile -Raw).Trim()) } catch { $port = 0 }
    }
    $apiUp = ($port -gt 0 -and (Test-EchoApi $port))
    if ($apiUp) {
        try {
            $st = Invoke-RestMethod -Uri ("http://127.0.0.1:{0}/api/status" -f $port) -TimeoutSec 10
            if ($st.meeting -and $st.meeting.active) {
                Err '这场会正在录音 —— 不能升级。先结束录音再跑一次（音频与文字稿都在，不会丢）。'
                exit 1
            }
            if ($st.busy) { Err '有命令正在处理中 —— 稍后再升级。'; exit 1 }
        } catch {
            Warn ("读不到 /api/status（{0}）—— 继续尝试正常停止" -f $_.Exception.Message)
        }
        try {
            $r = Invoke-RestMethod -Uri ("http://127.0.0.1:{0}/api/control/echo/stop" -f $port) `
                                   -Method Post -TimeoutSec 10
            if (-not $r.ok) {
                # 这条路径自己也会拦"在录音/有命令"；把服务端原话带出来，别自己编原因。
                Err ("ECHO 拒绝停止：{0}" -f $r.message)
                exit 1
            }
            Info '已请求 ECHO 正常停止（它会先请边条退出）'
        } catch {
            # 不要退回 stop.ps1：旧装的 stop.ps1 不认 -KeepBackend，会**顺手停掉后端**
            # （用户口径：后端不要碰）；而且未知参数会让"停服务"这一步直接失败。
            # 宁可如实停下，让人手动关：没停掉就覆盖代码 = 半升级。
            Err ("调 /api/control/echo/stop 失败：{0}" -f $_.Exception.Message)
            Err '没有停掉 ECHO 就不会覆盖代码。请手动关闭 ECHO（面板「关闭」或托盘退出）后重跑一次。'
            exit 1
        }
        for ($i = 0; $i -lt 40; $i++) {
            Start-Sleep -Milliseconds 500
            if (-not (Test-EchoApi $port)) { break }
        }
        if (Test-EchoApi $port) {
            Err ("ECHO 还在监听 {0} —— 没有停掉就不覆盖代码（会留下半升级的目录）。" -f $port)
            Err '请手动关掉 ECHO 后重跑；或先看 data\logs\echo-server.log 里它为什么没退。'
            exit 1
        }
    } else {
        Info 'ECHO 没在运行（只确认边条）'
    }

    # 边条是独立进程，会锁住 sidebar\bin\...\echo-sidebar.exe/.dll。它由 ECHO 的
    # `/api/control/echo/stop` 顺带请退；ECHO 本来没在跑时则要自己找一次。
    $needle = ($script:CoreDir.TrimEnd('\') + '\sidebar\').ToLower()
    $sidebars = @(Get-CimInstance Win32_Process -Filter "Name like 'echo-sidebar%'" -ErrorAction SilentlyContinue |
                  Where-Object { $_.CommandLine -and $_.CommandLine.ToLower().Contains($needle) })
    if ($sidebars.Count -gt 0) {
        # 单实例边条：它一定持有这条命名管道；发 quit 是它自己的正常退出路径。
        try {
            $pipe = New-Object System.IO.Pipes.NamedPipeClientStream('.', 'echo-sidebar-toggle',
                        [System.IO.Pipes.PipeDirection]::Out)
            $pipe.Connect(800)
            $writer = New-Object System.IO.StreamWriter($pipe)
            $writer.Write('quit'); $writer.Flush(); $writer.Dispose(); $pipe.Dispose()
            Info '已请边条退出'
        } catch {
            Warn ("通知边条退出失败（{0}）—— 等它自己退/必要时强制" -f $_.Exception.Message)
        }
        for ($i = 0; $i -lt 20; $i++) {
            Start-Sleep -Milliseconds 500
            $sidebars = @(Get-CimInstance Win32_Process -Filter "Name like 'echo-sidebar%'" -ErrorAction SilentlyContinue |
                          Where-Object { $_.CommandLine -and $_.CommandLine.ToLower().Contains($needle) })
            if ($sidebars.Count -eq 0) { break }
        }
        if ($sidebars.Count -gt 0) {
            Warn '边条没有自己退出 —— 只强制结束**本棵树**的边条（否则代码文件会被占用）'
            $sidebars | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
            Start-Sleep -Seconds 1
        }
    }
    Ok 'ECHO 与边条已停（后端保持运行）'
    EndStep '停止服务'
}

function Backup-UpgradeDatabase {
    $db = Join-Path $script:TargetRoot 'data\echo.db'
    if (-not (Test-Path -LiteralPath $db)) { return }
    try {
        $stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
        $dst = "$db.bak-before-upgrade-$stamp"
        Copy-Item -LiteralPath $db -Destination $dst -Force
        $script:UpgradeDbBackup = $dst
        Ok ("数据库已备份：{0}" -f $dst)
    } catch {
        # 备份失败不挡升级（数据目录本身不会被覆盖），但必须让人看见。
        Warn ("数据库备份失败（{0}）—— 数据目录不会被覆盖，先继续" -f $_.Exception.Message)
    }
}

function Ask-UpgradeDsh {
    # 只有"已装过 DSH"才值得问；默认**不升级**（回车 = 不）。
    $hasDsh = (Test-Path (Join-Path $script:TargetRoot 'dsh\app')) -or
              (Test-Path (Join-Path $script:TargetRoot 'dsh\home\settings.yaml')) -or
              (Test-Path (Join-Path $script:TargetRoot 'data\harness'))
    if (-not $hasDsh) { return $false }
    $interactive = $true
    try { if ([Console]::IsInputRedirected) { $interactive = $false } } catch { $interactive = $false }
    if (-not $interactive -or $Yes) {
        Info '非交互/无人值守：不升级 DSH（要它请加 -Agent harness 或手动跑安装技能）'
        return $false
    }
    Write-Host ''
    Write-Host '    检测到已安装 DSH 标准版（智能体）。' -ForegroundColor White
    Info '升级只会替换 DSH 的程序文件；智能体相关的配置（选哪个智能体 / harness 命令等）一个字不改。'
    $ans = ''
    try { $ans = Read-Host '    要顺便升级 DSH 标准版（harness）吗？(y/N)' } catch { }
    return (('{0}' -f $ans).Trim() -match '^(y|yes|是)$')
}

# ---------------------------------------------------------------- 入口
Write-Host ''
Write-Host '  ============================================' -ForegroundColor Cyan
function Invoke-InstallModeProbe {
    # 2026-10-11（用户口径）：**安装器负责分辨"全新安装"还是"升级安装"**，并写下来。
    # 为什么必须由安装器决定：全新装完、服务一起来几秒内就会建库写设置，所以事后再去
    # "看数据目录空不空"分不出这两者。面板读 <数据根>\install-mode.json 决定要不要进
    # 「首次启用向导」（fresh）还是直接进工作状态（upgrade）。
    Step '安装模式探测（全新 / 升级）'
    $root = $script:TargetRoot
    $data = Join-Path $root 'data'
    $hasDsh = $false
    foreach ($p in @((Join-Path $root 'dsh\home'), (Join-Path $data 'harness'))) {
        foreach ($n in @('settings.yaml', '.credentials.yaml', 'skills', 'sessions')) {
            if (Test-Path (Join-Path $p $n)) { $hasDsh = $true }
        }
    }
    $db = Join-Path $data 'echo.db'
    $hasDb = Test-Path $db
    $meetings = 0
    foreach ($m in @((Join-Path $root 'meeting'), (Join-Path $data 'meetings'))) {
        if (Test-Path $m) {
            $meetings += @(Get-ChildItem $m -Directory -ErrorAction SilentlyContinue).Count
        }
    }
    $mode = if ($hasDsh -or $hasDb -or ($meetings -gt 0)) { 'upgrade' } else { 'fresh' }
    if ($hasDsh) { Info '已有 DSH 家目录 —— 智能体那一步会按「已装好即跳过」处理（升级 DSH 需显式指定）' }
    if ($hasDb) { Info ('已有数据库 {0} —— 配置与业务数据都会继承（不删、不覆盖）' -f $db) }
    if ($meetings -gt 0) { Ok ('已有 {0} 场会议 —— 会议数据继承' -f $meetings) }
    $modeFile = Join-Path $data 'install-mode.json'
    try {
        New-Item -ItemType Directory -Force -Path $data | Out-Null
        $json = ([ordered]@{ mode = $mode; note = ("dsh={0} db={1} meetings={2}" -f $hasDsh, $hasDb, $meetings)
                             at = (Get-Date -Format 'yyyy-MM-dd HH:mm:ss') } | ConvertTo-Json)
        # **不写 BOM**：PS 5.1 的 `Set-Content -Encoding UTF8` 会加 BOM，而 ECHO 那边读的是
        # 严格 utf-8（虽然也做了 utf-8-sig 兜底，两个方向都堵上更稳）。
        [System.IO.File]::WriteAllText($modeFile, $json, (New-Object System.Text.UTF8Encoding($false)))
        Ok ('安装模式 = {0}（已写 {1}）' -f $mode, $modeFile)
    } catch {
        Warn ('写安装模式失败（不影响安装）：{0}' -f $_.Exception.Message)
    }
    EndStep '安装模式'
    return $mode
}

function Invoke-SkillsSetup {
    # 2026-10-11（用户指出的坑）：随包技能在 <代码目录>\.dsh\skills\，而 agent 是从
    # **DSH_HOME\skills\** 读技能 —— 两个不同的目录，所以新装的机器上连随包的
    # meeting-record 都没生效。交给 ECHO 自己搬（它**只补缺、绝不覆盖**用户已有的同名技能）。
    # 2026-10-16（用户要求把"专有名词纠错机制"内置）：同一步还写**家目录与会议工作区的
    # AGENTS.md**（机制写在里面，agent 每个会话自动加载）—— 同样只补缺，用户自己那份一个字不动。
    Step '装随包技能与家目录说明（AGENTS.md）'
    $port = Get-EchoOwnPort
    if (-not $port) { Warn '没拿到面板端口 —— 这一步跳过；装完在面板里也能补'; EndStep '技能'; return }
    try {
        $r = Invoke-RestMethod -Uri ("http://127.0.0.1:{0}/api/skills/setup" -f $port) `
                               -Method Post -ContentType 'application/json' -Body '{}' -TimeoutSec 60
        if (@($r.installed).Count) { Ok ('已装 {0} 项：{1}' -f @($r.installed).Count, (@($r.installed) -join ' / ')) }
        if (@($r.skipped).Count) { Info ('已有自己的版本，保留不动：{0}' -f (@($r.skipped) -join ' / ')) }
        if (@($r.absent).Count) { Warn ('包里缺这些技能（包没打全）：{0}' -f (@($r.absent) -join ' / ')) }
        if (@($r.errors).Count) { Warn ('有几项没装成：{0}' -f (@($r.errors) -join '；')) }
        # 说明文件那一段（老 echo-core 没有 files 这一块 → 整段跳过，别报成失败）
        if ($r.files) {
            if (@($r.files.installed).Count) { Ok ('已写入说明文件 {0} 项：{1}' -f @($r.files.installed).Count, (@($r.files.installed) -join ' / ')) }
            if (@($r.files.updated).Count) { Ok ('说明文件已升到内置默认：{0}' -f (@($r.files.updated) -join ' / ')) }
            if (@($r.files.kept).Count) { Info ('已有你自己的说明文件，保留不动：{0}' -f (@($r.files.kept) -join ' / ')) }
            if (@($r.files.absent).Count) { Warn ('包里缺说明文件模板（包没打全）：{0}' -f (@($r.files.absent) -join ' / ')) }
            if (@($r.files.errors).Count) { Warn ('说明文件没写全：{0}' -f (@($r.files.errors) -join '；')) }
        }
    } catch {
        Warn ('装随包技能失败：{0}（面板 → 能力 里能看到，也能补）' -f $_.Exception.Message)
    }
    EndStep '技能'
}

Write-Host '   ECHO 个人语音助理 - 一键安装（快路，不绕 agent）' -ForegroundColor White
Write-Host ("   模式: {0}   档位: {1}" -f $(if ($Offline) { '离线（不联网）' } else { '在线' }), $Profile) -ForegroundColor Gray
Write-Host '  ============================================' -ForegroundColor Cyan
Log 'BEGIN' ("install-all.ps1 Profile={0} Offline={1} Agent={2} Yes={3}" -f $Profile, $Offline, $Agent, $Yes)

# 参数归一：Agent 离线默认 none（离线包里不带、也不许带 DSH —— 私有 npm 包不能分发）
if (-not $Agent) { $Agent = if ($Offline) { 'none' } else { 'harness' } }
# 2026-10-31 用户拍板：客户端默认档不装任何本地 STT 模型/引擎，语音唤醒取消。
# -Engines / -Wake / -NoWake / -Diarize / -Profile 两个档位都只为兼容老脚本保留，
# 传了也不生效（明确警告，不静默）。
$engineList = @()
$wantWake = $false
if (@($Engines).Count -gt 0 -or $Wake -or $NoWake.IsPresent -or $Diarize) {
    Warn '客户端已不再安装本地 STT 引擎/唤醒/分离组件（2026-10-31 起转写全走能力后端）——这些参数已忽略'
}
if ($Diarize) { Warn '会议说话人分离由能力后端完成：不需要在客户端装 pyannote' }
# 步号只为让人看得懂进度：离线那条路多三步（运行时/核心依赖/模型），后端那一步两边都有；
# 2026-10-11 又多了两步：安装模式探测（开头）+ 装随包技能（服务起来之后）。
$script:TotalSteps = if ($Offline) { 11 } else { 8 }

Resolve-KitLayout

# **装之前**就把"这是全新还是升级"定下来并写盘（事后分不出来：新装完几秒就有库与设置了）。
$installMode = Invoke-InstallModeProbe
$script:IsUpgrade = ($installMode -eq 'upgrade')
$upgradeDsh = $false
if ($script:IsUpgrade) {
    # 升级：代码更新 + 停/起服务；数据、设置、模型、后端、手表、智能体配置都不碰。
    $script:TotalSteps = 10
    Show-UpgradePlan
    Stop-EchoForUpgrade
    Backup-UpgradeDatabase
    # 升级不装/不换模型，也不装依赖（引擎列表清空 → 组件脚本不会去写 sttModel）。
    $engineList = @()
    $wantWake = $false   # historical: client has no wake/KWS any more
    # 升级只问"要不要升 DSH"，不再问"已有 DSH 目录"（外部目录只用于全新安装）。
    $DshDir = ''
    $AskDsh = $false
    $upgradeDsh = $false
    if (Ask-UpgradeDsh) {
        $Agent = 'harness'
        $upgradeDsh = $true
        Info 'DSH 标准版：本次会升级（只换程序文件，不改任何智能体配置）'
    } else {
        $Agent = 'none'
        Info 'DSH 标准版：保持现状（不升级、不检查、不改配置）'
    }
}

# 离线：先备好运行时/依赖/模型，再让 install.ps1 走 -SkipRuntime 那条路
# ⚠️ 升级**不走这一段**：已有运行时/依赖/模型权重一律不动。
$rcPy = ''
if ($Offline -and -not $script:IsUpgrade) {
    $rcPy = Build-OfflineRuntime
    Install-CoreDepsOffline $rcPy
    Copy-BundleModels
    # 2026-10-31：客户端不装任何本地 STT 模型 —— bundle 里没有 models\ 是正常的，
    # 不再按引擎过滤模型、也不再检查唤醒词 KWS 权重。
}

Install-TreeAndShortcuts

# **先起我们自己的实例、把端口钉死**，再让组件脚本干活 —— 否则它会连到别人的 ECHO
# （端口推断顺序见 Start-EchoOwnService 的说明）。
# 升级时一定重新拉起：刚才为了覆盖代码把它停了，不能把用户晾在"服务没起"的状态。
if (-not $SkipStart -or $script:IsUpgrade) { $null = Start-EchoOwnService }
else { Warn '-SkipStart：不起服务（组件那一步会自己起，端口按安装根的 data\echo-port.txt）'; Remove-Item env:ECHO_PORT -ErrorAction SilentlyContinue }

# 服务起来了才能问它"技能装到哪" —— 随包技能必须落到 DSH 家目录的 skills\ 下才生效
# （它们跟着代码树走，两者不是同一个目录，见 app/skills_setup.py 的说明）。
if (-not $SkipStart -or $script:IsUpgrade) { Invoke-SkillsSetup }

# 离线：组件脚本自己也会 `pip install`（引擎依赖）。它的命令行里不会带 --no-index，
# 所以这里把 pip 的**环境变量**钉死 —— pip 认 PIP_NO_INDEX / PIP_FIND_LINKS，
# 效果与 `--no-index --find-links <wheels>` 完全一样（我们自己的核心依赖那一步
# 是显式带命令行长参数的，日志里看得见）。
if ($Offline -and -not $script:IsUpgrade) {
    $env:PIP_NO_INDEX = '1'
    $env:PIP_FIND_LINKS = (Join-Path $script:Bundle 'wheels')
    $env:PIP_DISABLE_PIP_VERSION_CHECK = '1'
    Info ("离线引脚已设置：PIP_NO_INDEX=1  PIP_FIND_LINKS={0}" -f $env:PIP_FIND_LINKS)
    Log 'EXEC' ("PIP_NO_INDEX=1 PIP_FIND_LINKS={0}" -f $env:PIP_FIND_LINKS)
}

if ($script:IsUpgrade) {
    # 升级：**不写任何设置、不装依赖、不碰模型**，只跑自检（组件脚本里的 -KeepSettings）。
    # 引擎列表为空 → 组件脚本不会去写 sttModel / meetingSttModel，也不会下模型。
    $compExit = Invoke-ComponentsScript -EngineList @() `
                    -KeepSettings -SkipPip -SkipModels -UpgradeDsh:$upgradeDsh
} else {
    $compExit = Invoke-ComponentsScript -EngineList @()
}
# 「后端怎么来」排在组件之后、收尾之前：此刻服务已经起来了（`$env:ECHO_PORT` 钉死），
# 配对与「起本机后端」都能立刻生效；结果再由 `Show-Result` 一起打进摘要。
# ⚠️ 这一步**不许改退出码**：后端是可选项，装客户端本身是好的（`Invoke-BackendStep` 里
# 只 Warn/Info + 写 `$script:BackendSummary`）。
if ($script:IsUpgrade) {
    # 升级**不问后端、不碰后端**（用户口径：后端配置不要碰）。
    Step '后端：保持现有配置（未改动）'
    try {
        $bp = Get-EchoOwnPort
        if ($bp) {
            $cap = Invoke-RestMethod -Uri ("http://127.0.0.1:{0}/api/capability" -f $bp) -TimeoutSec 20
            $echo = @($cap.backends | Where-Object { $_.name -eq 'echo-server' }) | Select-Object -First 1
            if ($echo) { Info ("现有后端：{0}（ready={1}）" -f $echo.name, $echo.ready) }
        }
    } catch { }
    Info '后端地址、配对与「允许音频去哪」都保持原样；不重新解包、不重新配对。'
    $script:BackendSummary = '后端：保持现有配置（未改动）'
    EndStep '后端'
} else {
    Invoke-BackendStep -Dir $BackendDir
}
Show-Result -ComponentsExit $compExit
if ($compExit -ne 0) { exit $compExit }
exit 0
