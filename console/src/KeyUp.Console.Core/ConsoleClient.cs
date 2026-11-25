// SPDX-License-Identifier: GPL-2.0-only
using System.Buffers.Binary;
using System.Net.WebSockets;
using System.Text;
using System.Text.Json;
using System.Text.Json.Serialization;

namespace KeyUp.Console.Core;

public sealed record Talkgroup(int Id, string Name, int Priority, bool Emergency);
public sealed record Zone(int Id, string Name, string? Color, int[] Talkgroups);
public sealed record Patch(int Id, string Name, int TgA, int TgB, bool Enabled);
public sealed record Rights(bool Inhibit, bool Priority, bool Patch);

public sealed class Profile
{
    public int ConsoleId { get; init; }
    public string ConsoleName { get; init; } = "";
    public int UnitId { get; init; }
    public string UnitAlias { get; init; } = "";
    public string SystemName { get; init; } = "KeyUp";
    public int MaxCallSeconds { get; init; } = 120;
    public Rights Rights { get; init; } = new(false, false, false);
    public List<Zone> Zones { get; init; } = new();
    public Dictionary<int, Talkgroup> Talkgroups { get; init; } = new();
    public List<Patch> Patches { get; init; } = new();
    public Dictionary<int, (int Src, string Alias)> Emergencies { get; init; } = new();
}

public sealed record CallInfo(long CallId, int Tg, int CallTg, int Src, string Alias, bool Emergency, string From);

/// <summary>
/// One connection to KeyUp's dispatch gateway (/console/ws), over the WireGuard tunnel or HTTPS.
/// Events are raised on the receive thread; UI code must marshal them to its own thread.
/// </summary>
public sealed class ConsoleClient : IAsyncDisposable
{
    public const byte AudioUp = 0x01, AudioDown = 0x02;
    public const int FrameBytes = 160;

    private readonly ClientWebSocket ws = new();
    private readonly SemaphoreSlim sendLock = new(1, 1);
    private CancellationTokenSource cts = new();
    private Task? receiver, pinger;

    public Profile? Profile { get; private set; }
    public string Transport { get; private set; } = "";
    public bool Connected => ws.State == WebSocketState.Open;

    public event Action<Profile>? ProfileChanged;
    public event Action<CallInfo>? CallStarted;
    public event Action<long, int, string>? CallEnded;               // call id, tg, reason
    public event Action<int, int, long, byte[]>? Audio;              // tg, src, call id, 160 bytes mu-law
    public event Action<int>? Granted;                               // tg
    public event Action<int, string>? Denied;                        // tg, reason
    public event Action<int, string>? TxEnded;                       // tg, reason
    public event Action<int, int, string, bool>? Emergency;          // tg, src, alias, on
    public event Action<int, bool, bool, string>? InhibitResult;     // unit, on, ok, reason
    public event Action<string>? AuthFailed;
    public event Action<string>? Disconnected;

    /// <summary>Connect and authenticate. Returns null on success, else the server's reason.</summary>
    public async Task<string?> ConnectAsync(string url, string token, string transport, string machine,
                                            TimeSpan timeout, CancellationToken ct = default)
    {
        Transport = transport;
        ws.Options.KeepAliveInterval = TimeSpan.FromSeconds(10);
        using var tcs = CancellationTokenSource.CreateLinkedTokenSource(ct);
        tcs.CancelAfter(timeout);
        await ws.ConnectAsync(new Uri(url), tcs.Token);
        await SendJsonAsync(new { t = "auth", token, app = "keyup-console/1.0", machine, transport });
        var first = await ReceiveTextAsync(tcs.Token);
        using var doc = JsonDocument.Parse(first ?? "{}");
        var root = doc.RootElement;
        var kind = Str(root, "t");
        if (kind != "auth_ok")
            return Str(root, "reason") ?? "refused";
        Profile = ParseProfile(root);
        receiver = Task.Run(ReceiveLoop);
        pinger = Task.Run(PingLoop);
        return null;
    }

    // ------------------------------------------------------------------ commands
    public Task MonitorAsync(IEnumerable<int> tgs) => SendJsonAsync(new { t = "monitor", tgs = tgs.Distinct().ToArray() });
    public Task PttOnAsync(int tg, bool emergency = false) => SendJsonAsync(new { t = "ptt", on = true, tg, emergency });
    public Task PttOffAsync() => SendJsonAsync(new { t = "ptt", on = false });
    public Task InhibitAsync(int unit, bool on) => SendJsonAsync(new { t = "inhibit", unit, on });
    public Task PatchAsync(int a, int b, bool on) => SendJsonAsync(new { t = "patch", tgs = new[] { a, b }, on });
    public Task ClearEmergencyAsync(int tg) => SendJsonAsync(new { t = "emergency_clear", tg });

    /// <summary>Send whole 20 ms mu-law frames (160 bytes each).</summary>
    public async Task SendAudioAsync(byte[] ulawFrames)
    {
        var buf = new byte[1 + ulawFrames.Length];
        buf[0] = AudioUp;
        ulawFrames.CopyTo(buf, 1);
        await SendAsync(buf, WebSocketMessageType.Binary);
    }

    // ------------------------------------------------------------------ plumbing
    private static readonly JsonSerializerOptions JsonOut = new() { DefaultIgnoreCondition = JsonIgnoreCondition.Never };

    private Task SendJsonAsync(object o) => SendAsync(JsonSerializer.SerializeToUtf8Bytes(o, JsonOut), WebSocketMessageType.Text);

    private async Task SendAsync(byte[] data, WebSocketMessageType type)
    {
        if (ws.State != WebSocketState.Open) return;
        await sendLock.WaitAsync();
        try { await ws.SendAsync(data, type, true, cts.Token); }
        catch (Exception) { /* the receive loop reports the disconnect */ }
        finally { sendLock.Release(); }
    }

    private async Task<string?> ReceiveTextAsync(CancellationToken ct)
    {
        var (type, data) = await ReceiveMessageAsync(ct);
        return type == WebSocketMessageType.Text ? Encoding.UTF8.GetString(data) : null;
    }

    private async Task<(WebSocketMessageType, byte[])> ReceiveMessageAsync(CancellationToken ct)
    {
        var buffer = new byte[8192];
        using var ms = new MemoryStream();
        while (true)
        {
            var r = await ws.ReceiveAsync(buffer, ct);
            if (r.MessageType == WebSocketMessageType.Close)
                return (WebSocketMessageType.Close, Array.Empty<byte>());
            ms.Write(buffer, 0, r.Count);
            if (r.EndOfMessage)
                return (r.MessageType, ms.ToArray());
        }
    }

    private async Task PingLoop()
    {
        while (!cts.IsCancellationRequested && ws.State == WebSocketState.Open)
        {
            await SendJsonAsync(new { t = "ping", ts = DateTimeOffset.UtcNow.ToUnixTimeMilliseconds() / 1000.0 });
            try { await Task.Delay(5000, cts.Token); } catch (OperationCanceledException) { return; }
        }
    }

    private async Task ReceiveLoop()
    {
        string reason = "closed";
        try
        {
            while (!cts.IsCancellationRequested)
            {
                var (type, data) = await ReceiveMessageAsync(cts.Token);
                if (type == WebSocketMessageType.Close) { reason = ws.CloseStatusDescription ?? "closed by server"; break; }
                if (type == WebSocketMessageType.Binary) { OnBinary(data); continue; }
                using var doc = JsonDocument.Parse(data);
                OnMessage(doc.RootElement);
            }
        }
        catch (OperationCanceledException) { reason = "closed"; }
        catch (Exception e) { reason = e.Message; }
        Disconnected?.Invoke(reason);
    }

    private void OnBinary(byte[] m)
    {
        if (m.Length < 13 || m[0] != AudioDown) return;
        int tg = (int)BinaryPrimitives.ReadUInt32BigEndian(m.AsSpan(1));
        int src = (int)BinaryPrimitives.ReadUInt32BigEndian(m.AsSpan(5));
        long call = BinaryPrimitives.ReadUInt32BigEndian(m.AsSpan(9));
        for (int i = 13; i + FrameBytes <= m.Length; i += FrameBytes)
            Audio?.Invoke(tg, src, call, m.AsSpan(i, FrameBytes).ToArray());
    }

    private void OnMessage(JsonElement m)
    {
        switch (Str(m, "t"))
        {
            case "profile":
                Profile = ParseProfile(m);
                ProfileChanged?.Invoke(Profile);
                break;
            case "call_start":
                CallStarted?.Invoke(new CallInfo(Long(m, "call_id"), Int(m, "tg"), Int(m, "call_tg"), Int(m, "src"),
                    Str(m, "alias") ?? "", Bool(m, "emergency"), Str(m, "from") ?? ""));
                break;
            case "call_end": CallEnded?.Invoke(Long(m, "call_id"), Int(m, "tg"), Str(m, "reason") ?? ""); break;
            case "grant": Granted?.Invoke(Int(m, "tg")); break;
            case "deny": Denied?.Invoke(Int(m, "tg"), Str(m, "reason") ?? "denied"); break;
            case "tx_end": TxEnded?.Invoke(Int(m, "tg"), Str(m, "reason") ?? ""); break;
            case "emergency": Emergency?.Invoke(Int(m, "tg"), Int(m, "src"), Str(m, "alias") ?? "", Bool(m, "on", true)); break;
            case "inhibit_result":
                InhibitResult?.Invoke(Int(m, "unit"), Bool(m, "on"), Bool(m, "ok"), Str(m, "reason") ?? ""); break;
            case "auth_fail": AuthFailed?.Invoke(Str(m, "reason") ?? "refused"); break;
        }
    }

    public static Profile ParseProfile(JsonElement m)
    {
        var con = m.GetProperty("console");
        var unit = m.GetProperty("unit");
        var sys = m.GetProperty("system");
        var rights = m.GetProperty("rights");
        var p = new Profile
        {
            ConsoleId = Int(con, "id"), ConsoleName = Str(con, "name") ?? "",
            UnitId = Int(unit, "id"), UnitAlias = Str(unit, "alias") ?? "",
            SystemName = Str(sys, "name") ?? "KeyUp", MaxCallSeconds = Int(sys, "max_call_s", 120),
            Rights = new Rights(Bool(rights, "inhibit"), Bool(rights, "priority"), Bool(rights, "patch")),
        };
        foreach (var t in m.GetProperty("talkgroups").EnumerateArray())
            p.Talkgroups[Int(t, "id")] = new Talkgroup(Int(t, "id"), Str(t, "name") ?? "", Int(t, "priority", 5), Bool(t, "emergency"));
        foreach (var z in m.GetProperty("zones").EnumerateArray())
            p.Zones.Add(new Zone(Int(z, "id"), Str(z, "name") ?? "", Str(z, "color"),
                z.GetProperty("talkgroups").EnumerateArray().Select(x => x.GetInt32()).ToArray()));
        if (m.TryGetProperty("patches", out var pa))
            foreach (var x in pa.EnumerateArray())
                p.Patches.Add(new Patch(Int(x, "id"), Str(x, "name") ?? "", Int(x, "tg_a"), Int(x, "tg_b"), Bool(x, "enabled", true)));
        if (m.TryGetProperty("emergencies", out var em) && em.ValueKind == JsonValueKind.Object)
            foreach (var e in em.EnumerateObject())
                if (int.TryParse(e.Name, out var tg))
                    p.Emergencies[tg] = (Int(e.Value, "src"), Str(e.Value, "alias") ?? "");
        return p;
    }

    internal static string? Str(JsonElement e, string n) =>
        e.ValueKind == JsonValueKind.Object && e.TryGetProperty(n, out var v) && v.ValueKind == JsonValueKind.String ? v.GetString() : null;
    internal static int Int(JsonElement e, string n, int d = 0) =>
        e.ValueKind == JsonValueKind.Object && e.TryGetProperty(n, out var v) && v.ValueKind == JsonValueKind.Number ? v.GetInt32() : d;
    internal static long Long(JsonElement e, string n) =>
        e.ValueKind == JsonValueKind.Object && e.TryGetProperty(n, out var v) && v.ValueKind == JsonValueKind.Number ? v.GetInt64() : 0;
    internal static bool Bool(JsonElement e, string n, bool d = false) =>
        e.ValueKind == JsonValueKind.Object && e.TryGetProperty(n, out var v)
            ? v.ValueKind == JsonValueKind.True || (v.ValueKind != JsonValueKind.False && d) : d;

    public async ValueTask DisposeAsync()
    {
        cts.Cancel();
        try
        {
            if (ws.State == WebSocketState.Open)
                await ws.CloseAsync(WebSocketCloseStatus.NormalClosure, "bye", CancellationToken.None);
        }
        catch (Exception) { }
        ws.Dispose();
    }
}
