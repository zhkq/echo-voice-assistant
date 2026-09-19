# PowerShell 编码与脚本经验笔记（2026-08-28）

> 适用范围：本仓库所有会被 **`powershell.exe`（Windows PowerShell 5.1）** 执行的 `.ps1`
> （桌面快捷方式、开机自启、`start-all` 等都指向 WinPS 5.1，不是 pwsh）。
> 本文是一份"踩坑 & 避坑"备忘录，来自 ECHO 启动脚本 `forrtl: window-CLOSE` 与中文乱码问题排查。

---

## 一、最核心：WinPS 5.1 对无 BOM 的 .ps1 按系统 ANSI 解析

- 中文系统上，`powershell.exe -File x.ps1`（5.1）读取无 BOM 的脚本时**按 GBK（系统代码页）解码**，
  UTF-8 中文字节流会被当作乱码，**不只是显示问题，而是结构损坏**。
- 最隐蔽的破坏：UTF-8 多字节字符结尾有时会"吞掉"换行符，导致**注释行与下一行代码粘连**，
  下一行被整体注释掉 → 变量根本没被赋值（变 `$null`），运行时报：
  ```
  Cannot bind argument to parameter 'Path' because it is null
  ```
  且**报错行号会错位**（报在某条注释行），非常迷惑。
  - 实锤案例：`start.ps1` 里 `$pyw = $py -replace 'python\.exe$','pythonw.exe'` 被"注释吞掉"，
    `$pyw` 为 null，`Test-Path $pyw` 崩溃。

**结论：凡要交给 `powershell.exe` 执行的 `.ps1`，一律以 "UTF-8 with BOM"（前 3 字节 `EF BB BF`）保存。**
本仓库其余脚本（`stop.ps1`、`install-*.ps1`）本身都是带 BOM 的，勿破坏。

---

## 二、编辑工具会偷偷弄丢 BOM（本次最大教训）

用文本编辑工具修改**已带 BOM** 的 `.ps1` 后，写入结果可能变成无 BOM —— 内容看起来完全正常
（按 UTF-8 读都对），但一旦用户在 WinPS 5.1 下跑就崩。

**修改这类中文脚本的原则性三步：**
1. **改完立即查 BOM**——只 `read` 验证不了编码，必须看字节；
2. 在真实 `powershell.exe`（不是 pwsh）下跑一遍，确认无报错、无乱码；
3. 若 BOM 丢了，用下面的命令补回去。

---

## 三、Windows PowerShell 5.1 与 PowerShell 7（pwsh）是两回事

| 项目 | powershell.exe（WinPS 5.1） | pwsh（7+） |
|---|---|---|
| `.ps1` 无 BOM | 按 ANSI/GBK 解析 → 中文脚本崩 | 默认 UTF-8，无 BOM 也 OK |
| 重定向 `>`/`*>` 默认编码 | 系统 ANSI → 中文日志乱码 | UTF-8 |
| `Get-Content` 读中文 | 建议显式 `-Encoding UTF8` | 默认 UTF-8 |

- "我本机跑没问题" ≠ "目标宿主（WinPS 5.1）没问题"；**以脚本实际执行的宿主为准排查**。
- 区分"数据坏了"和"显示坏了"：UTF-8 数据在 GBK 控制台显示成 `Â·`/`å°` 等，只是显示层问题，
  不要误判为数据损坏。

---

## 四、自查 / 修复命令

```powershell
# 查某文件是否带 BOM（返回前 3 字节，应为 45,101,110 或 239,187,191）
[System.IO.File]::ReadAllBytes($f)[0..2]

# 把无 BOM 的 UTF-8 文件重新存为带 BOM
$c = [System.IO.File]::ReadAllText($f, [Text.UTF8Encoding]::new($false))
[System.IO.File]::WriteAllText($f, $c, [Text.UTF8Encoding]::new($true))
```

---

## 五、非字符集但同源的脚本健壮性坑

1. **后台服务不要用 `python.exe -NoNewWindow` 挂到启动窗口上**
   - `Start-Process` 参数集限制：`-NoNewWindow` 与 `-RedirectStandardOutput` 同一参数集，
     **不能**与 `-WindowStyle Hidden` 同用。
   - `-NoNewWindow` = 服务依附启动控制台 → **关窗发 `window-CLOSE`，整个进程树被杀**。
     日志铁证：`forrtl: error (200): program aborting due to window-CLOSE event`。
   - 正确姿势：**`pythonw.exe`（GUI 子系统，不创建/不依附任何控制台）+ `-RedirectStandardOutput/Error` 写日志文件**。
     没有窗口可关，也不会收到窗口关闭事件。这是 ECHO"关窗即断"的修复核心。

2. **PowerShell 返回值会漏到控制台**：函数/语句结果不接收不丢弃就会流进管道打印出来
   （窗口里的 `true` 就是 `Open-Panel` 的 `return $true`）。
   调用有返回值的东西用 `$null = Func` 或 `| Out-Null` 吸收。

3. **组策略可能拦 `-WindowStyle Hidden`**（症状：双击快捷方式无反应），
   隐藏窗口应在脚本内部做（`ShowWindow(GetConsoleWindow(), 0)`），别写在快捷方式参数里。

4. **git 提示 `LF will be replaced by CRLF`** 只是 autocrlf 归一化提示，不影响 PowerShell 解析。

5. **管理员策略会拦"引号内含 `|`"的命令行**（2026-09-19 实测，`WinError 786`）
   - 症状：`subprocess.run(["powershell", ..., "-Command", ps])` 在 `CreateProcess` 阶段
     直接抛 `OSError: [WinError 786] Access to %1 has been restricted by your Administrator
     by policy rule %2`（中文："管理员用策略规则限制了对 %1 的访问"）。
   - 触发条件（`scripts/probe_powershell.py` 夹出来的）：命令里出现**引号内的 `|` 字面量**
     （例如 `"$_.Name + ' | ' + $_.Culture"`）→ 被拦；同样带**真管道** `|`（在引号外）→ 通过。
     疑似 EDR 按命令行文本做注入/管道启发式判定。
   - 与"命令行多长"无关，与"哪个 venv 的 python"无关，与"用不用 Add-Type"无关。
   - **规避**：不要在 PowerShell 命令的字符串里放 `|`。要输出多列就分语句输出
     （`... { $_.A; $_.B }`）由调用方排版，或用 `-f` 格式化。
   - ECHO 自己的两条命令（`app/platform/win32/sapi.py` 的常驻 SAPI、`win32/env.py` 的桌面
     通知）都**不含**这种写法，实测通过 —— 所以**产品功能不受影响**，被拦的只是当时的探针。
   - 判别手段：`scripts/probe_powershell.py`（直接 import 生产模块拿脚本正文来试）。

---

## 六、收尾/提交前检查清单

- [ ] 被 `powershell.exe` 执行的 `.ps1` 全部带 UTF-8 BOM
- [ ] 在真实的 `powershell.exe -NoProfile -ExecutionPolicy Bypass -File ...` 下跑过一遍，无报错无乱码
- [ ] 中文日志按预期编码写入 `data\logs\`（无 GBK 乱码）
- [ ] 后台服务用 `pythonw.exe` 启动，不依附任何控制台窗口
- [ ] 有返回值的函数调用已用 `$null =` / `| Out-Null` 吸收，启动窗口不再打印杂项
- [ ] `-Command` 的命令行里**没有**"引号内的 `|`"（会被管理员策略拦，WinError 786）
