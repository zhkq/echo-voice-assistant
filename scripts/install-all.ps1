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
#   # 在线快路（默认 minimal：只要 sherpa，装完立刻能语音指令/转写）
#   powershell -NoProfile -ExecutionPolicy Bypass -File <kit>\ECHO\scripts\install-all.ps1 -Yes
#
#   # 离线包（bundle\wheels + bundle\models 随包，全程不联网）
#   powershell -NoProfile -ExecutionPolicy Bypass -File <kit>\ECHO\scripts\install-all.ps1 `
#       -Offline -Yes -Agent none
#
#   # 办公本档（sherpa + 唤醒词），智能体走标准版 harness
#   ... -Profile main -Agent harness -Root D:\ECHO
#
# 参数
# ----
#   -Profile minimal|main    minimal=只要 sherpa（最小实例）；main 再加唤醒词 KWS
#                            （对应 components\profiles.json 的默认档）
#   -Root <安装根>           数据/模型/运行时都在它下面，代码进 <根>\echo-core。
#                            默认 D:\ECHO（没有 D 盘就 C:\ECHO）
#   -Agent none|harness|dsh  智能体。**离线默认 none**：离线包里不带 DSH/harness
#                            （@deepseek-ai/dsh 是私有 npm 包，许可上不随包分发）
#   -Offline                 离线模式：依赖只从 bundle\wheels 装（pip 带
#                            --no-index --find-links），模型直接用 bundle\models 里的
#   -Yes                     无人值守：不提问、全默认
#   -KitRoot <目录>          资料夹根（里面有 ECHO\、echo-install\、bundle\）；
#                            默认按本脚本位置推（<kit>\ECHO\scripts\install-all.ps1）
#   -BundleDir <目录>        离线载荷目录（默认 <KitRoot>\bundle）
#   -Engines <列表>          覆盖档位的引擎，例如 -Engines sherpa,whisper-base
#   -Wake / -NoWake          唤醒词 KWS（main 档默认开；离线包里没有 KWS 模型时自动跳过）
#   -Diarize                 说话人分离（重依赖 + 权重不随包，必然要联网）
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
    [switch]$SkipStart
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
        $script:Tree = Join-Path $script:KitRoot 'ECHO'
        if (-not (Test-Path (Join-Path $script:Tree 'app\main.py'))) {
            # 兼容 -KitRoot 直接指到主程序目录（有人把 -KitRoot 当成 -Tree 用）
            if (Test-Path (Join-Path $script:KitRoot 'app\main.py')) {
                $script:Tree = $script:KitRoot
                $script:KitRoot = Split-Path $script:Tree -Parent
            } else {
                Err ("-KitRoot 里既没有 ECHO\ 也没有 app\main.py：{0}" -f $KitRoot); exit 1
            }
        }
    } else {
        # 本脚本在 <kit>\ECHO\scripts\ 里（主包白名单带 scripts/），父目录就是主程序目录
        $cand = Split-Path $here -Parent                     # <kit>\ECHO 或仓库根
        if (Test-Path (Join-Path $cand 'app\main.py')) {
            $script:Tree = $cand
            $up = Split-Path $cand -Parent
            if ((Split-Path $cand -Leaf) -ieq 'ECHO') { $script:KitRoot = $up }
        }
    }
    if (-not $script:Tree) {
        $cand = Split-Path $here -Parent
        Err ("没找到 ECHO 主程序（找过 {0}\app\main.py）。用 -KitRoot <资料夹> 指定。" -f $cand)
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
    $r = $Root -replace '/', '\' -replace '\\+$', ''
    if ($r -notmatch '^[A-Za-z]:[\\/]') { Err ("安装根必须是盘符开头的绝对路径：{0}" -f $Root); exit 1 }
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

function Build-OfflineRuntime {
    # 离线建运行时：三级降级，**每一级都不联网**。
    #   ① 已经就绪（重跑） ② uv 的缓存 CPython（uv venv，离线可命中缓存）
    #   ③ 本机 Python 3.11（py / python，venv + ensurepip 自带 wheel）
    #   ④ 包里的 python.org 嵌入包 + get-pip.py
    Step '建运行时（离线：只看本机与包里的载荷）'
    $rcDir = Join-Path $script:TargetRoot 'runtime-core'
    $py = Test-RuntimePython $rcDir
    if ($py) {
        Ok ("runtime-core 已就绪: {0}" -f $py)
        if (-not (Assert-Pip $py)) { Err '这个运行时没有可用的 pip'; exit 1 }
        EndStep '运行时'
        return $py
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
        foreach ($cand in @(@('py', @('-3.11')), @('python', @()), @('python3', @()))) {
            $exe = Get-Command $cand[0] -ErrorAction SilentlyContinue
            if (-not $exe) { continue }
            Info ("② 用 {0} 建 venv（本机 Python，不联网）..." -f $cand[0])
            $r = Invoke-Native $exe.Source ($cand[1] + @('-m', 'venv', $rcDir))
            if ($r.out) { Write-Host ("      " + $r.out) -ForegroundColor DarkGray }
            $made = Test-RuntimePython $rcDir
            if ($made) { break }
        }
    }
    if (-not $made) {
        Info '③ 用包里的 python.org 嵌入包（不联网）...'
        $made = Install-EmbeddedFromBundle $rcDir
    }
    if (-not $made) {
        Err '离线建不出运行时：本机没有 uv 缓存、没有 Python 3.11，包里也没有 runtime\python-3.11.9-embed-amd64.zip'
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
        Err ("  多半是 bundle\wheels 里缺 wheel；看上面的 `"No matching distribution`" 是哪一行")
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
    if (-not (Test-Path $src)) { Warn '离线载荷里没有 models\ —— 模型要在面板里自己下（需要网络）'; EndStep '模型'; return }
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
    if ($NoShortcuts) { $argList += @('-SkipDesktopLnk', '-SkipStartupLnk') }
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
    param([string[]]$EngineList, [bool]$WantWake)
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
    $argv = @{ DestDir = $script:TargetRoot; Engines = $EngineList; Agent = $Agent }
    if ($WantWake) { $argv['Wake'] = $true }
    if ($Diarize) { $argv['Diarize'] = $true }
    if ($NotesDir) { $argv['NotesDir'] = $NotesDir }
    if ($PipIndex -and -not $Offline) { $argv['PipIndex'] = $PipIndex }
    $shown = ($EngineList -join ',')
    Info ("& {0} -DestDir {1} -Engines {2} -Agent {3}{4}{5}" -f `
          $comp, $script:TargetRoot, $shown, $Agent,
          $(if ($WantWake) { ' -Wake' } else { '' }), $(if ($Diarize) { ' -Diarize' } else { '' }))
    Log 'EXEC' ("& {0} -DestDir {1} -Engines {2} -Agent {3} -Wake={4} -Diarize={5}" -f `
                $comp, $script:TargetRoot, $shown, $Agent, $WantWake, $Diarize)
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
            $m = Invoke-WebRequest -Uri ("http://127.0.0.1:{0}/api/models" -f $port) -UseBasicParsing -TimeoutSec 5
            $mb = if ($m.RawContentStream) { $m.RawContentStream.ToArray() } else { @() }
            $models = ([System.Text.Encoding]::UTF8.GetString($mb) | ConvertFrom-Json)
            foreach ($k in @('sherpa', 'kws', 'whisper-base')) {
                $it = $null
                if ($models.items -and $models.items.PSObject.Properties.Name -contains $k) { $it = $models.items.$k }
                if ($it) { Info ("模型 {0,-12} ready={1}" -f $k, $it.ready) }
            }
        } catch { }
    } else {
        Warn ("服务没起来（或 -SkipStart）—— 面板地址（起来后）: {0}" -f $url)
        Info ("手动启动：powershell -NoProfile -ExecutionPolicy Bypass -File `"{0}\scripts\start.ps1`"" -f $script:CoreDir)
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
        Write-Host '  全部就绪。' -ForegroundColor Green
    } else {
        Write-Host ("  装完了，但组件那一步有没就绪的项（退出码 {0}）—— 上面列了是哪几项。" -f $ComponentsExit) -ForegroundColor Yellow
    }
}

# ---------------------------------------------------------------- 入口
Write-Host ''
Write-Host '  ============================================' -ForegroundColor Cyan
Write-Host '   ECHO 个人语音助理 - 一键安装（快路，不绕 agent）' -ForegroundColor White
Write-Host ("   模式: {0}   档位: {1}" -f $(if ($Offline) { '离线（不联网）' } else { '在线' }), $Profile) -ForegroundColor Gray
Write-Host '  ============================================' -ForegroundColor Cyan
Log 'BEGIN' ("install-all.ps1 Profile={0} Offline={1} Agent={2} Yes={3}" -f $Profile, $Offline, $Agent, $Yes)

# 参数归一：Agent 离线默认 none（离线包里不带、也不许带 DSH —— 私有 npm 包不能分发）
if (-not $Agent) { $Agent = if ($Offline) { 'none' } else { 'harness' } }
# 引擎 / 唤醒词（档位）
if ($Engines.Count -eq 0) { $Engines = @('sherpa') }
$engineList = @($Engines | Where-Object { $_ } | ForEach-Object { $_.Trim() } | Where-Object { $_ })
$wantWake = (($Profile -eq 'main') -or $Wake) -and -not $NoWake
# 步号只为让人看得懂进度：离线那条路多三步（运行时/核心依赖/模型）
$script:TotalSteps = if ($Offline) { 8 } else { 5 }

Resolve-KitLayout

# 离线：先备好运行时/依赖/模型，再让 install.ps1 走 -SkipRuntime 那条路
$rcPy = ''
if ($Offline) {
    $rcPy = Build-OfflineRuntime
    Install-CoreDepsOffline $rcPy
    Copy-BundleModels
    # 离线不许"顺手去下模型"：包里没有模型的引擎直接摘掉并说清楚
    $modelFor = @{
        'sherpa' = 'sherpa-onnx-streaming'; 'whisper-tiny' = 'faster-whisper\tiny'
        'whisper-base' = 'faster-whisper\base'; 'whisper-small' = 'faster-whisper\small'
        'whisper-medium' = 'faster-whisper\medium'; 'whisper-large-v3' = 'faster-whisper\large-v3'
        'sensevoice' = 'sensevoice'; 'qwen3asr' = 'qwen3asr'
    }
    $kept = @()
    foreach ($e in $engineList) {
        $rel = $modelFor[$e]
        if ($rel -and (Test-Path (Join-Path (Join-Path $script:Bundle 'models') $rel))) { $kept += $e }
        else { Warn ("离线包里没有 {0} 的模型 —— 这一档跳过（要它就联网装：去掉 -Offline）" -f $e) }
    }
    if ($kept.Count -eq 0) { $kept = @('sherpa') }   # 兜底：sherpa 是必装档
    $engineList = $kept
    if ($wantWake -and -not (Test-Path (Join-Path $script:Bundle 'models\wakeword'))) {
        Warn '离线包里没有唤醒词模型（bundle\models\wakeword）—— 唤醒词跳过'
        $wantWake = $false
    }
    if ($Diarize) { Warn '-Diarize 的权重不随包分发（pyannote）—— 离线装不了，这一步会失败' }
}

Install-TreeAndShortcuts

# **先起我们自己的实例、把端口钉死**，再让组件脚本干活 —— 否则它会连到别人的 ECHO
# （端口推断顺序见 Start-EchoOwnService 的说明）。
if (-not $SkipStart) { $null = Start-EchoOwnService }
else { Warn '-SkipStart：不起服务（组件那一步会自己起，端口按安装根的 data\echo-port.txt）'; Remove-Item env:ECHO_PORT -ErrorAction SilentlyContinue }

# 离线：组件脚本自己也会 `pip install`（引擎依赖）。它的命令行里不会带 --no-index，
# 所以这里把 pip 的**环境变量**钉死 —— pip 认 PIP_NO_INDEX / PIP_FIND_LINKS，
# 效果与 `--no-index --find-links <wheels>` 完全一样（我们自己的核心依赖那一步
# 是显式带命令行长参数的，日志里看得见）。
if ($Offline) {
    $env:PIP_NO_INDEX = '1'
    $env:PIP_FIND_LINKS = (Join-Path $script:Bundle 'wheels')
    $env:PIP_DISABLE_PIP_VERSION_CHECK = '1'
    Info ("离线引脚已设置：PIP_NO_INDEX=1  PIP_FIND_LINKS={0}" -f $env:PIP_FIND_LINKS)
    Log 'EXEC' ("PIP_NO_INDEX=1 PIP_FIND_LINKS={0}" -f $env:PIP_FIND_LINKS)
}

$compExit = Invoke-ComponentsScript -EngineList $engineList -WantWake $wantWake
Show-Result -ComponentsExit $compExit
if ($compExit -ne 0) { exit $compExit }
exit 0
