# =====================================================================
#  导入.ps1 - 把「ECHO 真实会议测试夹具」导入目标机的 ECHO
#
#  为什么需要这个脚本（一句话）
#  --------------------------
#  ECHO **没有**「导入录音作为会议」这个功能：`/api/stt/transcribe` 只是单文件转写、
#  不走会议链路；面板的会议列表来自**数据库**（`GET /api/meetings`），而音频是按
#  「会议目录里的 01.wav / 02.wav …」读的。**所以只把文件夹拷进去，面板里不会出现这场会议。**
#  本脚本做两件事，缺一不可：
#    ① 把 wav + meta.json 放进**这台机器实际生效的**会议目录；
#    ② 用仓库自己的代码路径（`app/db.py` 的 create_meeting/update_meeting）建一条会议记录。
#
#  它不做什么
#  ----------
#   * 不写死任何路径：目标位置来自正在跑的 ECHO 的 `GET /api/paths/env`
#     （或回落：用本机解释器 import `app.paths` 现算）。新布局（<安装根>\meeting）与
#     老扁平布局（<数据根>\data\meetings）走的是**同一份**解析代码，脚本里没有硬编码目录名。
#   * 不改任何设置（不碰 capability*、不改 meetingsDir、不重启 ECHO）。
#   * 不碰其它会议：全程只操作包内那**一个**会议名。
#   * 幂等：重复跑不报错、不产生重复记录（会议表 name 有 UNIQUE，脚本也会先查）。
#
#  用法
#  ----
#    powershell -NoProfile -ExecutionPolicy Bypass -File .\导入.ps1
#    powershell ... -File .\导入.ps1 -Port 18060          # 面板不在默认端口时
#    powershell ... -File .\导入.ps1 -DryRun              # 只看会做什么
#    powershell ... -File .\导入.ps1 -Force               # 已存在也重刷（会清掉本场转写行）
#    powershell ... -File .\导入.ps1 -Remove -Yes         # 删掉这场测试会议（记录 + 音频目录）
#
#  退出码：0 = 已导入 / 已存在跳过 / 删除完成；1 = 失败（原因在最后一行）
#
#  面板端口的发现顺序（谁先答 GET /api/paths/env 就用谁）：
#      -Port  ->  $env:ECHO_PORT  ->  候选安装根下的 data\echo-port.txt  ->  8970
#  注意 `-Port` **不是隔离开关**：它只是"第一个被尝试的候选"；指定那个端口没在听时，
#  脚本会继续往下试（多实例的机器上很可能因此找到**另一个**实例）。要精确指定，
#  用 -Port 指对，并在必要时清掉 ECHO_PORT、或用 -DataRoot/-MeetingsRoot 直接钉死目标。
#
#  编码：含中文，**必须 UTF-8 带 BOM**（Windows PowerShell 5.1 否则按 ANSI 解析）。
# =====================================================================
[CmdletBinding()]
param(
    [string]$Name = '',           # 会议名（= 目录名）；留空自动用包里唯一的那个
    [string]$PackRoot = '',       # 包根目录（含 meeting\ 与 先读我.md）；留空 = 本脚本所在目录
    [string]$EchoRoot = '',       # ECHO 代码目录（含 app\）；留空自动发现
    [string]$Python = '',         # 解释器；留空按 <安装根>\runtime-core\python.exe 等顺序找
    [int]$Port = 0,               # 面板端口；留空按 ECHO_PORT -> data\echo-port.txt -> 8970
    [string]$DataRoot = '',       # 数据根（数据库所在目录）；一般不用填
    [string]$MeetingsRoot = '',   # 会议目录；一般不用填
    [switch]$Force,               # 已存在也重刷记录（并清掉本场转写行，便于再测一次）
    [switch]$DryRun,              # 只报告，不动文件也不动数据库
    [switch]$Remove,              # 删除这场测试会议（记录 + 音频目录）
    [switch]$Yes                  # 配合 -Remove：不再交互确认
)

$ErrorActionPreference = 'Stop'
$script:VERSION = '1.0'
$script:Py = ''
$script:DataRootActual = ''
$script:MeetingsRootActual = ''
$script:HelperMode = ''           # 'api' = 问了正在跑的 ECHO；'offline' = 自己 import 算

function Say  { param([string]$m) Write-Host $m }
function Step { param([string]$m) Write-Host ''; Write-Host ('== ' + $m + ' ==') -ForegroundColor Cyan }
function Ok   { param([string]$m) Write-Host ('  [OK] ' + $m) -ForegroundColor Green }
function Info { param([string]$m) Write-Host ('       ' + $m) }
function Warn { param([string]$m) Write-Host ('  [!]  ' + $m) -ForegroundColor Yellow }
function Die  { param([string]$m) Write-Host ''; Write-Host ('  [X]  ' + $m) -ForegroundColor Red; exit 1 }

# ---------------------------------------------------------------------
#  嵌入的 Python 助手（**只能 ASCII** —— 中文一律由本 .ps1 打印/传入）
#  职责：只做"数据"这一层 —— 读 meta.json、用 app/db.py 建/改/删会议记录、回一份 JSON。
#  它只 import app.db / app.paths（`app.meeting` 只在 -Remove 那一步按需 import），
#  也**不拼任何 SQL**：建记录走 db.create_meeting + db.update_meeting。
# ---------------------------------------------------------------------
$script:PY_SRC = @'
import json
import os
import re
import sys

MARK = "###FIXTURE-JSON###"


def _opt(args, key, default=""):
    if key in args:
        i = args.index(key)
        if i + 1 < len(args):
            return args[i + 1]
    return default


def _flag(args, key):
    return key in args


def _bind(data_root):
    """Point app.db at the target data root (the DB path is a module-level constant)."""
    from app import db
    if data_root:
        root = os.path.abspath(data_root)
        db.DATA_DIR = root
        db.DB_FILE = os.path.join(root, "echo.db")
    return db


def _read_meta(path):
    try:
        with open(path, "r", encoding="utf-8-sig") as fh:
            data = json.load(fh)
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _wavs(folder):
    try:
        names = os.listdir(folder)
    except OSError:
        return []
    return sorted(n for n in names if re.match(r"^\d+\.wav$", n, re.I))


def _ensure_schema(db):
    """Create/migrate only when the meetings table is missing (never write needlessly)."""
    try:
        db.get_meeting_by_name("__echo_fixture_probe__")
        return "ok"
    except Exception:
        pass
    db.init()
    db.get_meeting_by_name("__echo_fixture_probe__")
    return "migrated"


def cmd_probe(args):
    from app import paths
    db = _bind(_opt(args, "--data-root"))
    meetings = _opt(args, "--meetings-root") or paths.meetings_root()
    return {
        "dataRoot": db.DATA_DIR,
        "dbFile": db.DB_FILE,
        "dbExists": os.path.isfile(db.DB_FILE),
        "echoRoot": paths.echo_root(),
        "echoBase": paths.echo_base() or "",
        "meetingsRoot": meetings,
    }


def cmd_status(args):
    name = _opt(args, "--name")
    meetings = os.path.abspath(_opt(args, "--meetings-root"))
    folder = os.path.join(meetings, name)
    db = _bind(_opt(args, "--data-root"))
    row = None
    schema = "ok"
    try:
        row = db.get_meeting_by_name(name)
    except Exception:
        schema = "missing"
    lines = 0
    if row is not None:
        try:
            lines = len(db.get_lines(int(row["id"])))
        except Exception:
            lines = -1
    return {
        "schema": schema,
        "dbFile": db.DB_FILE,
        "dbExists": os.path.isfile(db.DB_FILE),
        "meetingRow": row is not None,
        "meetingId": int(row["id"]) if row is not None else 0,
        "status": (row.get("status") or "") if row is not None else "",
        "lines": lines,
        "folderExists": os.path.isdir(folder),
        "wavs": _wavs(folder),
        "meta": os.path.isfile(os.path.join(folder, "meta.json")),
        "transcript": os.path.isfile(os.path.join(folder, "transcript.md")),
        "summary": os.path.isfile(os.path.join(folder, "summary.md")),
    }


def cmd_apply(args):
    name = _opt(args, "--name")
    meetings = os.path.abspath(_opt(args, "--meetings-root"))
    folder = os.path.join(meetings, name)
    db = _bind(_opt(args, "--data-root"))
    _ensure_schema(db)
    meta = _read_meta(os.path.join(folder, "meta.json"))
    cfg = meta.get("config") or {}
    wavs = _wavs(folder)
    if not wavs:
        raise SystemExit("no audio segments (NN.wav) under %s" % folder)
    audio_bytes = 0
    for w in wavs:
        try:
            audio_bytes += os.path.getsize(os.path.join(folder, w))
        except OSError:
            pass
    row = db.get_meeting_by_name(name)
    action = "exists"
    if row is None:
        mid = db.create_meeting(
            name,
            started_at=str(meta.get("start") or ""),
            stt_model=str(cfg.get("sttModel") or "small"),
            stt_device=str(cfg.get("sttDevice") or "auto"),
            diarize=1 if cfg.get("diarize") else 0)
        action = "created"
    else:
        mid = int(row["id"])
    if action == "created" or _flag(args, "--force"):
        fields = {
            "ended_at": str(meta.get("end") or ""),
            "duration_seconds": float(meta.get("durationSeconds") or 0),
            "segments": len(wavs),
            "audio_bytes": int(audio_bytes),
            "status": "interrupted",
        }
        for key, opt in (("title", "--title"), ("notes", "--notes"), ("error", "--error")):
            val = _opt(args, opt)
            if val:
                fields[key] = val
        db.update_meeting(mid, **fields)
        if _flag(args, "--force"):
            db.clear_meeting_lines(mid)
    cur = db.get_meeting(mid) or {}
    return {
        "action": action,
        "id": mid,
        "name": cur.get("name") or name,
        "status": cur.get("status") or "",
        "durationSeconds": float(cur.get("duration_seconds") or 0),
        "segments": int(cur.get("segments") or 0),
        "audioBytes": int(cur.get("audio_bytes") or 0),
        "lines": len(db.get_lines(mid)),
        "wavs": wavs,
        "transcript": os.path.isfile(os.path.join(folder, "transcript.md")),
        "summary": os.path.isfile(os.path.join(folder, "summary.md")),
    }


def cmd_remove(args):
    name = _opt(args, "--name")
    meetings = os.path.abspath(_opt(args, "--meetings-root"))
    db = _bind(_opt(args, "--data-root"))
    _ensure_schema(db)
    row = db.get_meeting_by_name(name)
    out = {"name": name, "meetingRow": row is not None, "deleted": ""}
    if row is None:
        return out
    mid = int(row["id"])
    out["id"] = mid
    try:
        # app.meeting.delete_meeting = the product's own path (row + DSH session mapping).
        from app import meeting as meeting_mod
        ok, msg = meeting_mod.delete_meeting(mid)
        out["deleted"] = "db" if ok else "refused"
        out["message"] = str(msg or "")
    except Exception as exc:
        db.delete_meeting(mid)
        out["deleted"] = "db-only"
        out["message"] = "%s: %s" % (type(exc).__name__, exc)
    return out


def main():
    argv = sys.argv[1:]
    # The code root must be pushed into sys.path explicitly: this script can be run from
    # anywhere (Downloads dir, a USB stick...), and then `from app import db` would fail.
    # Insert at the very front so ECHO's own tree wins over a same-named app\ in the CWD.
    code_root = _opt(argv, "--code-root")
    if code_root:
        code_root = os.path.abspath(code_root)
        if code_root not in sys.path:
            sys.path.insert(0, code_root)
    mode = argv[0] if argv else ""
    args = argv[1:]
    table = {"probe": cmd_probe, "status": cmd_status, "apply": cmd_apply, "remove": cmd_remove}
    if mode not in table:
        sys.stderr.write("unknown mode: %s\n" % mode)
        return 2
    try:
        out = table[mode](args)
    except SystemExit as exc:
        sys.stderr.write("%s\n" % exc)
        return 3
    except Exception:
        import traceback
        traceback.print_exc()
        return 4
    sys.stdout.write(MARK + "\n")
    sys.stdout.write(json.dumps(out, ensure_ascii=True, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
'@

# ---------------------------------------------------------------------
#  小工具
# ---------------------------------------------------------------------

#  跑一次助手（程序经 stdin 喂给 python，**不落临时文件**），返回解析后的对象
function Invoke-Helper {
    param([string]$Mode, [string[]]$Extra = @())
    if ($script:PY_SRC -match '[^\x00-\x7F]') {
        Die '内部错误：嵌入的 Python 助手含非 ASCII 字符（请报告这个包）'
    }
    $cli = @('-', $Mode) + $Extra
    if ($script:DataRootActual) { $cli += @('--data-root', $script:DataRootActual) }
    if ($script:MeetingsRootActual) { $cli += @('--meetings-root', $script:MeetingsRootActual) }
    if ($EchoRoot) { $cli += @('--code-root', $EchoRoot) }
    $out = $script:PY_SRC | & $script:Py @cli 2>&1
    $code = $LASTEXITCODE
    $lines = @()
    foreach ($l in $out) { $lines += [string]$l }
    if ($code -ne 0) {
        Say ''
        Warn ("Python 助手（$Mode）失败，退出码 $code ，它说：")
        foreach ($l in $lines) { Info ('py| ' + $l) }
        Die '导入没有完成 —— 数据库/路径没有按预期解析出来（原因见上面几行）'
    }
    $idx = -1
    for ($i = 0; $i -lt $lines.Count; $i++) {
        if ($lines[$i].Trim() -eq '###FIXTURE-JSON###') { $idx = $i }
    }
    if ($idx -lt 0 -or ($idx + 1) -ge $lines.Count) {
        foreach ($l in $lines) { Info ('py| ' + $l) }
        Die 'Python 助手没有返回结果（上面是它的原始输出）'
    }
    $json = ($lines[($idx + 1)..($lines.Count - 1)] -join "`n")
    return ($json | ConvertFrom-Json)
}

function Get-TcpJson {
    param([int]$P, [string]$Path, [int]$TimeoutSec = 4)
    try {
        return Invoke-RestMethod -Uri ("http://127.0.0.1:$P" + $Path) -TimeoutSec $TimeoutSec
    } catch {
        return $null
    }
}

function Read-PortFile {
    param([string]$Root)
    if (-not $Root) { return 0 }
    $f = Join-Path $Root 'data\echo-port.txt'
    if (-not (Test-Path -LiteralPath $f)) { return 0 }
    try {
        $t = (Get-Content -LiteralPath $f -TotalCount 1).Trim()
        if ($t -match '^\d+$') { return [int]$t }
    } catch { }
    return 0
}

#  候选安装根（只用来找 echo-port.txt / runtime-core；找不到就回落到参数或环境变量）
function Get-RootCandidates {
    $c = New-Object System.Collections.Generic.List[string]
    if ($env:ECHO_BASE) { $c.Add($env:ECHO_BASE) }
    if ($env:ECHO_DATA) { $c.Add((Split-Path -Parent $env:ECHO_DATA)); $c.Add($env:ECHO_DATA) }
    if ($EchoRoot) {
        $c.Add($EchoRoot)
        $c.Add((Split-Path -Parent $EchoRoot))
    }
    $common = @('D:\ECHO', 'C:\ECHO', 'D:\echo', 'C:\echo')
    if ($env:USERPROFILE) { $common += (Join-Path $env:USERPROFILE 'ECHO') }
    if ($env:USERPROFILE) { $common += (Join-Path $env:USERPROFILE 'echo') }
    if ($env:LOCALAPPDATA) { $common += (Join-Path $env:LOCALAPPDATA 'ECHO') }
    if ($env:LOCALAPPDATA) { $common += (Join-Path $env:LOCALAPPDATA 'Programs\ECHO') }
    foreach ($d in $common) { if ($d) { $c.Add($d) } }
    $seen = @{}
    $out = @()
    foreach ($x in $c) {
        if (-not $x) { continue }
        $k = $x.TrimEnd('\').ToLower()
        if ($seen.ContainsKey($k)) { continue }
        $seen[$k] = $true
        if (Test-Path -LiteralPath $x) { $out += $x.TrimEnd('\') }
    }
    return $out
}

#  在某个根下找解释器（顺序与 scripts\start.ps1 / check-windows.ps1 一致）
function Find-PythonUnder {
    param([string]$Root)
    if (-not $Root) { return '' }
    foreach ($rel in @('runtime-core\python.exe', 'runtime-core\Scripts\python.exe',
                       'venv\Scripts\python.exe', 'venv\python.exe')) {
        $p = Join-Path $Root $rel
        if (Test-Path -LiteralPath $p) { return $p }
    }
    return ''
}

function Test-IsPython3 {
    param([string]$P)
    if (-not $P -or -not (Test-Path -LiteralPath $P)) { return $false }
    try {
        $v = & $P -c "import sys; print(sys.version_info[0])" 2>$null
        return ([string]$v -match '3')
    } catch { return $false }
}

# =====================================================================
#  0. 包自检
# =====================================================================
Say ''
Say ('ECHO 会议测试夹具 · 导入器 v' + $script:VERSION)
Say '---------------------------------------------------------------'

if (-not $PackRoot) {
    if ($PSScriptRoot) { $PackRoot = $PSScriptRoot } else { $PackRoot = (Get-Location).Path }
}
if (-not (Test-Path -LiteralPath $PackRoot)) { Die "找不到包目录：$PackRoot" }
$meetPackDir = Join-Path $PackRoot 'meeting'
if (-not (Test-Path -LiteralPath $meetPackDir)) { Die "包不完整：缺少 meeting\ 目录（$meetPackDir）" }

$dirs = @(Get-ChildItem -LiteralPath $meetPackDir -Directory | Sort-Object Name)
if ($Name) {
    $hit = @($dirs | Where-Object { $_.Name -eq $Name })
    if ($hit.Count -eq 0) { Die "包内没有会议目录：$Name" }
    $srcDir = $hit[0].FullName
} elseif ($dirs.Count -eq 1) {
    $srcDir = $dirs[0].FullName
} else {
    Die ('包内 meeting\ 下有 ' + $dirs.Count + ' 个目录，请用 -Name 指定一个：' +
         (($dirs | ForEach-Object { $_.Name }) -join ', '))
}
$Name = Split-Path -Leaf $srcDir

$srcWavs = @(Get-ChildItem -LiteralPath $srcDir -File |
             Where-Object { $_.Name -match '^\d+\.wav$' } | Sort-Object Name)
if ($srcWavs.Count -eq 0) { Die "包里这场会议没有任何 wav 分段：$srcDir" }
$srcMeta = Join-Path $srcDir 'meta.json'
if (-not (Test-Path -LiteralPath $srcMeta)) { Die "包里这场会议缺 meta.json：$srcDir" }
$meta = $null
try { $meta = Get-Content -LiteralPath $srcMeta -Raw -Encoding UTF8 | ConvertFrom-Json } catch { }
$srcBytes = 0
foreach ($w in $srcWavs) { $srcBytes += $w.Length }
$srcMinutes = 0
if ($meta -and $meta.durationSeconds) {
    $srcMinutes = [math]::Round(([double]$meta.durationSeconds) / 60.0, 1)
}

Ok ("会议：{0}（{1} 段 wav，{2} MB，约 {3} 分钟）" -f $Name, $srcWavs.Count,
    [math]::Round($srcBytes / 1MB, 1), $srcMinutes)
Info ("源目录：{0}" -f $srcDir)
if (Test-Path -LiteralPath (Join-Path $PackRoot '参考-旧转写结果')) {
    Info '（参考-旧转写结果\ 只是给对照看的旧转写/纪要，**不会被导入**）'
}

# =====================================================================
#  1. 找出这台机器上"真正生效"的会议目录 / 数据目录
# =====================================================================
Step '1/5 找出这台机器上真正生效的会议目录与数据目录'

$apiReport = $null
$ports = New-Object System.Collections.Generic.List[int]
if ($Port -gt 0) { $ports.Add($Port) }
if ($env:ECHO_PORT -and $env:ECHO_PORT -match '^\d+$') { $ports.Add([int]$env:ECHO_PORT) }
foreach ($r in (Get-RootCandidates)) {
    $pf = Read-PortFile $r
    if ($pf -gt 0) { $ports.Add($pf) }
}
$ports.Add(8970)
$portOrder = @()
foreach ($p in $ports) { if ($portOrder -notcontains $p) { $portOrder += $p } }

$livePort = 0
foreach ($p in $portOrder) {
    $rep = Get-TcpJson -P $p -Path '/api/paths/env' -TimeoutSec 3
    if ($rep -and $rep.roots) {
        $apiReport = $rep
        $livePort = $p
        break
    }
}

if ($apiReport) {
    $script:HelperMode = 'api'
    $rootMap = @{}
    foreach ($r in $apiReport.roots) { $rootMap[[string]$r.name] = [string]$r.path }
    if (-not $EchoRoot) { $EchoRoot = [string]$rootMap['ECHO'] }
    if (-not $DataRoot) { $DataRoot = [string]$rootMap['DATA'] }
    if (-not $MeetingsRoot) { $MeetingsRoot = [string]$rootMap['MEETINGS'] }
    Ok ("正在跑的 ECHO 在端口 {0}（面板 http://127.0.0.1:{0}/）" -f $livePort)
    Info ("ECHO 代码目录  ：{0}" -f $rootMap['ECHO'])
    Info ("数据根（库）  ：{0}" -f $rootMap['DATA'])
    Info ("会议目录      ：{0}" -f $rootMap['MEETINGS'])
    if ($rootMap['ECHO_BASE']) { Info ("安装根        ：{0}" -f $rootMap['ECHO_BASE']) }
    else { Info '安装根        ：（老式扁平布局：代码就在安装根下）' }
} else {
    Warn '没有找到正在跑的 ECHO（面板）。将直接按本机解释器 import app.paths 现算路径。'
    Info '（导入本身不需要 ECHO 在跑；但"面板里看到这场会议"需要它随后在跑。）'
    if (-not $EchoRoot -and $env:ECHO_ROOT) { $EchoRoot = $env:ECHO_ROOT }
    if (-not $EchoRoot) {
        foreach ($r in (Get-RootCandidates)) {
            if (Test-Path -LiteralPath (Join-Path $r 'app\paths.py')) { $EchoRoot = $r; break }
        }
    }
    $cwd = (Get-Location).Path
    if (-not $EchoRoot -and (Test-Path -LiteralPath (Join-Path $cwd 'app\paths.py'))) {
        $EchoRoot = $cwd
    }
    if (-not $EchoRoot -or -not (Test-Path -LiteralPath (Join-Path $EchoRoot 'app\paths.py'))) {
        Die ('找不到 ECHO 代码目录（含 app\paths.py）。请用 -EchoRoot <目录> 指定，' +
             '或先把 ECHO 面板打开再跑本脚本。')
    }
    $script:HelperMode = 'offline'
    Ok ("ECHO 代码目录：{0}（用户指定/自动发现）" -f $EchoRoot)
}

#  代码目录必须真的是 ECHO 的那棵树（助手会把 --code-root 塞进 sys.path 后 import app）
if (-not $EchoRoot -or -not (Test-Path -LiteralPath (Join-Path $EchoRoot 'app\paths.py'))) {
    Die ("ECHO 代码目录不可用：{0}（里面没有 app\paths.py）。请用 -EchoRoot <目录> 指定。" -f $EchoRoot)
}

# =====================================================================
#  2. 找解释器
# =====================================================================
Step '2/5 找能 import app 的解释器'

$pyCands = New-Object System.Collections.Generic.List[string]
if ($Python) { $pyCands.Add($Python) }
if ($env:ECHO_PYTHON) { $pyCands.Add($env:ECHO_PYTHON) }
$bases = New-Object System.Collections.Generic.List[string]
if ($EchoRoot) {
    $bases.Add($EchoRoot)
    if ((Split-Path -Leaf $EchoRoot) -ieq 'echo-core') { $bases.Add((Split-Path -Parent $EchoRoot)) }
}
if ($DataRoot) {
    $bases.Add((Split-Path -Parent $DataRoot))
    $bases.Add($DataRoot)
}
foreach ($r in (Get-RootCandidates)) { $bases.Add($r) }
foreach ($b in $bases) {
    $p = Find-PythonUnder $b
    if ($p) { $pyCands.Add($p) }
}

$script:Py = ''
foreach ($c in $pyCands) {
    if (Test-IsPython3 $c) { $script:Py = $c; break }
}
if (-not $script:Py) {
    Warn '没找到能用的解释器（<安装根>\runtime-core\python.exe 或 venv\Scripts\python.exe）'
    Die '请用 -Python <python.exe 全路径> 指定（就是 ECHO 自己用的那个解释器）'
}
Ok ("解释器：{0}" -f $script:Py)

# 离线模式：先让助手把数据根 / 会议目录算出来
if ($script:HelperMode -eq 'offline' -and (-not $DataRoot -or -not $MeetingsRoot)) {
    $probe = Invoke-Helper -Mode 'probe'
    if (-not $DataRoot) { $DataRoot = [string]$probe.dataRoot }
    if (-not $MeetingsRoot) { $MeetingsRoot = [string]$probe.meetingsRoot }
    Info ("数据根（库）  ：{0}" -f $probe.dataRoot)
    Info ("会议目录      ：{0}" -f $probe.meetingsRoot)
    if ($probe.echoBase) { Info ("安装根        ：{0}" -f $probe.echoBase) }
    else { Info '安装根        ：（老式扁平布局）' }
}

if (-not $DataRoot) { Die '没能解析出数据根（数据库所在目录）。请用 -DataRoot 指定。' }
if (-not $MeetingsRoot) { Die '没能解析出会议目录。请用 -MeetingsRoot 指定。' }
$script:DataRootActual = $DataRoot
$script:MeetingsRootActual = $MeetingsRoot
if (-not (Test-Path -LiteralPath $DataRoot)) {
    Warn ("数据根目录还不存在：{0}（写入时会创建库文件）" -f $DataRoot)
}

# =====================================================================
#  3. 看现状（幂等判据）
# =====================================================================
Step '3/5 看现状'

$st = Invoke-Helper -Mode 'status' -Extra @('--name', $Name)
$destDir = Join-Path $MeetingsRoot $Name
Info ("目标目录：{0}" -f $destDir)
Info ("数据库  ：{0}" -f $st.dbFile)
if ($st.schema -ne 'ok') { Warn '这个数据根里还没有 ECHO 的库/表（接下来的写入会顺带建库）' }
if ($st.meetingRow) {
    Info ("已有会议记录：id={0} status={1} lines={2}" -f $st.meetingId, $st.status, $st.lines)
} else {
    Info '还没有这场会议的记录'
}
if ($st.folderExists) { Info ("目标目录已存在，里面有 {0} 个 wav" -f @($st.wavs).Count) }
if ($st.transcript) { Warn '目标目录里已经有 transcript.md（上次的转写结果）；「重新转写」会覆盖它' }

# =====================================================================
#  4a. 删除模式
# =====================================================================
if ($Remove) {
    Step '4/5 删除这场测试会议'
    if (-not $st.meetingRow -and -not $st.folderExists) {
        Ok '本来就没有这场会议，无需删除'
        exit 0
    }
    if ($st.meetingRow) {
        Say ("  将删除：会议记录 id={0}（name={1}）" -f $st.meetingId, $Name)
    } else {
        Say '  将删除：只有音频目录，没有会议记录'
    }
    Say ("          音频目录 {0}（{1} 个 wav）" -f $destDir, @($st.wavs).Count)
    if (-not $Yes) {
        $a = Read-Host '  确认删除？(y/N)'
        if ($a -notmatch '^[yY]') { Warn '已取消，什么都没删'; exit 0 }
    }
    if ($DryRun) { Warn '-DryRun：只报告，不删'; exit 0 }
    $res = Invoke-Helper -Mode 'remove' -Extra @('--name', $Name)
    if ($res.meetingRow) { Ok ("会议记录已删除（{0}）" -f $res.deleted) }
    else { Ok '没有会议记录需要删' }
    if (Test-Path -LiteralPath $destDir) {
        Remove-Item -LiteralPath $destDir -Recurse -Force
        Ok ("音频目录已删除：{0}" -f $destDir)
    }
    Say ''
    Ok '删除完成。面板的会议列表几秒内会自己刷新掉它。'
    exit 0
}

# =====================================================================
#  4b. 导入
# =====================================================================
Step '4/5 放音频'

if ($st.meetingRow -and -not $Force) {
    Info '这场会议的记录已经存在 —— 跳过建记录（幂等：重复跑不会产生第二条）'
    Info '（要让记录按当前包重刷，加 -Force；它会清掉本场的转写行，便于再测一次）'
}

$copied = 0
$skipped = 0
if (-not (Test-Path -LiteralPath $destDir)) {
    if ($DryRun) { Warn ("-DryRun：本会创建目录 {0}" -f $destDir) }
    else { New-Item -ItemType Directory -Force -Path $destDir | Out-Null }
}
foreach ($w in $srcWavs) {
    $dst = Join-Path $destDir $w.Name
    if (Test-Path -LiteralPath $dst) {
        $dstLen = (Get-Item -LiteralPath $dst).Length
        if ($dstLen -eq $w.Length) {
            $skipped++
            Info ("已有且大小一致，跳过：{0}" -f $w.Name)
            continue
        }
        if (-not $Force) {
            Warn ("目标已有 {0} 但大小不同（{1} != {2}）—— 未覆盖（要覆盖加 -Force）" -f
                  $w.Name, $dstLen, $w.Length)
            continue
        }
    }
    if ($DryRun) {
        Warn ("-DryRun：本会复制 {0}（{1} MB）" -f $w.Name, [math]::Round($w.Length / 1MB, 1))
        continue
    }
    Copy-Item -LiteralPath $w.FullName -Destination $dst -Force
    $copied++
    Ok ("已放入 {0}（{1} MB）" -f $w.Name, [math]::Round($w.Length / 1MB, 1))
}
$srcMetaItem = Get-Item -LiteralPath $srcMeta
$dstMeta = Join-Path $destDir 'meta.json'
$metaSame = (Test-Path -LiteralPath $dstMeta) -and
            ((Get-Item -LiteralPath $dstMeta).Length -eq $srcMetaItem.Length)
if ($metaSame) {
    Info '已有且大小一致，跳过：meta.json'
} elseif ($DryRun) {
    Warn '-DryRun：本会复制 meta.json'
} else {
    Copy-Item -LiteralPath $srcMeta -Destination $dstMeta -Force
    Ok '已放入 meta.json'
}
if ($copied -eq 0 -and $skipped -eq 0 -and -not $DryRun) {
    Warn '一个音频文件都没放进去 —— 目标目录里可能已经有同名但大小不同的文件（见上面的告警）'
}

Step '5/5 建/核对会议记录（走 app/db.py，不拼 SQL）'

$title = '【测试夹具】真实会议 ' + $Name
if ($srcMinutes -gt 0) { $title = $title + '（约 ' + $srcMinutes + ' 分钟）' }
$notes = ('ECHO-meeting-fixture 导入的测试会议（' + $Name +
          '）：音频已就位、尚未转写。「已中断」是导入时的预期状态，不是故障。' +
          '测法与删除见包内 先读我.md。')
$errText = '导入的测试夹具：音频已就位，本机还没有它的转写结果 —— 点详情页的「重新转写」开始。'

if ($DryRun) {
    Warn '-DryRun：不写数据库。下面这些本来会做：'
    if ($st.meetingRow) {
        if ($Force) {
            Info ("记录已存在 id={0}：会重刷字段并清空本场转写行" -f $st.meetingId)
        } else {
            Info ("记录已存在 id={0}：会跳过，不改" -f $st.meetingId)
        }
    } else {
        Info 'create_meeting(name, started_at=meta.start, stt_model=meta.config.sttModel, ...)'
        Info 'update_meeting(id, ended_at/duration_seconds/segments/audio_bytes/status=interrupted)'
        Info ('再加 title=' + $title)
    }
    Say ''
    Ok 'DryRun 结束：什么都没改。'
    exit 0
}

$extraArgs = @('--name', $Name, '--title', $title, '--notes', $notes, '--error', $errText)
if ($Force) { $extraArgs += '--force' }
$res = Invoke-Helper -Mode 'apply' -Extra $extraArgs

switch ([string]$res.action) {
    'created' { Ok ("已建会议记录：id={0}" -f $res.id) }
    'exists'  {
        if ($Force) { Ok ("已按 -Force 重刷会议记录：id={0}（转写行已清空）" -f $res.id) }
        else { Ok ("会议记录已存在：id={0}（未改动）" -f $res.id) }
    }
    default   { Ok ("会议记录处理完成：{0}" -f $res.action) }
}
Info ("name={0}  status={1}  时长 {2} 秒  分段 {3}  音频 {4} MB  已有转写行 {5}" -f
      $res.name, $res.status, [math]::Round([double]$res.durationSeconds, 0), $res.segments,
      [math]::Round([double]$res.audioBytes / 1MB, 1), $res.lines)

# ---------------------------------------------------------------- 用 API 复核（若可用）
if ($livePort -gt 0) {
    $list = Get-TcpJson -P $livePort -Path '/api/meetings?limit=500' -TimeoutSec 8
    if ($list -and $list.items) {
        $mine = @($list.items | Where-Object { $_.name -eq $Name })
        if ($mine.Count -eq 1) {
            Ok ("GET /api/meetings 里能看到这场会议（共 {0} 场），id={1} status={2}" -f
                @($list.items).Count, $mine[0].id, $mine[0].status)
            $detail = Get-TcpJson -P $livePort -Path ('/api/meetings/' + $mine[0].id) -TimeoutSec 8
            if ($detail -and $detail.folder) { Info ("详情里的目录：{0}" -f $detail.folder) }
            try {
                # 不设 Range 头：WinPS 5.1 的 -Headers 不接受 Range（受限头，会直接抛
                # "The 'Range' header must be modified using the appropriate property"）。
                # 整段拉一次也不贵（回环、18 MB），顺便核对大小与 RIFF 头。
                $u = 'http://127.0.0.1:' + $livePort + '/api/meetings/' + $mine[0].id + '/audio?seg=1'
                $r = Invoke-WebRequest -Uri $u -UseBasicParsing -TimeoutSec 180
                $magic = ''
                if ($r.Content.Length -ge 4) {
                    $magic = [System.Text.Encoding]::ASCII.GetString($r.Content[0..3])
                }
                if ($magic -eq 'RIFF') {
                    Ok ("第 1 段音频能取到：HTTP {0}，{1} 字节，RIFF wav（说明音频路径对得上）" -f
                        $r.StatusCode, $r.RawContentLength)
                } else {
                    Warn ("第 1 段音频返回 {0} 字节，但不是 RIFF wav（头 4 字节：{1}）" -f
                          $r.RawContentLength, $magic)
                }
            } catch {
                Warn ("第 1 段音频取不到：{0}" -f $_.Exception.Message)
                Warn '（若端口上那个 ECHO 与本次导入用的不是同一份数据根，就会出现这种不一致）'
            }
        } elseif ($mine.Count -eq 0) {
            Warn 'GET /api/meetings 里还看不到这场会议 —— 可能是那个 ECHO 用的数据根与本次不同'
        } else {
            Warn ("GET /api/meetings 里有 {0} 条同名记录（不该发生，请检查）" -f $mine.Count)
        }
    }
}

# =====================================================================
#  收尾：下一步
# =====================================================================
Say ''
Say '---------------------------------------------------------------'
Ok '导入完成。'
Say ''
Say '下一步（面板里）：'
if ($livePort -gt 0) { Say ('  0. 打开面板：http://127.0.0.1:' + $livePort + '/') }
else { Say '  0. 打开面板（ECHO 没在跑就先启动它）' }
Say  '  1. 左侧点「会议」页签 —— 列表每 2 秒自刷一次，应能看到：'
Say ('     ' + $title + '   /   状态「已中断」= 有音频、还没转写')
Say  '  2. 点它打开详情页 → 右上角「重新转写」'
Say  '  3. 要测【能力后端】那条链路：先在「能力后端」页签的「会议转写服务」卡里，'
Say  '     把「会议转写用哪个后端」选成「ECHO 后端」（"会议产出"选「全部」= 转写+说话人+声纹），'
Say  '     按卡上方「ECHO 后端」那节填好地址并完成配对；出网许可不能是「不出机」。'
Say  '     然后回详情页点「重新转写」—— 这一场就会整场交给 ECHO 后端。'
Say  '  4. 想删掉这场测试会议：再跑一次本脚本并加 -Remove（会一并删掉音频目录）'
Say ''
Say '（包内 先读我.md 里有完整的说明、预期结果与耗时估算。）'
exit 0
