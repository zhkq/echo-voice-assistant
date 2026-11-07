using EchoLauncher.Core;
using Xunit;

namespace EchoLauncher.Tests;

public sealed class LauncherOptionsTests
{
    [Fact]
    public void No_arguments_starts_with_the_default_wait()
    {
        LauncherOptions? options = LauncherOptions.Parse(Array.Empty<string>(), out string? error);
        Assert.Null(error);
        Assert.NotNull(options);
        Assert.Equal(LauncherMode.Start, options!.Mode);
        Assert.False(options.Quiet);
        Assert.False(options.Autostart);
        Assert.Equal(TimeSpan.FromSeconds(90), options.Wait);
    }

    [Fact]
    public void Stop_mode_is_recognised()
    {
        LauncherOptions? options = LauncherOptions.Parse(new[] { "--stop" }, out string? error);
        Assert.Null(error);
        Assert.Equal(LauncherMode.Stop, options!.Mode);
    }

    [Fact]
    public void Autostart_and_quiet_are_flags()
    {
        LauncherOptions? options = LauncherOptions.Parse(
            new[] { "--autostart", "--quiet", "--wait=30" }, out string? error);
        Assert.Null(error);
        Assert.True(options!.Autostart);
        Assert.True(options.Quiet);
        Assert.Equal(TimeSpan.FromSeconds(30), options.Wait);
    }

    [Theory]
    [InlineData("--wait=4")]
    [InlineData("--wait=601")]
    [InlineData("--wait=nope")]
    public void Invalid_wait_is_rejected(string arg)
    {
        Assert.Null(LauncherOptions.Parse(new[] { arg }, out string? error));
        Assert.NotNull(error);
    }

    [Fact]
    public void Unknown_argument_is_rejected()
    {
        Assert.Null(LauncherOptions.Parse(new[] { "--frobnicate" }, out string? error));
        Assert.Contains("不认识的参数", error);
    }

    [Fact]
    public void Help_mode_is_recognised()
    {
        LauncherOptions? options = LauncherOptions.Parse(new[] { "--help" }, out _);
        Assert.Equal(LauncherMode.Help, options!.Mode);
    }
}
