# =====================================================================
# install.ps1 — ECHO 一键安装向导（同事版）
#
# 适用路线：**内部「整包」**（需要 ECHO-*.zip，内含预置 venv 与已下载模型）。
# 公开仓库**只提供从源码安装**（见 README）：整包不在仓库、也不进 Release ——
# 它内含单位内网信息，以及需要单独授权的模型权重，不适合公开分发。
#
# 场景 A（推荐，交付 zip）：把本脚本 + install.bat 与
#   ECHO-交付包-YYYYMMDD.zip 放在同一文件夹，双击 install.bat 即可：
#   自动发现 zip → 选择安装目录 → 解压 → venv 修复校验 → 建库 →
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
#     -Silent             无人值守：全默认值，不询问
#     -DryRun             只模拟：打印将执行的动作，不写任何系统位置
#     -SkipSetup          跳过 venv 初始化（已初始化过的包）
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
    [switch]$Silent,
    [switch]$DryRun,
    [switch]$SkipSetup,
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
    $parentOfScripts = Split-Path $PSScriptRoot -Parent
    if (Test-Path (Join-Path $parentOfScripts 'venv\Scripts\python.exe')) {
        $script:ExistingDir = $parentOfScripts
        Ok ("检测到已有 ECHO 目录: {0}" -f $script:ExistingDir)
        return
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
    # 探测 zip 顶层目录
    $r = Invoke-Native 'tar' @('-tf', $script:ZipPath)
    $firstLine = ($r.out -split "`r?`n" | Where-Object { $_.Trim() } | Select-Object -First 1)
    if ($firstLine) {
        $top = ($firstLine -split '[\\/]')[0]
        if ($top) { $script:ZipTop = $top }
    }
    Info ("zip 顶层目录: {0}（期望 ECHO）" -f $script:ZipTop)
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
        Info "(模拟) tar -xf {0} 到 {1}" -f $script:ZipPath, $script:TargetDir
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
        $topDir = Get-ChildItem $tmp -Directory | Select-Object -First 1
        if (-not $topDir) {
            Err 'zip 内未发现顶层目录（交付包结构异常）。'
            exit 1
        }
        # 顶层目录放入目标：
        #   目标不存在 / 为空 → 整体改名移动（同盘 rename，瞬间完成）
        #   目标非空（用户已确认）→ 顶层内容逐项移入
        $tItems = @(Get-ChildItem $script:TargetDir -Force -ErrorAction SilentlyContinue)
        if ($tItems.Count -eq 0) {
            Remove-Item $script:TargetDir -Force -ErrorAction SilentlyContinue
            Move-Item $topDir.FullName $script:TargetDir -Force
        } else {
            Get-ChildItem $topDir.FullName -Force | Move-Item -Destination $script:TargetDir -Force
            Remove-Item $topDir.FullName -Force -Recurse -ErrorAction SilentlyContinue
        }
        Remove-Item $tmp -Recurse -Force -ErrorAction SilentlyContinue
    } catch {
        Remove-Item $tmp -Recurse -Force -ErrorAction SilentlyContinue
        throw
    }
    if (-not (Test-Path (Join-Path $script:TargetDir 'venv\Scripts\python.exe'))) {
        Err '解压完成但未找到 venv\Scripts\python.exe，包可能不完整。'
        exit 1
    }
    Ok ("解压完成: {0}" -f $script:TargetDir)
    $script:ExistingDir = $script:TargetDir
}

function Step05-Init {
    Step 5 'venv 初始化与建库（new-machine-setup.ps1）'
    $setup = Join-Path $script:ExistingDir 'scripts\new-machine-setup.ps1'
    if (-not (Test-Path $setup)) {
        Err ("未找到初始化脚本: {0}" -f $setup)
        if ($script:DryRun) { return }
        exit 1
    }
    if ($SkipSetup) {
        Warn '已跳过初始化（-SkipSetup）'
        return
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
    $venvPy = Join-Path $script:ExistingDir 'venv\Scripts\python.exe'
    if (Test-Path $venvPy) { Ok 'venv\Scripts\python.exe 存在' } else { Err '缺 venv\Scripts\python.exe'; $fail++ }
    $pyw = Join-Path $script:ExistingDir 'venv\Scripts\pythonw.exe'
    if (Test-Path $pyw) { Ok 'venv\Scripts\pythonw.exe 存在' } else { Warn '缺 pythonw.exe（启动将失败，建议重跑初始化）' }
    $start = Join-Path $script:ExistingDir 'scripts\start.ps1'
    if (Test-Path $start) { Ok 'scripts\start.ps1 存在' } else { Err '缺 scripts\start.ps1'; $fail++ }
    $web = Join-Path $script:ExistingDir 'web\index.html'
    if (Test-Path $web) { Ok 'web 面板存在' } else { Warn '缺 web\index.html' }
    # 端口占用检查
    $c = New-Object System.Net.Sockets.TcpClient
    try {
        $iar = $c.BeginConnect('127.0.0.1', 8970, $null, $null)
        if ($iar.AsyncWaitHandle.WaitOne(800)) {
            $c.EndConnect($iar)
            Warn '端口 8970 已被占用（本机可能已有 ECHO 在运行，不影响安装）'
        } else {
            Ok '端口 8970 空闲'
        }
    } catch { } finally { $c.Dispose() }
    if ($fail -gt 0) {
        Err ("自检失败项: {0} 项，安装可能不可用" -f $fail)
        if (-not (Ask-YesNo '继续？')) { exit 1 }
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
        Warn '跳过桌面快捷方式'
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
    $icon = Join-Path $root 'venv\Scripts\python.exe'
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
