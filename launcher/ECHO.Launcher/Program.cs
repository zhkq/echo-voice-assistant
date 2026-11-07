using System.Diagnostics;
using System.Text;
using EchoLauncher.Core;

namespace EchoLauncher;

internal static class Program
{
    private const int ExitOk = 0;
    private const int ExitFailed = 1;
    private const int ExitBusy = 2;
    private const int ExitTimeout = 3;
    private const int ExitUsage = 64;

    [STAThread]
    private static async Task<int> Main(string[] args)
    {
        LauncherOptions? options = LauncherOptions.Parse(args, out string? parseError);
        if (options is null)
        {
            NativeMethods.MessageBox(IntPtr.Zero, parseError ?? "参数不合法", "ECHO",
                NativeMethods.MB_OK | NativeMethods.MB_ICONERROR | NativeMethods.MB_SETFOREGROUND);
            return ExitUsage;
        }

        string exePath = Environment.ProcessPath
                         ?? Process.GetCurrentProcess().MainModule?.FileName
                         ?? "";
        string localAppData = Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData);
        LauncherLayout layout = LauncherLayout.Discover(
            exePath,
            localAppData,
            GetEnv("ECHO_BASE"),
            GetEnv("ECHO_DATA"),
            GetEnv("ECHO_PYTHON"));
        var log = new LauncherLog(layout.LauncherLog);
        log.Write($"launcher mode={options.Mode} exe={layout.ExePath}");
        log.Write($"layout installed={layout.InstalledLayout} code={layout.CodeRoot} " +
                  $"state={layout.StateRoot} data={layout.DataRoot} runtime={layout.RuntimePythonw ?? "(missing)"}");

        if (options.Mode == LauncherMode.Help)
        {
            NativeMethods.MessageBox(IntPtr.Zero, LauncherOptions.HelpText, "ECHO",
                NativeMethods.MB_OK | NativeMethods.MB_SETFOREGROUND);
            return ExitOk;
        }

        using var api = new ApiClient();
        try
        {
            if (options.Mode == LauncherMode.Stop)
            {
                return await StopAsync(layout, log, api);
            }
            return await StartAsync(layout, log, api, options);
        }
        catch (Exception ex)
        {
            Fail(log, options, "启动器发生未处理的错误：" + ex.Message,
                LauncherLog.Tail(log.Path));
            return ExitFailed;
        }
    }

    private static async Task<int> StartAsync(
        LauncherLayout layout, LauncherLog log, ApiClient api, LauncherOptions options)
    {
        IReadOnlyList<string> problems = layout.Validate();
        if (problems.Count > 0)
        {
            Fail(log, options, "ECHO 安装不完整：\n" + string.Join("\n", problems),
                LauncherLog.Tail(log.Path));
            return ExitFailed;
        }

        int port = PortFile.Read(layout.DataRoot);
        log.Write($"probe existing instance on port {port}");
        if (await api.IsUpAsync(port, TimeSpan.FromSeconds(2)))
        {
            log.Write("already running; focusing");
            if (!options.Autostart) Focus(layout, port, log);
            return ExitOk;
        }

        string mutexName = LauncherMutex.Name(layout.StateRoot);
        using var mutex = new Mutex(initiallyOwned: true, mutexName, out bool createdNew);
        if (!createdNew)
        {
            // Another launcher is starting ECHO.  Wait for it instead of spawning a second
            // pythonw (ECHO's own port guard would reject it anyway, leaving a confusing log).
            log.Write("another launcher owns the mutex; waiting for readiness");
            var waitUntil = DateTime.UtcNow + TimeSpan.FromSeconds(30);
            while (DateTime.UtcNow < waitUntil)
            {
                port = PortFile.Read(layout.DataRoot);
                if (await api.IsUpAsync(port, TimeSpan.FromSeconds(1)))
                {
                    log.Write("the other launcher brought ECHO up; focusing");
                    if (!options.Autostart) Focus(layout, port, log);
                    return ExitOk;
                }
                await Task.Delay(500);
            }
            Fail(log, options,
                "ECHO 正在启动，但 30 秒内没有就绪。请在任务管理器结束残留的 ECHO/pythonw 后再试。",
                LauncherLog.Tail(log.Path));
            return ExitFailed;
        }

        var startInfo = new ProcessStartInfo
        {
            FileName = layout.RuntimePythonw!,
            Arguments = "-m app.main",
            WorkingDirectory = layout.CodeRoot,
            UseShellExecute = false,
            CreateNoWindow = true,
            WindowStyle = ProcessWindowStyle.Hidden,
            RedirectStandardOutput = true,
            RedirectStandardError = true,
            StandardOutputEncoding = new UTF8Encoding(encoderShouldEmitUTF8Identifier: false),
            StandardErrorEncoding = new UTF8Encoding(encoderShouldEmitUTF8Identifier: false),
        };
        ApplyEnvironment(startInfo, layout);

        using var process = new Process { StartInfo = startInfo, EnableRaisingEvents = true };
        process.OutputDataReceived += (_, e) =>
        {
            if (e.Data is not null) log.Write("[echo] " + Redact(e.Data));
        };
        process.ErrorDataReceived += (_, e) =>
        {
            if (e.Data is not null) log.Write("[echo!] " + Redact(e.Data));
        };

        try
        {
            if (!process.Start())
            {
                Fail(log, options, "无法启动 ECHO 进程（Process.Start 返回 false）",
                    LauncherLog.Tail(log.Path));
                return ExitFailed;
            }
        }
        catch (Exception ex)
        {
            Fail(log, options, "无法启动 ECHO：\n" + ex.Message + $"\n\n运行时：{layout.RuntimePythonw}",
                LauncherLog.Tail(log.Path));
            return ExitFailed;
        }

        process.BeginOutputReadLine();
        process.BeginErrorReadLine();
        log.Write($"started pythonw pid={process.Id} runtime={layout.RuntimePythonw}");

        using JobObject? job = JobObject.TryCreate(log);
        if (job is not null && !job.Assign(process))
        {
            // Non-fatal: the service can still stop its own children, but be honest in the log.
            log.Write("job object: could not assign ECHO to the job (child cleanup is best-effort)");
        }

        DateTime deadline = DateTime.UtcNow + options.Wait;
        bool ready = false;
        var lastProbe = DateTime.MinValue;
        while (DateTime.UtcNow < deadline)
        {
            if (process.HasExited)
            {
                process.WaitForExit();
                Fail(log, options,
                    $"ECHO 启动后立即退出（退出码 {process.ExitCode}）。\n" +
                    "常见原因：运行时依赖不全、数据目录不可写、端口被占用。",
                    LauncherLog.Tail(log.Path));
                return ExitFailed;
            }

            int actual = PortFile.Read(layout.DataRoot, port);
            if (actual != port)
            {
                port = actual;
                log.Write($"ECHO picked port {port}");
            }

            if (DateTime.UtcNow - lastProbe >= TimeSpan.FromSeconds(1))
            {
                lastProbe = DateTime.UtcNow;
                if (await api.IsUpAsync(port, TimeSpan.FromSeconds(1)))
                {
                    ready = true;
                    break;
                }
            }
            await Task.Delay(300);
        }

        if (!ready)
        {
            Fail(log, options,
                $"ECHO 在 {options.Wait.TotalSeconds:0} 秒内没有就绪（端口 {port}）。\n" +
                "请看日志；如果是在干净机器上首次启动，请确认运行时依赖已随安装包就位。",
                LauncherLog.Tail(log.Path));
            TryKillTree(process);
            return ExitTimeout;
        }

        log.Write($"ECHO ready on http://127.0.0.1:{port}/ (pid={process.Id})");
        // Stay resident: the job object must outlive pythonw.  When ECHO stops (panel ⏻,
        // installer --stop, or a crash) this returns and the job closes, taking any
        // detached children (sidebar / backend / DSH) with it.
        await process.WaitForExitAsync();
        process.WaitForExit();
        log.Write($"ECHO exited code={process.ExitCode}");
        return process.ExitCode == 0 ? ExitOk : ExitFailed;
    }

    private static async Task<int> StopAsync(LauncherLayout layout, LauncherLog log, ApiClient api)
    {
        int port = PortFile.Read(layout.DataRoot);
        if (!await api.IsUpAsync(port, TimeSpan.FromSeconds(2)))
        {
            log.Write("stop: ECHO is not listening; nothing to do");
            return ExitOk;
        }

        StopResult result = await api.StopAsync(port, TimeSpan.FromSeconds(20));
        log.Write($"stop: code={result.Code} message={Redact(result.Message)}");
        if (result.Code == ExitBusy) return ExitBusy;
        if (result.Code != ExitOk)
        {
            // The request may have raced with a shutdown; if the port is already closed we
            // succeeded even though the HTTP call reported an error.
            if (!await api.IsUpAsync(port, TimeSpan.FromSeconds(1))) return ExitOk;
            return ExitTimeout;
        }

        var deadline = DateTime.UtcNow + TimeSpan.FromSeconds(60);
        while (DateTime.UtcNow < deadline)
        {
            if (!await api.IsUpAsync(port, TimeSpan.FromSeconds(1)))
            {
                log.Write("stop: port closed");
                return ExitOk;
            }
            await Task.Delay(500);
        }

        log.Write("stop: timed out waiting for the port to close");
        return ExitTimeout;
    }

    private static void Focus(LauncherLayout layout, int port, LauncherLog log)
    {
        string url = $"http://127.0.0.1:{port}/";
        string? sidebar = layout.SidebarCandidates.FirstOrDefault(File.Exists);
        if (sidebar is not null && DesktopRuntimePresent())
        {
            try
            {
                var startInfo = new ProcessStartInfo
                {
                    FileName = sidebar,
                    Arguments = $"--signal=rail-expand --url={url}",
                    UseShellExecute = false,
                    CreateNoWindow = true,
                    WindowStyle = ProcessWindowStyle.Hidden,
                };
                if (Process.Start(startInfo) is not null)
                {
                    log.Write("focus: sidebar signaled");
                    return;
                }
            }
            catch (Exception ex)
            {
                log.Write("focus: sidebar failed (" + ex.Message + "); falling back to browser");
            }
        }

        try
        {
            Process.Start(new ProcessStartInfo(url) { UseShellExecute = true });
            log.Write("focus: browser opened");
        }
        catch (Exception ex)
        {
            log.Write("focus: browser failed (" + ex.Message + ")");
        }
    }

    private static void ApplyEnvironment(ProcessStartInfo startInfo, LauncherLayout layout)
    {
        startInfo.Environment["ECHO_ROOT"] = layout.CodeRoot;
        if (layout.UseStateEnvironment)
        {
            startInfo.Environment["ECHO_BASE"] = layout.StateRoot;
            startInfo.Environment["ECHO_DATA"] = layout.DataRoot;
        }
        startInfo.Environment["PYTHONUTF8"] = "1";
        startInfo.Environment["PYTHONIOENCODING"] = "utf-8";
        // A user-level PYTHONHOME/PYTHONSTARTUP can make the bundled CPython look for a
        // different stdlib and fail before app.main even imports.
        startInfo.Environment.Remove("PYTHONHOME");
        startInfo.Environment.Remove("PYTHONSTARTUP");
    }

    private static bool DesktopRuntimePresent()
    {
        var roots = new List<string>();
        void Add(string? value)
        {
            if (!string.IsNullOrWhiteSpace(value)) roots.Add(value);
        }

        Add(Environment.GetEnvironmentVariable("DOTNET_ROOT"));
        Add(Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.ProgramFiles), "dotnet"));
        Add(Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
            "Microsoft", "dotnet"));
        string? programFilesX86 = Environment.GetEnvironmentVariable("ProgramFiles(x86)");
        Add(programFilesX86 is null ? null : Path.Combine(programFilesX86, "dotnet"));

        return roots.Any(root => Directory.Exists(
            Path.Combine(root, "shared", "Microsoft.WindowsDesktop.App")));
    }

    private static void TryKillTree(Process process)
    {
        try
        {
            if (!process.HasExited) process.Kill(entireProcessTree: true);
        }
        catch
        {
        }
    }

    private static void Fail(LauncherLog log, LauncherOptions options,
        string message, string? tail = null)
    {
        log.Write("ERROR " + Redact(message).Replace("\r", " ").Replace("\n", " | "));
        if (options.Quiet || options.Autostart) return;

        string text = message;
        if (!string.IsNullOrWhiteSpace(tail))
        {
            text += "\n\n最近日志：\n" + tail;
        }
        text += "\n\n日志文件：" + log.Path;
        if (text.Length > 1800) text = text[..1800];
        NativeMethods.MessageBox(IntPtr.Zero, text, "ECHO",
            NativeMethods.MB_OK | NativeMethods.MB_ICONERROR | NativeMethods.MB_SETFOREGROUND);
    }

    private static string Redact(string? text)
    {
        string value = text ?? "";
        int index = value.IndexOf("token=", StringComparison.OrdinalIgnoreCase);
        if (index < 0) return value;
        int end = index + "token=".Length;
        int stop = end;
        while (stop < value.Length && !char.IsWhiteSpace(value[stop]) && value[stop] != '&')
        {
            stop++;
        }
        return value[..end] + "***" + value[stop..];
    }

    private static string? GetEnv(string name)
    {
        string? value = Environment.GetEnvironmentVariable(name);
        return string.IsNullOrWhiteSpace(value) ? null : value;
    }
}
