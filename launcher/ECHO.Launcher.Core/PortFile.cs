namespace EchoLauncher.Core;

/// <summary>
/// `data/echo-port.txt` is written by ECHO after it picks a free port (Windows may
/// reserve the preferred one).  The launcher must read it instead of assuming 8970.
/// </summary>
public static class PortFile
{
    public const int DefaultPort = 8970;
    public const string FileName = "echo-port.txt";

    public static int Read(string dataRoot, int fallback = DefaultPort)
    {
        try
        {
            string path = Path.Combine(dataRoot, FileName);
            if (!File.Exists(path)) return fallback;
            return Parse(File.ReadAllText(path)) ?? fallback;
        }
        catch
        {
            return fallback;
        }
    }

    public static int? Parse(string? text)
    {
        if (string.IsNullOrWhiteSpace(text)) return null;
        if (!int.TryParse(text.Trim(), out int port)) return null;
        return port is > 0 and <= 65535 ? port : null;
    }
}
