namespace EchoLauncher.Core;

public enum LauncherMode
{
    Start,
    Stop,
    Help,
}

/// <summary>
/// Command line:
///   ECHO.exe                 start, or focus the already running instance
///   ECHO.exe --stop          stop the running service (used by installer/uninstaller)
///   ECHO.exe --autostart     start without focusing (HKCU Run)
///   ECHO.exe --quiet         never show a blocking MessageBox (CI / installer)
///   ECHO.exe --wait=SECONDS  readiness timeout for the first start (default 90)
///   ECHO.exe --help
/// </summary>
public sealed class LauncherOptions
{
    public LauncherMode Mode { get; init; } = LauncherMode.Start;
    public bool Quiet { get; init; }
    public bool Autostart { get; init; }
    public TimeSpan Wait { get; init; } = TimeSpan.FromSeconds(90);

    public static LauncherOptions? Parse(string[] args, out string? error)
    {
        error = null;
        var mode = LauncherMode.Start;
        bool quiet = false;
        bool autostart = false;
        var wait = TimeSpan.FromSeconds(90);

        foreach (string raw in args ?? Array.Empty<string>())
        {
            string arg = (raw ?? string.Empty).Trim();
            if (arg.Length == 0) continue;
            switch (arg.ToLowerInvariant())
            {
                case "--stop":
                    mode = LauncherMode.Stop;
                    break;
                case "--help":
                case "-h":
                case "/?":
                    mode = LauncherMode.Help;
                    break;
                case "--quiet":
                case "-q":
                    quiet = true;
                    break;
                case "--autostart":
                    autostart = true;
                    break;
                default:
                    if (arg.StartsWith("--wait=", StringComparison.OrdinalIgnoreCase))
                    {
                        string value = arg.Substring("--wait=".Length);
                        if (!int.TryParse(value, out int seconds) || seconds < 5 || seconds > 600)
                        {
                            error = $"等待时间不合法：{value}（5-600 秒）";
                            return null;
                        }
                        wait = TimeSpan.FromSeconds(seconds);
                        break;
                    }
                    error = $"不认识的参数：{arg}";
                    return null;
            }
        }

        if (mode == LauncherMode.Stop) autostart = false;
        return new LauncherOptions { Mode = mode, Quiet = quiet, Autostart = autostart, Wait = wait };
    }

    public static string HelpText =>
        "ECHO.exe                 启动 ECHO（已在运行则唤出面板）\n" +
        "ECHO.exe --stop          停止 ECHO（安装/升级/卸载用）\n" +
        "ECHO.exe --autostart     开机自启用：启动后不主动弹面板\n" +
        "ECHO.exe --quiet         失败时不弹窗，只写日志\n" +
        "ECHO.exe --wait=90       首次启动等待就绪的秒数（5-600）";
}
