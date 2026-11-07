using System.Text;

namespace EchoLauncher.Core;

/// <summary>
/// Small append-only log at %LOCALAPPDATA%\ECHO\logs\launcher.log.
/// ECHO itself does not capture the launcher's view (runtime errors, exit codes), so
/// this file is the first place to look when the panel never comes up.
/// </summary>
public sealed class LauncherLog
{
    private const long RotateBytes = 2 * 1024 * 1024;
    private readonly object _gate = new();
    private readonly string _path;
    private int _writes;

    public LauncherLog(string path)
    {
        _path = path;
        try { Directory.CreateDirectory(System.IO.Path.GetDirectoryName(path)!); } catch { }
    }

    public string Path => _path;

    public void Write(string message)
    {
        lock (_gate)
        {
            try
            {
                if (++_writes % 50 == 0 || _writes == 1) RotateIfNeeded();
                string line = $"[{DateTime.Now:yyyy-MM-dd HH:mm:ss}] {message}{Environment.NewLine}";
                File.AppendAllText(_path, line, new UTF8Encoding(encoderShouldEmitUTF8Identifier: false));
            }
            catch
            {
                // Logging must never take the launcher down.
            }
        }
    }

    private void RotateIfNeeded()
    {
        try
        {
            var info = new FileInfo(_path);
            if (!info.Exists || info.Length < RotateBytes) return;
            string old = _path + ".1";
            File.Delete(old);
            File.Move(_path, old);
        }
        catch
        {
        }
    }

    /// <summary>Last N lines, for the error dialog (never throws).</summary>
    public static string Tail(string path, int lines = 20)
    {
        try
        {
            using var stream = new FileStream(path, FileMode.Open, FileAccess.Read,
                FileShare.ReadWrite | FileShare.Delete);
            using var reader = new StreamReader(stream, Encoding.UTF8);
            var queue = new Queue<string>();
            string? line;
            while ((line = reader.ReadLine()) != null)
            {
                queue.Enqueue(line);
                if (queue.Count > lines) queue.Dequeue();
            }
            return string.Join(Environment.NewLine, queue);
        }
        catch
        {
            return "(读不到日志)";
        }
    }
}
