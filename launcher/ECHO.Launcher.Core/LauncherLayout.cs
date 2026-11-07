using System.Runtime.InteropServices;

namespace EchoLauncher.Core;

/// <summary>
/// The on-disk layout of an installed ECHO.
///
/// Installer layout (phase 1):
///   %LOCALAPPDATA%\Programs\ECHO\ECHO.exe
///   %LOCALAPPDATA%\Programs\ECHO\echo-core\app\main.py
///   %LOCALAPPDATA%\Programs\ECHO\runtime-core\pythonw.exe
///   %LOCALAPPDATA%\ECHO\data\echo.db          <- fixed data root, never removed by uninstall
///
/// The launcher therefore starts python with ECHO_BASE/ECHO_DATA pointing at the
/// persistent state root, while app/paths.py still derives echo_root() from __file__
/// (the program directory).  A legacy flat tree keeps its old behaviour: no ECHO_BASE
/// is injected and data lives next to the code.
/// </summary>
public sealed class LauncherLayout
{
    public required string ExePath { get; init; }
    public required string ProgramRoot { get; init; }
    public required string CodeRoot { get; init; }
    public required string StateRoot { get; init; }
    public required string DataRoot { get; init; }
    public required bool InstalledLayout { get; init; }
    public required bool UseStateEnvironment { get; init; }
    public required string? RuntimePythonw { get; init; }
    public required IReadOnlyList<string> RuntimeCandidates { get; init; }

    public string LogsDir => Path.Combine(DataRoot, "logs");
    public string LauncherLog => Path.Combine(LogsDir, "launcher.log");
    public string PortFile => Path.Combine(DataRoot, "echo-port.txt");

    public IReadOnlyList<string> SidebarCandidates => new[]
    {
        Path.Combine(CodeRoot, "sidebar", "bin", "Release", "net7.0-windows", "win-x64", "echo-sidebar.exe"),
        Path.Combine(CodeRoot, "sidebar", "bin", "Debug", "net7.0-windows", "win-x64", "echo-sidebar.exe"),
        Path.Combine(CodeRoot, "sidebar", "bin", "Release", "net8.0-windows", "win-x64", "echo-sidebar.exe"),
        Path.Combine(CodeRoot, "sidebar", "bin", "Debug", "net8.0-windows", "win-x64", "echo-sidebar.exe"),
    };

    public static LauncherLayout Discover(
        string exePath,
        string localAppData,
        string? echoBase = null,
        string? echoData = null,
        string? echoPython = null)
    {
        string fullExe = SafeFullPath(exePath);
        string programRoot = Path.GetDirectoryName(fullExe) ?? fullExe;

        string echoCore = Path.Combine(programRoot, "echo-core");
        string flatCode = programRoot;
        string codeRoot = File.Exists(Path.Combine(echoCore, "app", "main.py")) ? echoCore : flatCode;

        bool installed = IsInstalledLayout(programRoot, fullExe);

        string stateRoot;
        bool useStateEnvironment;
        if (!string.IsNullOrWhiteSpace(echoBase))
        {
            stateRoot = SafeFullPath(echoBase);
            useStateEnvironment = true;
        }
        else if (installed)
        {
            stateRoot = SafeFullPath(Path.Combine(localAppData, "ECHO"));
            useStateEnvironment = true;
        }
        else if (string.Equals(Path.GetFileName(codeRoot), "echo-core", StringComparison.OrdinalIgnoreCase))
        {
            stateRoot = SafeFullPath(Path.GetDirectoryName(codeRoot) ?? codeRoot);
            useStateEnvironment = false;
        }
        else
        {
            stateRoot = codeRoot;
            useStateEnvironment = false;
        }

        string dataRoot = string.IsNullOrWhiteSpace(echoData)
            ? Path.Combine(stateRoot, "data")
            : SafeFullPath(echoData);
        if (!string.IsNullOrWhiteSpace(echoData)) useStateEnvironment = true;

        var candidates = RuntimeCandidatesFor(programRoot, codeRoot, stateRoot, echoPython);
        string? runtime = candidates.FirstOrDefault(File.Exists);

        return new LauncherLayout
        {
            ExePath = fullExe,
            ProgramRoot = programRoot,
            CodeRoot = codeRoot,
            StateRoot = stateRoot,
            DataRoot = dataRoot,
            InstalledLayout = installed,
            UseStateEnvironment = useStateEnvironment,
            RuntimePythonw = runtime,
            RuntimeCandidates = candidates,
        };
    }

    /// <summary>Human-readable problems; empty = ready to launch.</summary>
    public IReadOnlyList<string> Validate()
    {
        var problems = new List<string>();
        if (!File.Exists(Path.Combine(CodeRoot, "app", "main.py")))
        {
            problems.Add($"找不到程序文件：{Path.Combine(CodeRoot, "app", "main.py")}");
        }
        if (RuntimePythonw is null)
        {
            problems.Add("找不到 Python 运行时（runtime-core\\pythonw.exe）");
        }
        return problems;
    }

    private static IReadOnlyList<string> RuntimeCandidatesFor(
        string programRoot, string codeRoot, string stateRoot, string? echoPython)
    {
        var list = new List<string>();
        void Add(string? p)
        {
            if (string.IsNullOrWhiteSpace(p)) return;
            string full = SafeFullPath(p);
            if (!list.Contains(full, PathComparer)) list.Add(full);
        }

        // 2026-11-03: an explicit ECHO_PYTHON is the developer's escape hatch; still put the
        // installer-owned runtime first so a stale user variable cannot hijack an install.
        Add(Path.Combine(programRoot, "runtime-core", "pythonw.exe"));
        Add(Path.Combine(programRoot, "runtime-core", "Scripts", "pythonw.exe"));
        Add(Path.Combine(programRoot, "runtime-core", "python.exe"));
        Add(Path.Combine(programRoot, "venv", "Scripts", "pythonw.exe"));
        Add(Path.Combine(codeRoot, "runtime-core", "pythonw.exe"));
        Add(Path.Combine(codeRoot, "runtime-core", "Scripts", "pythonw.exe"));
        Add(Path.Combine(codeRoot, "venv", "Scripts", "pythonw.exe"));
        Add(Path.Combine(stateRoot, "runtime-core", "pythonw.exe"));
        Add(Path.Combine(stateRoot, "runtime-core", "Scripts", "pythonw.exe"));
        if (!string.IsNullOrWhiteSpace(echoPython)) Add(echoPython);
        return list;
    }

    private static bool IsInstalledLayout(string programRoot, string exePath)
    {
        // The installer owns a program root that contains ECHO.exe, echo-core/ and
        // runtime-core/.  Never infer this from the folder name alone: a developer tree
        // named echo-core must keep its old (flat/new-layout) data placement.
        if (!string.Equals(Path.GetFileName(exePath), "ECHO.exe", StringComparison.OrdinalIgnoreCase))
            return false;
        if (!Directory.Exists(Path.Combine(programRoot, "echo-core")))
            return false;
        return File.Exists(Path.Combine(programRoot, "runtime-core", "python.exe"))
            || File.Exists(Path.Combine(programRoot, "runtime-core", "pythonw.exe"))
            || File.Exists(Path.Combine(programRoot, "runtime-core", "Scripts", "python.exe"));
    }

    internal static StringComparer PathComparer =>
        OperatingSystem.IsWindows() ? StringComparer.OrdinalIgnoreCase : StringComparer.Ordinal;

    internal static string SafeFullPath(string path)
    {
        try
        {
            string full = Path.GetFullPath(path);
            return Path.TrimEndingDirectorySeparator(full);
        }
        catch
        {
            return path;
        }
    }
}
