using System.Security.Cryptography;
using System.Text;

namespace EchoLauncher.Core;

/// <summary>
/// Single-instance mutex name.  Scoped to the data root so a developer tree and an
/// installed ECHO can run side by side without one stealing the other's mutex.
/// `Local\` (not `Global\`) keeps it per Windows session/user.
/// </summary>
public static class LauncherMutex
{
    public static string Name(string stateRoot)
    {
        string normalized = LauncherLayout.SafeFullPath(stateRoot)
            .TrimEnd('\\', '/')
            .ToUpperInvariant();
        byte[] digest = SHA256.HashData(Encoding.UTF8.GetBytes(normalized));
        return "Local\\ECHO.Launcher." + Convert.ToHexString(digest)[..16];
    }
}
