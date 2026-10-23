// Program.cs — ECHO 仪表盘边条（独立进程 · 无边框 · 贴右缘 · 置顶 · 可拖左边框调宽）
//
// 为什么需要这个独立进程：
//   DSH Desktop 2.0.9 里，挂在 app.asar.unpacked 的插件只能拿到 electron 的
//   "工具/渲染进程变体"（own=[net,systemPreferences]，无 app/BrowserWindow/screen/
//   globalShortcut）；静态 import 与动态 import 结果一致（2026-09-12 实测，见
//   ECHO\data\logs\electron-probe.log）。而 2.0.5 时插件能拿到完整 API 并直接开边条
//   （旧日志 `ECHO 仪表盘: 展开（416px …）`）。因此边条改为独立进程：
//   .NET 7 WinForms + WebView2（本机已装运行时 152.x）承载同一个 http://127.0.0.1:8970。
//
// 交互（对齐升级前的边条）：
//   - 无边框、不占任务栏、始终置顶、贴 1 号屏右缘、高度铺满工作区
//   - 左边缘留 6px 把手：拖它调宽度（WebView2 是独立子窗口，会吞掉鼠标事件，
//     所以宽度把手必须由窗体自己那一条边来承担）；宽度会记住
//   - 单实例：再次启动（热键）→ 收起为 48px 窄条 ⇄ 展开
//   - **两页常驻**（2026-10-23 用户："折叠后再展开要等几秒"）：主面板与折叠条各一支
//     WebView，收起/展开只切可见性，不再重新导航 —— 展开是即时的，不会白屏重载
//   - **悬停才唤回 + 离开自动收回**（2026-10-23 用户：一碰就弹会挡到别的窗口滚动条）：
//     鼠标贴右缘**停住** 600ms 才滑出（拖滚动条那种持续移动不算），
//     滑出后若没点「展开」、鼠标离开 1.5s，自动收回到屏右缘
//
// 诊断：所有异常与每次吸附都会写 %TEMP%\echo-sidebar.log（排查宽度/DIP 问题用）。
//
// 退出（2026-10-21 真机 bug：ECHO 停了，边条还挂在屏幕上）：
//   * 正常停止：ECHO 经**已有的命名管道**发一条 `quit`（`app/runtime.py:quit_sidebar()` /
//     `scripts\stop.ps1` 也会发同一条命令）→ 本进程关窗退出；
//   * 兜底：服务被强杀 / 崩溃时没人发 quit，由心跳自己探活退出（判据见 Heartbeat.cs）。
//
// 用法：echo-sidebar.exe [--url=http://127.0.0.1:8970/] [--collapsed] [--width=450] [--probe]
//                       [--signal=toggle|rail-hide|rail-show|rail-expand|quit]

using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.IO.Pipes;
using System.Text;
using System.Windows.Forms;
using Microsoft.Web.WebView2.WinForms;

namespace EchoSidebar;

/// <summary>边条尺寸与交互常量（Program 与 SidebarForm 共用）。</summary>
internal static class SidebarConfig
{
    /// <summary>
    /// 单实例信号管道（已有的 IPC）：命令有 toggle / rail-hide / rail-show / rail-expand，
    /// 以及 2026-10-21 加的 **quit**（请边条退出）。
    /// 为什么复用这条管道而不是新开一套 HTTP/窗口消息：ECHO 早就在用它发 toggle，
    /// 边条侧的监听循环也早就在跑 —— 退出只是多一个命令字，不引入新协议、新端口、新依赖。
    /// </summary>
    public const string PipeName = "echo-sidebar-toggle";
    /// <summary>窄条显示态宽度（逻辑像素）。演进：48 → 64 → 100 → 64（2026-09-12 用户最终选 64）。</summary>
    public const int RailWidth = 64;
    public const int MinPanelWidth = 320;
    public const int MinLeftPad = 120;
    public const int DefaultWidth = 450;
    /// <summary>展开态左边缘把手宽度（逻辑像素）：留给自己做拖拽调宽，WebView2 不覆盖这条边。</summary>
    public const int GripWidth = 6;
    /// <summary>窄条态左侧"点击展开"区域宽度（逻辑像素）。位于 WebView 之外，不遮挡窄条内容。</summary>
    internal const int RailGripWidth = 12;
    /// <summary>隐藏态在屏右缘保留的可唤回宽度（逻辑像素）。</summary>
    internal const int RailPeekWidth = 3;
    /// <summary>鼠标进入屏右缘多少像素内就把窄条唤回（逻辑像素）。</summary>
    internal const int RailWakeMargin = 6;
    /// <summary>
    /// 鼠标在屏右缘**停留多久**才把窄条滑出（毫秒）。
    /// 为什么要停留（2026-10-23 用户）：原来一碰右缘就滑出，用户**拖别的窗口滚动条**时
    /// 会被它抢走鼠标 —— 滚动条正好在屏右缘。留一段悬停时间，正常滚动的"路过"就不会触发。
    /// </summary>
    internal const int RailHoverDwellMs = 600;
    /// <summary>
    /// 悬停滑出后，鼠标**离开窄条区域**多久自动收回（毫秒）。
    /// 用户 2026-10-23："没有按展开按钮前，鼠标离开一段时间后再自动隐藏"——
    /// 悬停只是"临时看一眼"，不是把边条永久钉在屏幕上；点「展开」才会变成常驻面板。
    /// </summary>
    internal const int RailAutoHideMs = 1500;
    /// <summary>
    /// 悬停计时允许的鼠标抖动（物理像素）：超过就重新计时。
    /// 拖滚动条时鼠标会持续上下移动 —— 一直在动就不该被判定成"悬停"，
    /// 这是"不影响操作其他窗口滚动条"的第二道保险（第一道是停留时间）。
    /// </summary>
    internal const int RailHoverJitterPx = 4;
    /// <summary>
    /// 窄条内容的左补偿（逻辑像素，正值=向左移）。
    /// 窄条态 WebView 从 RailGripWidth(12) 处开始，若按自身宽度居中，内容中心会落在
    /// 12+36/2=30px，相对 48px 窄条偏右 6px。这里按 2/3 补偿（4px），既接近居中又
    /// 不贴到把手边界。注入方式：rail 页面 body 上加 padding-left。
    /// </summary>
    internal const int RailVisualShift = RailGripWidth / 3;
}

internal static class Program
{
    internal static readonly string LogPath = Path.Combine(Path.GetTempPath(), "echo-sidebar.log");

    internal static void Log(string message)
    {
        try { File.AppendAllText(LogPath, $"[{DateTime.Now:HH:mm:ss.fff}] {message}\n"); } catch { /* 忽略 */ }
    }

    /// <summary>
    /// 用户拖拽过的展开宽度（逻辑像素）落盘文件。
    /// 为什么需要：记忆值原本只在进程内，重启边条（开机、DSH 升级后拉起）就退回 --width=450，
    /// 用户每次都得重新拖一次（2026-09-12 实测：拖到 400 后重启又变 450）。
    /// </summary>
    internal static readonly string WidthPath = Path.Combine(
        Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
        "echo-sidebar", "panel-width.txt");

    internal static int LoadRememberedWidth()
    {
        try
        {
            if (!File.Exists(WidthPath)) return 0;
            return int.TryParse(File.ReadAllText(WidthPath).Trim(), out var v) ? v : 0;
        }
        catch (Exception ex) { Log("load width failed: " + ex.Message); return 0; }
    }

    internal static void SaveRememberedWidth(int logical)
    {
        try
        {
            Directory.CreateDirectory(Path.GetDirectoryName(WidthPath));
            File.WriteAllText(WidthPath, logical.ToString());
        }
        catch (Exception ex) { Log("save width failed: " + ex.Message); }
    }

    [STAThread]
    private static void Main(string[] args)
    {
        AppDomain.CurrentDomain.UnhandledException += (_, e) =>
            Log("UNHANDLED: " + (e.ExceptionObject as Exception)?.ToString());
        Application.ThreadException += (_, e) => Log("THREAD EXCEPTION: " + e.Exception);

        var url = ArgValue(args, "--url") ?? "http://127.0.0.1:8970/";
        var collapsed = args.Any(a => a.Equals("--collapsed", StringComparison.OrdinalIgnoreCase));
        var width = ArgInt(args, "--width") ?? SidebarConfig.DefaultWidth;
        // 落盘的用户宽度优先于调用方的默认宽度：ECHO 固定传 --width=450，若直接采信，
        // 用户拖过的宽度在每次重启后都会丢。显式传非默认宽度（人工调试）仍然生效。
        if (width == SidebarConfig.DefaultWidth)
        {
            var remembered = LoadRememberedWidth();
            if (remembered > 0)
            {
                Log($"remembered width from disk: {remembered} (overrides --width={width})");
                width = remembered;
            }
        }
        // 折叠条页面：优先命令行 --rail，其次常见位置（ECHO\web\rail.html），都没有则用内嵌兜底
        var railArg = ArgValue(args, "--rail");
        var railPath = ResolveRailPath(railArg);
        Log($"rail page: {railPath ?? "(none - using inline fallback)"}");

        ApplicationConfiguration.Initialize();

        // 诊断模式：只计算尺寸并写日志，不建窗口
        if (args.Any(a => a.Equals("--probe", StringComparison.OrdinalIgnoreCase)))
        {
            var area = Screen.PrimaryScreen?.WorkingArea ?? Rectangle.Empty;
            Log($"probe: requested={width} workArea={area} bounds={Screen.PrimaryScreen?.Bounds} device={Screen.PrimaryScreen?.DeviceName}");
            return;
        }

        Log($"start: url={url} collapsed={collapsed} width={width} pid={Environment.ProcessId}");
        Log($"screen: workArea={Screen.PrimaryScreen?.WorkingArea} bounds={Screen.PrimaryScreen?.Bounds} " +
            $"deviceDpi={DeviceDpiOf()}");

        // 单实例：已有边条在跑 → 通知它按 --signal 指定的动作处理，然后本进程退出。
        // --signal 取值：toggle（默认，与热键同义）| rail-hide | rail-show | rail-expand | quit
        //   rail-hide   折叠条收到屏右缘（只留 peek 宽）
        //   rail-show   隐藏态滑回显示态窄条
        //   rail-expand 窄条展开成仪表盘边条（与窄条底部向左箭头同义）
        //   quit        请已有边条退出（ECHO 正常停止 / stop.ps1 发的是同一条管道命令）
        var signal = (ArgValue(args, "--signal") ?? "toggle").Trim();
        if (TrySignalExistingSidebar(signal))
        {
            Log($"existing sidebar signaled ({signal}); exiting");
            return;
        }
        // "quit" 是"请**已有**那个边条退出"。没人接的时候必须原地退出 ——
        // 否则手工敲 `echo-sidebar.exe --signal=quit`（或未来某个脚本这么写）会为了退出
        // 而**新开一个窗口**（黑色幽默级 bug）。
        if (string.Equals(signal, "quit", StringComparison.OrdinalIgnoreCase))
        {
            Log("quit signal: no existing sidebar to signal; nothing to do");
            return;
        }

        var form = new SidebarForm(url, collapsed, width, railPath);
        StartToggleListener(form);
        // 兜底自愈：服务被强杀/崩溃时没人发 quit，边条自己探活退出。
        // 放在 Application.Run 之前起，`--probe` 那条诊断路径在上面已经 return，不受影响。
        new SidebarHeartbeat(url, () => form.CloseBecauseServiceStopped("heartbeat")).Start();
        Application.Run(form);
    }

    /// <summary>从注册表取主屏 DPI（不依赖窗口句柄，启动早期也能拿到）。</summary>
    private static string DeviceDpiOf()
    {
        try
        {
            using var key = Microsoft.Win32.Registry.CurrentUser.OpenSubKey(@"Control Panel\Desktop\WindowMetrics");
            var applied = key?.GetValue("AppliedDPI");
            return applied is int dpi ? $"{dpi} ({(int)Math.Round(dpi / 96.0 * 100)}%)" : "unknown";
        }
        catch { return "unknown"; }
    }

    private static string ArgValue(string[] args, string name)
    {
        foreach (var a in args)
        {
            if (a.StartsWith(name + "=", StringComparison.OrdinalIgnoreCase)) return a[(name.Length + 1)..];
        }
        return null;
    }

    private static int? ArgInt(string[] args, string name)
    {
        var raw = ArgValue(args, name);
        return int.TryParse(raw, out var v) ? v : null;
    }

    /// <summary>
    /// 定位折叠条页面 rail.html。
    /// 依次尝试：显式 --rail 参数 → exe 同目录 → ECHO 项目 web 目录（沿可执行路径向上找）。
    /// 找不到则返回 null，由调用方使用内嵌兜底页。
    /// </summary>
    private static string ResolveRailPath(string explicitPath)
    {
        var candidates = new List<string>();
        if (!string.IsNullOrWhiteSpace(explicitPath)) candidates.Add(explicitPath);
        candidates.Add(Path.Combine(AppContext.BaseDirectory, "rail.html"));
        // exe 通常在 <ECHO>\sidebar\bin\Release\net7.0-windows\win-x64\ ，向上找 <ECHO>\web\rail.html
        var dir = new DirectoryInfo(AppContext.BaseDirectory);
        for (var i = 0; i < 8 && dir != null; i++, dir = dir.Parent)
        {
            candidates.Add(Path.Combine(dir.FullName, "web", "rail.html"));
        }
        foreach (var c in candidates)
        {
            try { if (File.Exists(c)) return Path.GetFullPath(c); } catch { /* 忽略非法路径 */ }
        }
        return null;
    }

    private static bool TrySignalExistingSidebar(string command)
    {
        try
        {
            using var client = new NamedPipeClientStream(".", SidebarConfig.PipeName, PipeDirection.Out, PipeOptions.None);
            client.Connect(600);
            using var writer = new StreamWriter(client, Encoding.UTF8) { AutoFlush = true };
            writer.Write(command);
            return true;
        }
        catch
        {
            return false;
        }
    }

    private static void StartToggleListener(SidebarForm form)
    {
        var worker = new Thread(() =>
        {
            while (true)
            {
                try
                {
                    using var server = new NamedPipeServerStream(SidebarConfig.PipeName, PipeDirection.In, 1);
                    server.WaitForConnection();
                    using var reader = new StreamReader(server, Encoding.UTF8);
                    var command = reader.ReadToEnd();
                    // "quit" 放在最前面判：它是"请退出"，与其它命令没有交集，
                    // 先判可以避免以后新增命令（含 "quit" 子串）时被别的分支吃掉。
                    if (command.Contains("quit", StringComparison.OrdinalIgnoreCase))
                    {
                        // ECHO 正常停止的通知（app/runtime.py:quit_sidebar）或 --signal=quit。
                        // 仍然是"先改标题说一句、2 秒后关窗"，与心跳兜底走**同一条**收尾路径。
                        form.BeginInvoke(new Action(() => form.CloseBecauseServiceStopped("ipc quit")));
                    }
                    else if (command.Contains("rail-expand", StringComparison.OrdinalIgnoreCase))
                    {
                        form.BeginInvoke(new Action(() => form.ExpandFromRail("signal")));
                    }
                    else if (command.Contains("rail-hide", StringComparison.OrdinalIgnoreCase))
                    {
                        form.BeginInvoke(new Action(form.HideRail));
                    }
                    else if (command.Contains("rail-show", StringComparison.OrdinalIgnoreCase))
                    {
                        form.BeginInvoke(new Action(() => form.ShowRail("signal")));
                    }
                    else if (command.Contains("toggle", StringComparison.OrdinalIgnoreCase))
                    {
                        form.BeginInvoke(new Action(form.ToggleFromHotkey));
                    }
                }
                catch
                {
                    Thread.Sleep(500);
                }
            }
        })
        { IsBackground = true, Name = "echo-sidebar-toggle-listener" };
        worker.Start();
    }
}

/// <summary>右缘仪表盘边条窗口。</summary>
internal sealed class SidebarForm : Form
{
    private const int WM_NCHITTEST = 0x0084;
    private const int HTCLIENT = 1;
    private const int HTLEFT = 10;

    // **两页常驻**（2026-10-23 用户："折叠后再展开要等几秒"）：
    // 主面板与折叠条各用一支 WebView，收起/展开只切可见性 —— **不再重新导航**。
    // 原来的做法是每次展开都 `Navigate(_baseUrl)`，而主面板首屏要聚合 /api/status、
    // /api/settings 等（其中几个 2 秒级），于是每次都由白屏重新等一遍。
    private readonly WebView2 _web = new();
    private readonly WebView2 _railWeb = new();
    private bool _panelReady;          // 主面板导航成功过（失败时展开前补一次导航）
    private bool _railReady;           // 折叠条页导航成功过
    private int _panelRetry;           // 主面板导航重试计数
    private System.Windows.Forms.Timer _panelRetryTimer;
    private readonly string _baseUrl;
    private readonly string _railPath;      // 折叠条页面本地路径（兜底用）
    private readonly string _railUrl;       // 折叠条页面 HTTP 地址（与 API 同源，优先使用）
    private readonly bool _startCollapsed;
    private bool _railMode;
    private bool _docking;
    private int _pendingWidth;
    private System.Windows.Forms.Timer _dockTimer;
    private int _railRetry;                              // 折叠条页面加载重试计数
    private System.Windows.Forms.Timer _railRetryTimer;  // 重试定时器（ECHO 重启期间用）

    // ---- 窄条显示/隐藏（2026-09-12 用户改为：不再自动隐藏；点底部箭头隐藏，鼠标贴屏右缘唤回）----
    private System.Windows.Forms.Timer _railTimer;   // 200ms 轮询：隐藏态下判断鼠标是否贴到屏右缘
    private bool _railVisible = true;                // 窄条当前是否在屏内（隐藏态只留 RailPeekWidth）
    private bool _railWakeArmed = true;              // 隐藏后必须先把鼠标移出贴边带，贴边才唤回（否则点箭头后立刻弹回）

    // ---- 悬停唤回的"停留计时"（2026-10-23 用户：一碰就弹会被滚动条误触）----
    private bool _railHovering;        // 鼠标已在贴边带里、正在累计停留时间
    private DateTime _railHoverAt;     // 本轮停留的起点
    private Point _railHoverAnchor;    // 起点处的鼠标位置（移动超过抖动阈值就重新计时）
    private bool _railShownByHover;    // 这次滑出是"悬停"引起的 → 鼠标离开一段时间后自动收回
    private DateTime? _railLeaveAt;    // 悬停滑出后，鼠标离开窄条区域的起点

    public SidebarForm(string url, bool collapsed, int width, string railPath = null)
    {
        _baseUrl = url;
        _railPath = railPath;
        // 折叠条页面走 ECHO 的 HTTP 服务：与 API 同源，不需要 CORS，也不受来源守卫影响
        _railUrl = string.IsNullOrEmpty(url) ? null : url.TrimEnd('/') + "/web/rail.html";
        _startCollapsed = collapsed;
        _pendingWidth = width < SidebarConfig.MinPanelWidth ? SidebarConfig.DefaultWidth : width;
        _railVisible = true;
        // 先把模式与位置定下来再显示：否则 Application.Run 会先把 300x300 的窗体在
        // **左上角**闪一下，再被 InitAsync 吸到屏右缘（用户会看到窗口"跳"一次）。
        _railMode = _startCollapsed;

        FormBorderStyle = FormBorderStyle.None;
        ShowInTaskbar = false;
        TopMost = true;
        StartPosition = FormStartPosition.Manual;
        Text = "ECHO 仪表盘";
        BackColor = Color.FromArgb(24, 24, 27);

        // 左侧留出把手：宽度 = 客户区 - 把手，WebView2 不再覆盖那条边。
        // 两支 WebView 叠在同一块客户区里，同一时刻只有一支可见（收起 = 折叠条页，
        // 展开 = 主面板页）—— 这样收起/展开只是切可见性，不需要重新加载页面。
        foreach (var web in new[] { _web, _railWeb })
        {
            web.Dock = DockStyle.None;
            web.Anchor = AnchorStyles.Top | AnchorStyles.Bottom | AnchorStyles.Left | AnchorStyles.Right;
            Controls.Add(web);
            web.MouseDown += OnWebViewMouseDown;
        }
        _railWeb.Visible = false;

        Load += async (_, _) => await InitAsync();
        Resize += (_, _) => OnResized();
        // 只有"用户拖拽结束"才是一次真正的宽度调整：Resize/Move 在程序自己设置 Bounds、
        // 子控件布局时都会触发，用它们记录用户宽度会把 --width 覆盖掉（2026-09-12 实测）。
        ResizeEnd += (_, _) => RememberUserWidth();

        // 显示前先贴到屏右缘（见上面 `_railMode` 处的注释）。
        DockToRightEdge();
    }

    /// <summary>用户拖拽左边框结束：记住宽度（换算回逻辑像素）并按新宽度重新贴右缘。</summary>
    private void RememberUserWidth()
    {
        if (_docking || _railMode) return;
        // Width 是物理像素；记忆值统一存逻辑像素（与 --width 参数同一空间）
        _pendingWidth = Math.Max(SidebarConfig.MinPanelWidth, (int)Math.Round(Width / DpiScale()));
        Program.Log($"user width remembered: {_pendingWidth} (physical={Width})");
        Program.SaveRememberedWidth(_pendingWidth);
        DockToRightEdge();
    }

    /// <summary>
    /// 把 WebView2 放到"把手右侧"：ClientSize 是物理像素，把手宽度按 DPI 换算，
    /// 保证"逻辑 6px 把手"在各缩放下视觉一致。
    /// </summary>
    private void LayoutWebView()
    {
        // 窄条态：WebView 占满整宽（折叠条内容需要完整宽度，且已取消"点击展开"把手区）。
        // 展开态：左边缘留 GripWidth（逻辑）把手给窗体，用于拖拽调宽。
        // 滑出/滑入动画期间（窄条隐藏/出现、折叠条 ⇄ 边条都是）：WebView 宽度固定、Left=0
        // （展开态再让出 GripWidth 把手，与动画结束后的布局完全一致，收尾不跳），跟着窗口左缘一起动，
        // 超出窗体的部分被裁剪 → 视觉上是"抽屉从屏幕右缘滑出/滑入"，内容是整体平移不重排。
        if (_railSlideWidth > 0)
        {
            var gripNow = _railMode ? 0 : ScaleLogical(SidebarConfig.GripWidth);
            var wasSliding = _docking;
            _docking = true;
            try
            {
                var bounds = new Rectangle(gripNow, 0, Math.Max(1, _railSlideWidth - gripNow), ClientSize.Height);
                _web.Bounds = bounds;
                _railWeb.Bounds = bounds;
            }
            finally { _docking = wasSliding; }
            return;
        }
        var grip = _railMode ? 0 : ScaleLogical(SidebarConfig.GripWidth);
        // 关键：设置子控件位置会触发窗体的 Resize/Layout，若不加 _docking 保护，
        // OnResized 会把"窗体当前宽度(初始 300)"误当成用户宽度记下 → --width=450 被覆盖成
        // 最小值 320（2026-09-12 实测：docked wanted=320，就是这么来的）。
        var wasDocking = _docking;
        _docking = true;
        try
        {
            var bounds = new Rectangle(grip, 0, Math.Max(0, ClientSize.Width - grip), ClientSize.Height);
            _web.Bounds = bounds;
            _railWeb.Bounds = bounds;
        }
        finally { _docking = wasDocking; }
    }

    /// <summary>鼠标左键按下：已取消"点击展开"（用户 2026-09-12 要求折叠条做成功能条）。</summary>
    protected override void OnMouseDown(MouseEventArgs e)
    {
        base.OnMouseDown(e);
    }

    private void OnWebViewMouseDown(object sender, MouseEventArgs e)
    {
        // 保留占位：折叠条上的控件由页面自己处理，窗口层不再做任何点击动作
    }

    /// <summary>
    /// 从折叠条展开成仪表盘边条：窄条底部向左箭头（页面消息 "rail-expand"）或
    /// `--signal rail-expand` 触发。展开会重新加载主面板，而主面板启动即停在仪表盘页签。
    /// </summary>
    internal void ExpandFromRail(string reason)
    {
        Program.Log($"expand from rail: {reason}");
        Expand();
    }

    private int ScaleLogical(int value) => (int)Math.Round(value * (DeviceDpi / 96.0));

    // ---- 服务没了（正常停止的 quit / 心跳判定）→ 自己退，不弹错 ----
    private bool _closingForServiceStop;

    /// <summary>
    /// 服务已经停了：先把标题改成一句人话（"ECHO 已停止"），**2 秒后**关窗退出。
    ///
    /// 为什么是"改标题 + 等 2 秒"而不是直接消失：用户刚点了「关机」，边条突然蒸发会让人
    /// 怀疑是不是崩了；给一句话再看它离开更符合预期。同理**绝不弹 MessageBox**
    /// —— 这不是错误，而且这条路径可能在无人值守（关机 / 服务崩溃）时触发。
    /// 线程：管道监听与心跳都在后台线程 → 这里负责切回 UI 线程；重复调用只安排一次。
    /// </summary>
    internal void CloseBecauseServiceStopped(string reason)
    {
        if (IsDisposed) return;
        if (InvokeRequired)
        {
            try { BeginInvoke(new Action(() => CloseBecauseServiceStopped(reason))); } catch { /* 窗体已在关 */ }
            return;
        }
        if (_closingForServiceStop) return;
        _closingForServiceStop = true;
        Program.Log($"closing sidebar: service stopped ({reason})");
        try { Text = "ECHO 已停止"; } catch { /* 标题只是提示，失败不影响退出 */ }
        var timer = new System.Windows.Forms.Timer { Interval = 2000 };
        timer.Tick += (_, _) =>
        {
            timer.Stop();
            try { Close(); } catch (Exception ex) { Program.Log("close failed: " + ex.Message); }
        };
        timer.Start();
    }

    protected override void WndProc(ref Message m)
    {
        base.WndProc(ref m);
        if (m.Msg == WM_NCHITTEST && !_railMode && m.Result == (IntPtr)HTCLIENT)
        {
            var pos = PointToClient(new Point(m.LParam.ToInt32() & 0xFFFF, (m.LParam.ToInt32() >> 16) & 0xFFFF));
            if (pos.X <= ScaleLogical(SidebarConfig.GripWidth))
            {
                m.Result = (IntPtr)HTLEFT;   // 左边缘 → 系统 resize 光标 + 拖拽调宽
            }
        }
    }

    private async Task InitAsync()
    {
        Program.Log($"form loaded: bounds={Bounds} client={ClientSize} dpi={DeviceDpi}");
        LayoutWebView();
        DockToRightEdge();

        try
        {
            var dataDir = Path.Combine(
                Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
                "echo-sidebar", "webview2");
            Directory.CreateDirectory(dataDir);
            var env = await Microsoft.Web.WebView2.Core.CoreWebView2Environment.CreateAsync(null, dataDir);
            await InitWebViewAsync(_web, env);
            await InitWebViewAsync(_railWeb, env);
        }
        catch (Exception ex)
        {
            Program.Log("WebView2 init failed: " + ex);
            MessageBox.Show(
                "WebView2 初始化失败：" + ex.Message + "\n\n请确认已安装 Microsoft Edge WebView2 Runtime。",
                "ECHO 边条", MessageBoxButtons.OK, MessageBoxIcon.Error);
        }

        _railMode = _startCollapsed;   // 构造函数里已设过；这里保留一次幂等赋值，防止以后挪动顺序
        LayoutWebView();
        DockToRightEdge();
        // **两页都先加载**（收起态也在后台把主面板加载好）：这样第一次展开也不用等。
        LoadPanel();
        LoadRail();
        ApplyModeVisibility();
        Show();
        StartRailWatch();
        Program.Log($"shown: bounds={Bounds} rail={_railMode} pending={_pendingWidth}");
    }

    /// <summary>给一支 WebView 建 CoreWebView2 并挂上同一套事件/设置（主面板与折叠条各一份）。</summary>
    private async Task InitWebViewAsync(WebView2 web, Microsoft.Web.WebView2.Core.CoreWebView2Environment env)
    {
        await web.EnsureCoreWebView2Async(env);
        web.CoreWebView2.Settings.AreDevToolsEnabled = false;
        web.CoreWebView2.Settings.IsStatusBarEnabled = false;
        // 页面 → 宿主的桥：窄条底部箭头用 window.chrome.webview.postMessage(...) 通知宿主。
        // 走 WebView2 内置桥而不是绕 ECHO 的 HTTP + 命名管道，少一跳且不依赖 ECHO 在跑。
        web.CoreWebView2.WebMessageReceived += OnWebMessage;
        web.CoreWebView2.NavigationCompleted += OnNavigationCompleted;
        web.CoreWebView2.NewWindowRequested += (_, e) =>
        {
            e.Handled = true;
            try { Process.Start(new ProcessStartInfo(e.Uri) { UseShellExecute = true }); } catch { }
        };
    }

    /// <summary>
    /// 按当前模式切换**可见的那一页**（收起 = 折叠条页；展开 = 主面板页）。
    /// 两页都常驻，这里只切可见性 + 通知页面"你现在是不是被看着"，页面据此暂停/恢复轮询
    /// （避免收起时主面板还在后台每 3 秒打一轮 /api/status —— 那会让 ECHO 一直忙着）。
    /// </summary>
    private void ApplyModeVisibility()
    {
        if (_railMode)
        {
            _web.Visible = false;
            _railWeb.Visible = true;
            _railWeb.BringToFront();
            TellPage(_web, "echoSetPanelVisible", false);
            TellPage(_railWeb, "__echoSetRailVisible", true);
        }
        else
        {
            _railWeb.Visible = false;
            _web.Visible = true;
            _web.BringToFront();
            TellPage(_railWeb, "__echoSetRailVisible", false);
            TellPage(_web, "echoSetPanelVisible", true);
        }
    }

    /// <summary>页面可能还没加载完：用 ExecuteScriptAsync 调一个**可选**的全局函数（不存在就算了）。</summary>
    private static void TellPage(WebView2 web, string fn, bool value)
    {
        try
        {
            web.CoreWebView2?.ExecuteScriptAsync(
                $"window.{fn} && window.{fn}({(value ? "true" : "false")});");
        }
        catch (Exception ex) { Program.Log($"tell page failed ({fn}): {ex.Message}"); }
    }

    /// <summary>
    /// 处理窄条页面的消息（WebView2 桥）："rail-hide" = 点底部右侧箭头隐藏；
    /// "rail-expand" = 点底部左侧箭头展开成仪表盘边条。
    /// 注意：消息在 UI 线程上到达（WebView2 事件即宿主线程），可直接操作窗体。
    /// </summary>
    private void OnWebMessage(object sender, Microsoft.Web.WebView2.Core.CoreWebView2WebMessageReceivedEventArgs e)
    {
        string msg;
        try { msg = e.TryGetWebMessageAsString(); }
        catch (Exception ex) { Program.Log("web message not a string: " + ex.Message); return; }
        Program.Log("web message: " + msg);
        if (string.Equals(msg, "rail-hide", StringComparison.OrdinalIgnoreCase)) HideRail();
        else if (string.Equals(msg, "rail-show", StringComparison.OrdinalIgnoreCase)) ShowRail("page requested");
        else if (string.Equals(msg, "rail-expand", StringComparison.OrdinalIgnoreCase)) ExpandFromRail("page requested");
        // 展开态仪表盘右下角箭头：把面板收成折叠条（与 rail-hide 的区别是整窗变窄条）
        else if (string.Equals(msg, "rail-collapse", StringComparison.OrdinalIgnoreCase)) Collapse();
    }

    // ------------------------------------------- 窄条：底部箭头隐藏 + 鼠标贴边唤回
    /// <summary>启动窄条监视定时器（200ms 轮询鼠标位置；WebView2 会吞鼠标事件，轮询最可靠）。</summary>
    private void StartRailWatch()
    {
        if (_railTimer != null) return;
        _railTimer = new System.Windows.Forms.Timer { Interval = 200 };
        _railTimer.Tick += (_, _) => RailWatchTick();
        _railTimer.Start();
    }

    /// <summary>鼠标是否贴到屏右缘的"唤回带"（隐藏态只剩 3px 时用）。</summary>
    private bool CursorNearScreenEdge()
    {
        var pos = Cursor.Position;
        var area = PrimaryWorkArea();
        return pos.X >= area.Right - ScaleLogical(SidebarConfig.RailWakeMargin)
               && pos.Y >= area.Top && pos.Y <= area.Bottom;
    }

    /// <summary>隐藏窄条：只留 RailPeekWidth 在屏右缘（点底部箭头 / rail-hide 信号触发）。</summary>
    internal void HideRail()
    {
        if (!_railMode || !_railVisible) return;
        _railVisible = false;
        _railHovering = false;
        _railShownByHover = false;
        _railLeaveAt = null;
        // 点箭头时鼠标正贴在屏右缘，若不先解除武装会在下一次 tick 立刻弹回来
        _railWakeArmed = !CursorNearScreenEdge();
        SlideRailTo(ToPhysical(SidebarConfig.RailPeekWidth), "hide");
    }

    /// <summary>唤回/保持窄条显示（贴屏右缘、rail-show 信号）。</summary>
    internal void ShowRail(string reason)
    {
        if (!_railMode || _railVisible) return;
        _railVisible = true;
        _railHovering = false;
        // 默认是"用户自己唤回"的（不自动收回）；悬停唤回那条路会在调用后把标记重新置真。
        _railShownByHover = false;
        _railLeaveAt = null;
        SlideRailTo(ToPhysical(SidebarConfig.RailWidth), reason);
    }

    // ---- 滑出/滑入动画（用户 2026-09-12 要求"鼠标贴边滑出"）----
    // 关键点：动画期间 WebView 宽度固定、Left 跟着窗口左缘走，超出部分由父窗口裁剪，
    // 于是内容整体平移（抽屉从屏幕右缘滑出/滑入）；若让 WebView 跟着窗口一起变窄，
    // 每帧都要重排（内容被压扁再弹开，实测很抖）。
    // 2026-09-13 用户要求"边条 ⇄ 折叠条也改成折叠条隐藏/出现那种滑动动效"，于是同一套动画
    // 现在服务三种切换：窄条显示 ⇄ 隐藏（WebView 固定窄条宽）｜折叠条 ⇄ 边条（WebView 固定面板宽）。
    private System.Windows.Forms.Timer _railAnim;
    private int _railAnimFrom, _railAnimTo, _railAnimStep, _railAnimSteps = RailAnimSteps;
    private int _railSlideWidth;           // >0 = 动画中，WebView 按此宽度摆放（含 grip 偏移的总宽）
    private Action _railAnimDone;          // 收尾回调：收起后再换成窄条页，避免滑动途中就看到被压扁的窄条页
    private const int RailAnimSteps = 9;   // 窄条 9 帧 × 16ms ≈ 145ms（实测含布局约 150~200ms）
    private const int PanelAnimSteps = 12; // 折叠条 ⇄ 边条跨度大（约 400 物理像素），多几帧 ≈ 190ms 更顺
    // 逐帧诊断（验证"确实在滑动"而不是跳变）：设 ECHO_SIDEBAR_ANIMLOG=1 才记录，默认不写日志
    private static readonly bool AnimLogFrames =
        Environment.GetEnvironmentVariable("ECHO_SIDEBAR_ANIMLOG") == "1";

    /// <param name="slideWidthLogical">
    /// 动画期间 WebView 的宽度（逻辑像素，含展开态 grip 偏移的总宽）：窄条滑出/滑入传 RailWidth；
    /// 折叠条 ⇄ 边条传面板宽度 —— WebView 保持面板宽、Left 跟窗口左缘，面板内容整体平移不重排。
    /// </param>
    /// <param name="onDone">动画收尾（Bounds 已归位）后的回调。</param>
    private void SlideRailTo(int toPhysical, string reason, int? slideWidthLogical = null,
                             Action onDone = null, int? steps = null)
    {
        _railAnim?.Stop();
        if (_railAnim == null)
        {
            _railAnim = new System.Windows.Forms.Timer { Interval = 16 };
            _railAnim.Tick += (_, _) => RailAnimTick();
        }
        _railAnimFrom = Width;             // 从当前实际宽度起步，动画中途再触发也能接上
        _railAnimTo = toPhysical;
        _railAnimStep = 0;
        _railAnimSteps = steps ?? RailAnimSteps;
        _railSlideWidth = ToPhysical(slideWidthLogical ?? SidebarConfig.RailWidth);
        _railAnimDone = onDone;
        Program.Log($"rail slide start: {_railAnimFrom} -> {_railAnimTo} physical, web={_railSlideWidth}, " +
                    $"{_railAnimSteps} frames ({reason})");
        _railAnim.Start();
    }

    private void RailAnimTick()
    {
        _railAnimStep++;
        double t = (double)_railAnimStep / _railAnimSteps;
        double eased = 1 - Math.Pow(1 - t, 3);   // ease-out：起步快、收尾稳
        int w = (int)Math.Round(_railAnimFrom + (_railAnimTo - _railAnimFrom) * eased);
        if (AnimLogFrames) Program.Log($"rail anim frame {_railAnimStep}/{_railAnimSteps}: w={w}");
        var area = PrimaryWorkArea();
        _docking = true;
        try
        {
            Bounds = new Rectangle(area.Right - w, area.Top, Math.Max(1, w), area.Height);
            LayoutWebView();
        }
        finally { _docking = false; }

        if (_railAnimStep >= _railAnimSteps)
        {
            _railAnim.Stop();
            _railSlideWidth = 0;
            DockToRightEdge();               // 收尾归位：此时 WebView 宽度与窗口一致
            Program.Log($"rail slide end: width={Width}");
            var done = _railAnimDone;
            _railAnimDone = null;
            if (done != null)
            {
                try { done(); } catch (Exception ex) { Program.Log("rail slide callback failed: " + ex.Message); }
            }
        }
    }

    /// <summary>停止动画并恢复正常布局（切到展开态/重新收起时必须调用，否则动画会继续改 Bounds）。</summary>
    private void StopRailAnim()
    {
        _railAnim?.Stop();
        _railSlideWidth = 0;
        // 丢弃未执行的回调：新动作已接管，旧回调会把页面换成过期内容
        _railAnimDone = null;
    }

    /// <summary>
    /// 窄条状态机（每 200ms 一次）：
    ///   隐藏态（只剩 3px）—— 鼠标先离开贴边带、再贴边并**停住**够久才滑出
    ///     （"停住"= 位移不超过抖动阈值；拖别的窗口滚动条时鼠标一直在动，不会误触）。
    ///   显示态 —— 若这次是"悬停临时滑出"，鼠标离开窄条区域一段时间后自动收回；
    ///     手动点开的、或已经按「展开」变成主面板的，都不自动收回。
    /// 为什么要"先离开再贴边"：点箭头隐藏的瞬间鼠标就在贴边带里，直接判断会立刻弹回。
    /// </summary>
    private void RailWatchTick()
    {
        if (IsDisposed || !_railMode) return;

        // ---- 隐藏态（只剩 RailPeekWidth）：要"停住"够久才滑出 ----
        if (!_railVisible)
        {
            if (!_railWakeArmed)
            {
                _railHovering = false;
                if (!CursorNearScreenEdge()) _railWakeArmed = true;
                return;
            }
            if (!CursorNearScreenEdge()) { _railHovering = false; return; }
            // 有鼠标键按着 = 正在拖拽（典型：拖别的窗口的滚动条，它正好在屏右缘）。
            // 这种"路过"绝不能算悬停 —— 用户 2026-10-23 反馈的就是这一类误触。
            if (Control.MouseButtons != MouseButtons.None)
            {
                _railHovering = false;
                return;
            }

            var pos = Cursor.Position;
            if (!_railHovering)
            {
                _railHovering = true;
                _railHoverAt = DateTime.UtcNow;
                _railHoverAnchor = pos;
                return;
            }
            // 鼠标还在动（典型：拖别的窗口的滚动条）→ 重新计时；停住不动才叫"悬停"。
            if (Math.Abs(pos.X - _railHoverAnchor.X) > SidebarConfig.RailHoverJitterPx ||
                Math.Abs(pos.Y - _railHoverAnchor.Y) > SidebarConfig.RailHoverJitterPx)
            {
                _railHoverAt = DateTime.UtcNow;
                _railHoverAnchor = pos;
                return;
            }
            if ((DateTime.UtcNow - _railHoverAt).TotalMilliseconds >= SidebarConfig.RailHoverDwellMs)
            {
                _railHovering = false;
                ShowRail("hover dwell");
                // ShowRail 会把"临时滑出"标记清掉：这一次要重新标上，鼠标离开后才会自动收回。
                _railShownByHover = true;
                _railLeaveAt = null;
                Program.Log("rail shown by hover dwell");
            }
            return;
        }

        // ---- 显示态：只有"悬停临时滑出"的那一次才自动收回 ----
        // 点过「展开」（变成主面板）就不会再走这里（Expand 会把标记清掉）。
        if (!_railShownByHover) return;
        if (CursorInRailBand()) { _railLeaveAt = null; return; }
        if (_railLeaveAt == null) { _railLeaveAt = DateTime.UtcNow; return; }
        if ((DateTime.UtcNow - _railLeaveAt.Value).TotalMilliseconds >= SidebarConfig.RailAutoHideMs)
        {
            Program.Log("rail auto-hide after hover leave");
            _railLeaveAt = null;
            _railShownByHover = false;
            HideRail();
        }
    }

    /// <summary>鼠标是否在"折叠条那块区域"里（宽 RailWidth、贴右缘、纵向铺满工作区）。</summary>
    private bool CursorInRailBand()
    {
        var pos = Cursor.Position;
        var area = PrimaryWorkArea();
        return pos.X >= area.Right - ToPhysical(SidebarConfig.RailWidth)
               && pos.Y >= area.Top && pos.Y <= area.Bottom;
    }

    /// <summary>加载**主面板**（常驻；收起态也先加载好，展开时立即可见）。失败会自动重试。</summary>
    private void LoadPanel()
    {
        _panelReady = false;
        try
        {
            // 带一个无意义 query：升级后 app.js/app.css 的同名 URL 可能还在 WebView 缓存里，
            // 每次启动换个 URL 就能确保拿到新文件（与折叠条那条 `?t=` 是同一个理由）。
            Program.Log("load panel: " + _baseUrl);
            _web.CoreWebView2?.Navigate(CacheBusted(_baseUrl));
        }
        catch (Exception ex) { Program.Log("navigate panel failed: " + ex.Message); }
    }

    /// <summary>给 URL 加一个每次都不同的 query（`?t=` 或 `&t=`）。</summary>
    private static string CacheBusted(string url)
    {
        if (string.IsNullOrEmpty(url)) return url;
        return url + (url.Contains("?") ? "&" : "?") + "t=" + DateTime.Now.Ticks;
    }

    /// <summary>加载**折叠条页**（与主面板各用一支 WebView，互不覆盖）。</summary>
    private void LoadRail()
    {
        _railReady = false;
        try
        {
            Program.Log($"load rail: retry={_railRetry}");
            // 折叠条改为经 ECHO 的 HTTP 服务加载（与 API 同源）。
            // 为什么不能再用 file://（2026-09-13 CRITICAL-1 修复的连带改动）：
            // API 现在有来源守卫，file:// 页面发出的请求带 Origin: null 会被 403 拒绝
            // （守卫必须拒 null，否则 sandbox iframe 之流又能绕回来）。同源加载后连
            // CORS 都不需要。ECHO 没起来时先重试，重试期间退回本地文件/内嵌兜底。
            if (!string.IsNullOrEmpty(_railUrl) && _railRetry < 6)
            {
                _railWeb.CoreWebView2?.Navigate(_railUrl + "?t=" + DateTime.Now.Ticks);
                return;
            }
            if (!string.IsNullOrEmpty(_railPath) && File.Exists(_railPath))
            {
                var uri = new Uri(_railPath).AbsoluteUri;
                // 换页/重复加载同一地址时 WebView2 不会重新导航，加个无意义 query 强制刷新
                _railWeb.CoreWebView2?.Navigate(uri + "?t=" + DateTime.Now.Ticks);
                return;
            }
            _railWeb.CoreWebView2?.NavigateToString(RailFallbackHtml(SidebarConfig.RailVisualShift));
        }
        catch (Exception ex) { Program.Log("navigate rail failed: " + ex.Message); }
    }

    /// <summary>
    /// 折叠条页面加载失败就重试（ECHO 可能正在启动/重启）；连续失败 6 次后 LoadRail 会
    /// 退回本地文件或内嵌兜底页面（此时守卫会拒掉它的跨域请求，界面会显示"不可达"状态）。
    /// </summary>
    /// <summary>
    /// 导航失败就重试（ECHO 可能正在启动/重启）。**两页各有各的计数**：
    /// 折叠条是在 ECHO 起来之前就要显示的（失败 6 次后退回本地文件/内嵌兜底）；
    /// 主面板可以一直等（收起态看不见，展开时再补），这里给 30 次 × 2 秒 ≈ 1 分钟。
    /// </summary>
    private void OnNavigationCompleted(object sender, Microsoft.Web.WebView2.Core.CoreWebView2NavigationCompletedEventArgs e)
    {
        if (ReferenceEquals(sender, _railWeb.CoreWebView2))
        {
            if (e.IsSuccess)
            {
                _railRetry = 0;
                _railReady = true;
                TellPage(_railWeb, "__echoSetRailVisible", _railWeb.Visible);
                return;
            }
            if (_railRetry >= 6)
            {
                Program.Log($"rail page load gave up after {_railRetry} retries ({e.WebErrorStatus})");
                return;
            }
            _railRetry++;
            Program.Log($"rail page load failed ({e.WebErrorStatus}) retry={_railRetry}");
            if (_railRetryTimer == null)
            {
                _railRetryTimer = new System.Windows.Forms.Timer { Interval = 700 };
                _railRetryTimer.Tick += (_, _) =>
                {
                    _railRetryTimer.Stop();
                    LoadRail();
                };
            }
            _railRetryTimer.Start();
            return;
        }

        // 主面板
        if (e.IsSuccess)
        {
            _panelRetry = 0;
            _panelReady = true;
            TellPage(_web, "echoSetPanelVisible", _web.Visible);
            return;
        }
        _panelReady = false;
        if (_panelRetry >= 30)
        {
            Program.Log($"panel load gave up after {_panelRetry} retries ({e.WebErrorStatus})");
            return;
        }
        _panelRetry++;
        Program.Log($"panel load failed ({e.WebErrorStatus}) retry={_panelRetry}");
        if (_panelRetryTimer == null)
        {
            _panelRetryTimer = new System.Windows.Forms.Timer { Interval = 2000 };
            _panelRetryTimer.Tick += (_, _) =>
            {
                _panelRetryTimer.Stop();
                LoadPanel();
            };
        }
        _panelRetryTimer.Start();
    }

    // ------------------------------------------------------------- 吸附/尺寸
    private static Rectangle PrimaryWorkArea()
    {
        return Screen.PrimaryScreen?.WorkingArea ?? new Rectangle(0, 0, 1920, 1080);
    }

    /// <summary>
    /// 逻辑像素 → 物理像素的换算系数（本机 150% 缩放 = 1.5）。
    /// 为什么需要：本进程声明了 PerMonitorV2（DeviceDpi=144），`Screen.WorkingArea` 给的是
    /// **物理**空间（2560×1528），而把"设计用的逻辑宽度"直接当作 Bounds 的宽度时，
    /// 窗体实际按**逻辑/DIP**空间落位 → 100 逻辑宽变成 150 物理宽，右边缘多出 50 物理像素
    /// 被推出屏外（2026-09-12 用户截图：左侧 logo 紧贴左缘、右侧被裁）。
    /// </summary>
    private double DpiScale() => DeviceDpi / 96.0;

    private int ToPhysical(double logical) => (int)Math.Round(logical * DpiScale());

    private int DesiredWidth()
    {
        var area = PrimaryWorkArea();
        // 展开宽度记忆值是"逻辑像素"（与 --width 参数、用户拖拽输入同一空间）
        var max = Math.Max(SidebarConfig.MinPanelWidth, area.Width / DpiScale() - SidebarConfig.MinLeftPad);
        return (int)Math.Clamp(_pendingWidth, SidebarConfig.MinPanelWidth, max);
    }

    private void DockToRightEdge()
    {
        if (IsDisposed) return;
        var area = PrimaryWorkArea();
        // 窄条有三种状态：显示态 RailWidth / 隐藏态只留 RailPeekWidth / 展开态按记忆宽度。
        // 三者都是"逻辑像素"，统一换算成物理像素后再算 Bounds（见 DpiScale 注释）。
        int logicalW;
        if (_railMode) { logicalW = _railVisible ? SidebarConfig.RailWidth : SidebarConfig.RailPeekWidth; }
        else { logicalW = DesiredWidth(); }
        var w = ToPhysical(logicalW);
        _docking = true;
        try
        {
            Bounds = new Rectangle(area.Right - w, area.Top, w, area.Height);
            LayoutWebView();
        }
        finally { _docking = false; }
        Program.Log($"docked: rail={_railMode} visible={_railVisible} wanted={w} bounds={Bounds} client={ClientSize} dpi={DeviceDpi}");
    }

    private void OnResized()
    {
        // 程序性吸附/子控件布局都会走到这里：只重排把手并保持贴右缘，
        // 绝不在这里记忆宽度（那是 ResizeEnd 的职责）。
        if (_docking) return;
        LayoutWebView();
        if (_railMode) DockToRightEdge();
    }

    // ------------------------------------------------------------- 热键行为
    public void ToggleFromHotkey()
    {
        Program.Log($"toggle from hotkey: visible={Visible} rail={_railMode}");
        if (!Visible || _railMode) { Expand(); return; }
        Collapse();
    }

    public void Expand()
    {
        StopRailAnim();           // 动画中按热键展开：先停掉动画，否则动画会继续按窄条宽度改 Bounds
        _railMode = false;
        _railVisible = true;      // 回到展开态：窄条状态复位
        _railHovering = false;
        _railShownByHover = false;   // 点了展开 = 常驻面板，不再有"离开就收回"
        _railLeaveAt = null;
        if (!Visible) Show();
        // **主面板一直活着**：这里只切可见性，不再 `Navigate` —— 展开是即时的，
        // 不会出现"白屏等几秒才加载完"（2026-10-23 用户反馈）。
        ApplyModeVisibility();
        if (!_panelReady) LoadPanel();     // 启动时 ECHO 还没起来 → 现在补一次
        BringToFront();
        Activate();
        // 与"折叠条隐藏/出现"同一套滑动动效（用户 2026-09-13 要求）：窗口左缘向左滑出面板宽度，
        // WebView 固定为面板宽度、Left 跟着窗口左缘 → 面板内容整体从屏幕右缘滑出，不重排、不闪。
        var panelW = DesiredWidth();
        SlideRailTo(ToPhysical(panelW), "expand", panelW, null, PanelAnimSteps);
    }

    public void Collapse()
    {
        StopRailAnim();
        // 记住"展开态宽度"供下次展开还原。注意 Width 是**物理**像素、_pendingWidth 是**逻辑**像素，
        // 必须除以 DpiScale()：否则每次收起→展开宽度都会乘一次缩放系数
        // （2026-09-12 用户报"宽度记忆没了"：150% 缩放下 400 逻辑 → 收起 → 展开变 600 逻辑 = 900 物理）。
        if (!_railMode) _pendingWidth = Math.Max(SidebarConfig.MinPanelWidth, (int)Math.Round(Width / DpiScale()));
        _railMode = true;
        _railVisible = true;                       // 收起后先显示，之后由底部箭头手动隐藏
        _railHovering = false;
        _railShownByHover = false;                 // 手动收起不算"悬停临时滑出"
        _railLeaveAt = null;
        _railWakeArmed = !CursorNearScreenEdge();
        if (!Visible) Show();
        StartRailWatch();
        // 滑动期间 WebView 仍按面板宽度摆放 → 用户看到的是"面板整体向右滑走"；滑完再切到
        // 折叠条页（两页都常驻，切换只是可见性，不再重新导航）。
        var panelW = DesiredWidth();
        SlideRailTo(ToPhysical(SidebarConfig.RailWidth), "collapse", panelW, () =>
        {
            ApplyModeVisibility();
            if (!_railReady) LoadRail();
        }, PanelAnimSteps);
    }

    /// <summary>兜底窄条页面（找不到 web/rail.html 时用）。padLeft 为左补偿像素。</summary>
    private static string RailFallbackHtml(int padLeft) => $$"""
        <html><head><meta charset="utf-8"><style>
        html,body{margin:0;height:100%;background:#18181b;color:#e4e4e7;
        font:12px/1.6 "Microsoft YaHei",system-ui;display:flex;align-items:center;justify-content:center;
        padding-left:{{padLeft}}px;box-sizing:border-box}
        .t{writing-mode:vertical-rl;letter-spacing:.25em}
        </style></head><body><div class="t">ECHO 仪表盘</div></body></html>
        """;
}
