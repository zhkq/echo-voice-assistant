using System.Net;
using System.Net.Http;
using System.Text;
using System.Text.Json;

namespace EchoLauncher;

internal sealed record StopResult(int Code, string Message);

/// <summary>
/// Minimal localhost HTTP client.  Loopback only, no proxy: corporate machines often set
/// http_proxy and would otherwise send these calls to the intranet proxy (ECHO's own
/// netlocal.install_loopback_bypass() fixes the Python side; the launcher needs the same).
/// </summary>
internal sealed class ApiClient : IDisposable
{
    private readonly HttpClient _http;

    public ApiClient()
    {
        var handler = new SocketsHttpHandler
        {
            UseProxy = false,
            AllowAutoRedirect = false,
            ConnectTimeout = TimeSpan.FromSeconds(2),
            PooledConnectionLifetime = TimeSpan.FromMinutes(1),
        };
        _http = new HttpClient(handler) { Timeout = Timeout.InfiniteTimeSpan };
        _http.DefaultRequestHeaders.UserAgent.ParseAdd("ECHO-Launcher/1.0");
    }

    public async Task<bool> IsUpAsync(int port, TimeSpan timeout)
    {
        try
        {
            using var cts = new CancellationTokenSource(timeout);
            using var response = await _http.GetAsync(
                $"http://127.0.0.1:{port}/api/status",
                HttpCompletionOption.ResponseHeadersRead, cts.Token);
            // Any HTTP response means something is listening and speaking HTTP.  A 401 is
            // still "ECHO is up" (it only means apiAuthEnabled is on and we are not web).
            return true;
        }
        catch
        {
            return false;
        }
    }

    /// <summary>
    /// POST /api/control/echo/stop.  Exit codes used by the installer:
    ///   0 = accepted (caller still waits for the port to close)
    ///   2 = ECHO refused (recording / command in progress)
    ///   3 = HTTP error / timeout
    /// </summary>
    public async Task<StopResult> StopAsync(int port, TimeSpan timeout)
    {
        try
        {
            using var cts = new CancellationTokenSource(timeout);
            using var content = new StringContent("{}", Encoding.UTF8, "application/json");
            using var response = await _http.PostAsync(
                $"http://127.0.0.1:{port}/api/control/echo/stop", content, cts.Token);
            string body = await response.Content.ReadAsStringAsync(cts.Token);
            if (!response.IsSuccessStatusCode)
            {
                return new StopResult(3, $"HTTP {(int)response.StatusCode}: {Short(body)}");
            }

            using var document = JsonDocument.Parse(body);
            JsonElement root = document.RootElement;
            bool ok = root.TryGetProperty("ok", out JsonElement okElement)
                      && okElement.ValueKind == JsonValueKind.True;
            string message = root.TryGetProperty("message", out JsonElement messageElement)
                             && messageElement.ValueKind == JsonValueKind.String
                ? messageElement.GetString() ?? ""
                : "";
            return ok ? new StopResult(0, message) : new StopResult(2, Short(message));
        }
        catch (OperationCanceledException)
        {
            return new StopResult(3, "停止请求超时");
        }
        catch (HttpRequestException ex)
        {
            return new StopResult(3, "停止请求失败：" + ex.Message);
        }
        catch (JsonException ex)
        {
            return new StopResult(3, "停止响应无法解析：" + ex.Message);
        }
    }

    public void Dispose() => _http.Dispose();

    private static string Short(string? text)
    {
        string value = (text ?? "").Trim();
        if (value.Length == 0) return "";
        return value.Length <= 300 ? value : value[..300] + "…";
    }
}
