// Heartbeat.cs — 边条侧心跳（兜底自愈）：服务没了，边条自己退
//
// 为什么需要它（2026-10-21 真机 bug）：
//   边条是**独立进程** —— `app/runtime.py:_spawn_sidebar()` 用 DETACHED_PROCESS 起它，
//   就是为了 ECHO 重启/被杀时边条能活下来。代价是：ECHO 退出**带不走它**。
//   正常停止时 ECHO 会经命名管道发 "quit"（见 Program.StartToggleListener），可
//   **服务被强杀 / 崩溃 / 掉电**时没人发那条命令 —— 现场就是"8970 不再监听、
//   echo-sidebar.exe 还挂在屏幕上"。
//
// 判据（每 5 秒探一次 <baseUrl>/api/status；连续 3 次失败 → 退）：
//   * 为什么探 HTTP 而不是看"窗口/鼠标/进程"：边条被收起、只留 3px、用户只是最小化，
//     都不代表服务停了；"服务还能不能回答"才是与"服务还在不在"同一个问题。
//   * 为什么前 30 秒的失败不算：边条可能**比服务先起来**（开机自启、ECHO 重启后由
//     autostart 拉起），那几秒探不到是正常的。没有这条宽限期，边条会"刚起来就自己退了"。
//   * 为什么成功一次就清零：服务在重启中抖两下，不该被当成"死了"。
using System.Diagnostics;

namespace EchoSidebar;

/// <summary>
/// 心跳判据：**纯函数、无 I/O、无 UI**。
/// 单独放一处，才能把"几次算死 / 宽限期 / 恢复清零"讲清楚，也才能被
/// `tests/test_sidebar_lifecycle.py` 直接钉住（那边不跑 .NET，按源码+常量核对）。
/// </summary>
internal static class HeartbeatPolicy
{
    /// <summary>探测间隔（毫秒）。</summary>
    public const int IntervalMs = 5000;

    /// <summary>连续失败多少次 → 边条退出。</summary>
    public const int FailureThreshold = 3;

    /// <summary>启动宽限期（毫秒）：这段时间里的失败**不计数**（边条可能比服务先起来）。</summary>
    public const int StartupGraceMs = 30000;

    /// <summary>一次探测结果 → 下一次的"连续失败"计数。</summary>
    public static int NextFailureCount(int failures, bool reachable, long elapsedMs)
    {
        if (reachable) return 0;                      // 服务恢复一次 → 计数清零
        if (elapsedMs < StartupGraceMs) return 0;     // 宽限期内的失败不计
        return failures + 1;
    }

    /// <summary>该不该自己退出：宽限期已过 <b>且</b> 连续失败到了阈值。</summary>
    public static bool ShouldExit(long elapsedMs, int failures)
    {
        return elapsedMs >= StartupGraceMs && failures >= FailureThreshold;
    }
}

/// <summary>
/// 心跳线程：定时探活，判定服务没了就回调（收尾动作交给宿主的 UI 线程）。
/// 刻意**不弹任何 MessageBox**：服务停掉时再糊一堆错误框只是噪音，而且这条路径可能在
/// 无人值守（关机 / 崩溃）时触发。
/// </summary>
internal sealed class SidebarHeartbeat
{
    /// <summary>
    /// 探活用同一个 HttpClient。
    /// **UseProxy=false 是必须的**（2026-10-21 真机实测）：默认会走系统/环境里的代理，
    /// 于是"连本机 8970"也先发给代理 —— 服务停着时每次探测都要等满 2 秒超时，
    /// 心跳节拍从 5 秒变成 7 秒（宽限后的 3 次失败要拖到 50 秒才退）；
    /// 而且把"探本机存活"这种请求交给外部代理根本没有意义。回环地址就该直连。
    /// </summary>
    private static readonly HttpClient Http =
        new(new HttpClientHandler { UseProxy = false }) { Timeout = TimeSpan.FromSeconds(2) };

    private readonly string _statusUrl;
    private readonly Action _onGaveUp;
    private Thread _worker;

    /// <param name="baseUrl">边条打开的 ECHO 地址（与窗体同一个，不另配一份）。</param>
    /// <param name="onGaveUp">判定"服务没了"之后要做的事（宿主负责切回 UI 线程）。</param>
    public SidebarHeartbeat(string baseUrl, Action onGaveUp)
    {
        // 探 /api/status：与面板同一个"服务活着"的口径 —— 它证明的不只是端口在听，
        // 而是 API 层能正常回答（边条显示的就是这个面板）。
        _statusUrl = (baseUrl ?? "").TrimEnd('/') + "/api/status";
        _onGaveUp = onGaveUp;
    }

    public void Start()
    {
        if (_worker != null) return;                  // 只起一次（重复调用是配置错误，不是退出信号）
        _worker = new Thread(Run) { IsBackground = true, Name = "echo-sidebar-heartbeat" };
        _worker.Start();
    }

    private void Run()
    {
        // 时钟从**心跳启动**算起：宽限期是"边条自己刚起来"的宽限，与 ECHO 何时启动无关。
        var clock = Stopwatch.StartNew();
        var failures = 0;
        while (true)
        {
            var tickAt = clock.ElapsedMilliseconds;   // 这一拍"计划发生"的时刻（判据用它）
            var reachable = Probe();
            var before = failures;
            failures = HeartbeatPolicy.NextFailureCount(failures, reachable, tickAt);
            if (!reachable && failures != before)
            {
                Program.Log($"heartbeat: /api/status unreachable (failures={failures}, elapsed={tickAt}ms)");
            }
            if (HeartbeatPolicy.ShouldExit(tickAt, failures))
            {
                Program.Log($"heartbeat: service gone ({failures} consecutive failures) -> closing sidebar");
                try { _onGaveUp?.Invoke(); }
                catch (Exception ex) { Program.Log("heartbeat callback failed: " + ex.Message); }
                return;
            }
            // 探测本身也要时间（2026-10-21 真机实测：这台机器上"连一个没人监听的本地端口"
            // 要 2.2 秒才被拒，裸 TcpClient 也一样），所以下一拍要**扣掉**已花掉的时间 ——
            // 否则"每 5 秒探一次"会退化成 7 秒一次，3 次失败判定也就跟着慢一拍。
            var spent = clock.ElapsedMilliseconds - tickAt;
            var wait = HeartbeatPolicy.IntervalMs - spent;
            Thread.Sleep(wait > 0 ? (int)wait : 0);
        }
    }

    /// <summary>探一次 /api/status：任何异常都算"探不到"（超时、连接被拒、DNS、TLS…）。</summary>
    private bool Probe()
    {
        try
        {
            using var resp = Http.GetAsync(_statusUrl).GetAwaiter().GetResult();
            return resp.IsSuccessStatusCode;
        }
        catch
        {
            // 不打日志：这里是每 5 秒一次的常态（服务没起来时），计数与判定由 Run 统一记录。
            return false;
        }
    }
}
