using EchoLauncher.Core;
using Xunit;

namespace EchoLauncher.Tests;

public sealed class LauncherLayoutTests : IDisposable
{
    private readonly string _root = Path.Combine(Path.GetTempPath(), "echo-launcher-test-" + Guid.NewGuid().ToString("N"));

    public LauncherLayoutTests() => Directory.CreateDirectory(_root);

    public void Dispose()
    {
        try { Directory.Delete(_root, recursive: true); } catch { }
    }

    private string Touch(params string[] parts)
    {
        string path = Path.Combine(new[] { _root }.Concat(parts).ToArray());
        Directory.CreateDirectory(Path.GetDirectoryName(path)!);
        File.WriteAllText(path, "x");
        return path;
    }

    private (string ProgramRoot, string LocalAppData) InstalledTree()
    {
        string local = Path.Combine(_root, "local");
        string program = Path.Combine(local, "Programs", "ECHO");
        Touch("local", "Programs", "ECHO", "ECHO.exe");
        Touch("local", "Programs", "ECHO", "echo-core", "app", "main.py");
        Touch("local", "Programs", "ECHO", "runtime-core", "pythonw.exe");
        return (program, local);
    }

    [Fact]
    public void Installed_layout_puts_state_under_localappdata()
    {
        var (program, local) = InstalledTree();
        LauncherLayout layout = LauncherLayout.Discover(
            Path.Combine(program, "ECHO.exe"), local);

        Assert.True(layout.InstalledLayout);
        Assert.True(layout.UseStateEnvironment);
        Assert.Equal(Path.Combine(program, "echo-core"), layout.CodeRoot);
        Assert.Equal(Path.Combine(local, "ECHO"), layout.StateRoot);
        Assert.Equal(Path.Combine(local, "ECHO", "data"), layout.DataRoot);
        Assert.Equal(Path.Combine(program, "runtime-core", "pythonw.exe"), layout.RuntimePythonw);
        Assert.Empty(layout.Validate());
    }

    [Fact]
    public void Explicit_echo_base_wins_over_installed_default()
    {
        var (program, local) = InstalledTree();
        string state = Path.Combine(_root, "custom-state");
        LauncherLayout layout = LauncherLayout.Discover(
            Path.Combine(program, "ECHO.exe"), local, echoBase: state);

        Assert.Equal(state, layout.StateRoot);
        Assert.Equal(Path.Combine(state, "data"), layout.DataRoot);
    }

    [Fact]
    public void Explicit_echo_data_wins_over_state_data()
    {
        var (program, local) = InstalledTree();
        string data = Path.Combine(_root, "custom-data");
        LauncherLayout layout = LauncherLayout.Discover(
            Path.Combine(program, "ECHO.exe"), local, echoData: data);

        Assert.Equal(data, layout.DataRoot);
        Assert.True(layout.UseStateEnvironment);
    }

    [Fact]
    public void Flat_tree_keeps_its_old_data_placement()
    {
        Touch("flat", "ECHO.exe");
        Touch("flat", "app", "main.py");
        Touch("flat", "runtime-core", "pythonw.exe");

        LauncherLayout layout = LauncherLayout.Discover(
            Path.Combine(_root, "flat", "ECHO.exe"), Path.Combine(_root, "local"));

        Assert.False(layout.InstalledLayout);
        Assert.False(layout.UseStateEnvironment);
        Assert.Equal(Path.Combine(_root, "flat"), layout.CodeRoot);
        Assert.Equal(Path.Combine(_root, "flat"), layout.StateRoot);
        Assert.Equal(Path.Combine(_root, "flat", "data"), layout.DataRoot);
    }

    [Fact]
    public void Missing_runtime_is_reported_not_guessed()
    {
        string program = Path.Combine(_root, "broken");
        Touch("broken", "ECHO.exe");
        Touch("broken", "echo-core", "app", "main.py");

        LauncherLayout layout = LauncherLayout.Discover(
            Path.Combine(program, "ECHO.exe"), Path.Combine(_root, "local"));

        Assert.Null(layout.RuntimePythonw);
        Assert.NotEmpty(layout.Validate());
    }

    [Fact]
    public void Sidebar_candidates_cover_release_and_debug()
    {
        var (program, local) = InstalledTree();
        LauncherLayout layout = LauncherLayout.Discover(Path.Combine(program, "ECHO.exe"), local);
        Assert.Contains(layout.SidebarCandidates,
            p => p.EndsWith(Path.Combine("Release", "net7.0-windows", "win-x64", "echo-sidebar.exe")));
        Assert.Contains(layout.SidebarCandidates,
            p => p.EndsWith(Path.Combine("Debug", "net7.0-windows", "win-x64", "echo-sidebar.exe")));
    }
}
