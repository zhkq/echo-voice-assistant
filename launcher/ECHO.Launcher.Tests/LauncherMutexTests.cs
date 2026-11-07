using EchoLauncher.Core;
using Xunit;

namespace EchoLauncher.Tests;

public sealed class LauncherMutexTests
{
    [Fact]
    public void Name_is_stable_and_scoped_to_the_state_root()
    {
        string a1 = LauncherMutex.Name(@"C:\Users\alice\AppData\Local\ECHO");
        string a2 = LauncherMutex.Name(@"C:\Users\alice\AppData\Local\ECHO\");
        string b = LauncherMutex.Name(@"C:\Users\alice\AppData\Local\ECHO-dev");

        Assert.StartsWith("Local\\ECHO.Launcher.", a1);
        Assert.Equal(a1, a2);
        Assert.NotEqual(a1, b);
    }
}
