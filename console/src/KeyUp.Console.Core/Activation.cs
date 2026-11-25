// SPDX-License-Identifier: GPL-2.0-only
using System.Net;
using System.Net.Http.Json;
using System.Text.Json;
using System.Text.Json.Serialization;

namespace KeyUp.Console.Core;

public sealed class TunnelSpec
{
    [JsonPropertyName("private_key")] public string PrivateKey { get; set; } = "";
    [JsonPropertyName("address")] public string Address { get; set; } = "";             // 10.77.0.9/32
    [JsonPropertyName("server_public_key")] public string ServerPublicKey { get; set; } = "";
    [JsonPropertyName("endpoint")] public string Endpoint { get; set; } = "";           // host:port
    [JsonPropertyName("allowed_ips")] public string AllowedIps { get; set; } = "";      // 10.77.0.1/32
    [JsonPropertyName("persistent_keepalive")] public int PersistentKeepalive { get; set; } = 15;

    public bool IsComplete => PrivateKey.Length > 0 && Address.Length > 0 && ServerPublicKey.Length > 0 && Endpoint.Length > 0;
    public string ServerIp => AllowedIps.Split('/')[0];
}

public sealed class ActivationResult
{
    [JsonPropertyName("token")] public string Token { get; set; } = "";
    [JsonPropertyName("console_id")] public int ConsoleId { get; set; }
    [JsonPropertyName("name")] public string Name { get; set; } = "";
    [JsonPropertyName("server")] public string Server { get; set; } = "";
    [JsonPropertyName("urls")] public Dictionary<string, string> Urls { get; set; } = new();
    [JsonPropertyName("wireguard")] public TunnelSpec WireGuard { get; set; } = new();
}

public static class Activation
{
    /// <summary>"lmr.example.com" or "https://lmr.example.com/" -> "https://lmr.example.com".</summary>
    public static string NormalizeServer(string server)
    {
        var s = (server ?? "").Trim().TrimEnd('/');
        if (s.Length == 0) throw new ArgumentException("Enter the KeyUp server address.");
        if (!s.StartsWith("http://", StringComparison.OrdinalIgnoreCase) && !s.StartsWith("https://", StringComparison.OrdinalIgnoreCase))
            s = "https://" + s;
        return s;
    }

    public static async Task<ActivationResult> ActivateAsync(string server, string code, string machine)
    {
        using var http = new HttpClient { Timeout = TimeSpan.FromSeconds(20) };
        var resp = await http.PostAsJsonAsync(NormalizeServer(server) + "/api/console/activate",
            new { code = code.Trim(), name = machine, app = "keyup-console/1.0" });
        var body = await resp.Content.ReadAsStringAsync();
        if (resp.StatusCode == HttpStatusCode.Forbidden)
            throw new InvalidOperationException("The activation code is wrong or has expired. Issue a new one in the KeyUp web UI (Dispatch Consoles > Activate).");
        if (!resp.IsSuccessStatusCode)
        {
            string detail = resp.ReasonPhrase ?? "";
            try { using var d = JsonDocument.Parse(body); detail = d.RootElement.GetProperty("detail").GetString() ?? detail; } catch { }
            throw new InvalidOperationException($"The KeyUp server answered {(int)resp.StatusCode}: {detail}");
        }
        return JsonSerializer.Deserialize<ActivationResult>(body) ?? throw new InvalidOperationException("Empty answer from the server.");
    }
}
