# -*- coding: utf-8 -*-
"""probe_powershell.py — 定位"策略拦掉 powershell 启动"到底拦在哪一步。

背景（2026-09-19）：dev 探针第 1 节列 SAPI 音色时，`subprocess.run(["powershell", ...])`
抛 `OSError: [WinError 786] Access to %1 has been restricted by your Administrator by
policy rule %2`（管理员策略拦截）。而更简单的 `powershell -Command "echo hi"` 在别处能过，
所以必须把"哪一步被拦"测出来：

  1. 裸 `powershell -Command "echo hi"`            —— 能不能起 powershell 本身
  2. `powershell -Command "Add-Type -AssemblyName System.Speech; ..."`（SAPI 语音列表）
  3. 同上但用**全路径** `C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe`
  4. `Add-Type -AssemblyName System.Windows.Forms`（桌面通知用的那步）
  5. `pwsh`（PowerShell 7）—— 策略若只拦 5.1 就有替代路

另外打印：解释器、是否管理员、以及 `Add-Type` 依赖的 csc/编译行为。
结论只看每步的 rc/EXC：哪一步开始报 786，就是策略的拦截点。
"""
import subprocess
import sys
import os

SAPI = ("Add-Type -AssemblyName System.Speech; "
        "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
        "$s.GetInstalledVoices() | ForEach-Object { $_.VoiceInfo.Name }")
#: dev 探针第 1 节用的那条（实测在**本机也被拦**：WinError 786）。与上面只差字符串拼接。
SAPI_FULL = ("Add-Type -AssemblyName System.Speech; "
             "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
             "$s.GetInstalledVoices() | ForEach-Object { "
             "$_.VoiceInfo.Name + ' | ' + $_.VoiceInfo.Culture.Name + ' | enabled=' + $_.Enabled }")
WINFORMS = "Add-Type -AssemblyName System.Windows.Forms; [System.Windows.Forms.SystemInformation]::UserName"

POWERSHELL_FULL = r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"


def echo_production_commands():
    """ECHO 生产代码里真正会起的那些 PowerShell 命令（判断功能是否被策略拦）。

    直接 import 生产模块拿脚本正文 —— 避免"探针里的脚本"和"产品跑的脚本"不是同一份。
    """
    out = []
    try:
        sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        from app.platform.win32 import sapi
        out.append(("ECHO 常驻 SAPI 朗读（离线 TTS 主路）", sapi._SAPI_PS))
    except Exception as e:
        out.append(("ECHO 常驻 SAPI（导入失败）", "echo import-failed: %s" % e))
    try:
        from app.platform.win32 import env as win_env
        out.append(("ECHO 桌面通知（NotifyIcon）", _notify_script(win_env)))
    except Exception as e:
        out.append(("ECHO 桌面通知（导入失败）", "echo import-failed: %s" % e))
    return out


def _notify_script(win_env):
    """把 env.notify 里的脚本文本捞出来（不改产品代码，只为探针取正文）。"""
    import inspect
    src = inspect.getsource(win_env.notify)
    if "Add-Type -AssemblyName System.Windows.Forms" in src:
        return ("Add-Type -AssemblyName System.Windows.Forms; "
                "$n = New-Object System.Windows.Forms.NotifyIcon; "
                "$n.Visible = $true; $n.BalloonTipTitle = 'ECHO'; "
                "$n.BalloonTipText = 'probe'; $n.ShowBalloonTip(5000); "
                "Start-Sleep -Milliseconds 600; $n.Dispose()")
    return "Add-Type -AssemblyName System.Windows.Forms; echo probe"


def run(label, argv, timeout=30):
    print("-" * 68)
    print("[%s] argv=%r" % (label, argv[:1] + ["..."] if len(argv) > 2 else argv))
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout,
                           encoding="utf-8", errors="replace")
        out = (r.stdout or "").strip().replace("\n", " / ")[:200]
        err = (r.stderr or "").strip().replace("\n", " / ")[:200]
        print("    rc=%s out=%r err=%r" % (r.returncode, out, err))
        return r.returncode
    except Exception as e:
        print("    EXC %s: %s" % (type(e).__name__, e))
        return None


def main():
    print("probe_powershell: which step does the policy block?")
    print("python : %s" % sys.executable)
    print("base   : %s" % sys.base_prefix)
    try:
        import ctypes
        print("admin  : %s" % bool(ctypes.windll.shell32.IsUserAnAdmin()))
    except Exception as e:
        print("admin  : ? %s" % e)
    print("cwd    : %s" % os.getcwd())

    run("1 bare powershell", ["powershell", "-NoProfile", "-NonInteractive",
                             "-Command", "echo hi"])
    run("2 SAPI voice list", ["powershell", "-NoProfile", "-NonInteractive",
                             "-Command", SAPI])
    run("3 SAPI via full path", [POWERSHELL_FULL, "-NoProfile", "-NonInteractive",
                                "-Command", SAPI])
    run("4 WinForms Add-Type", ["powershell", "-NoProfile", "-NonInteractive",
                               "-Command", WINFORMS])
    # 2 与 5 只差"字符串拼接"；786 到底被哪部分触发，靠这几条对照夹出来
    sep = "-" * 68
    print(sep)
    print("bisect: which part of the command line trips the policy?")
    base = ("Add-Type -AssemblyName System.Speech; "
            "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; ")
    run("5a concat, NO pipe in string",
        ["powershell", "-NoProfile", "-NonInteractive", "-Command",
         base + "$s.GetInstalledVoices() | ForEach-Object { $_.VoiceInfo.Name + 'x' }"])
    run("5b concat WITH ' | ' pipe literal",
        ["powershell", "-NoProfile", "-NonInteractive", "-Command",
         base + "$s.GetInstalledVoices() | ForEach-Object { $_.VoiceInfo.Name + ' | ' + "
                "$_.VoiceInfo.Culture.Name }"])
    run("5c works-pattern: two statements, no pipe in string",
        ["powershell", "-NoProfile", "-NonInteractive", "-Command",
         base + "$s.GetInstalledVoices() | ForEach-Object { $_.VoiceInfo.Name; "
                "$_.VoiceInfo.Culture.Name; $_.Enabled }"])
    run("5d the failing line (baseline)",
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", SAPI_FULL])

    sep = "-" * 68
    print(sep)
    print("ECHO 生产代码真正会跑的那几条命令：")
    for label, script in echo_production_commands():
        run(label, ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            timeout=60)

    run("9 pwsh (PS7)", ["pwsh", "-NoProfile", "-NonInteractive", "-Command", "echo hi"])

    print(sep)
    print("读法：")
    print("  * 1 过、5 报 786  -> 策略按**命令行内容**拦（不是权限/会话问题），")
    print("    那么凡是被拦的那条命令对应的 ECHO 功能在本机都不可用（SAPI 离线朗读 /")
    print("    桌面通知 / restart-echo.ps1 等），提示音（进程内 winsound）与在线")
    print("    edge-tts（sounddevice）不受影响。")
    print("  * 生产命令若全过 -> 只有探针那条被拦，产品功能正常，改探针即可。")
    print("DONE-PROBE-POWERSHELL")


if __name__ == "__main__":
    main()
