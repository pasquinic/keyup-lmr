// SPDX-License-Identifier: GPL-2.0-only
using System.Runtime.InteropServices;
using System.Text;
using System.Text.Json;
using System.Text.Json.Serialization;

namespace KeyUp.Console.Core;

public enum TransportPreference { Auto, Tunnel, Https }

/// <summary>Per-user console settings in %APPDATA%\KeyUp Dispatch Console\console.json.
/// The login token and the tunnel's private key are encrypted with Windows DPAPI (current user).</summary>
public sealed class ConsoleSettings
{
    public string Server { get; set; } = "";
    public int ConsoleId { get; set; }
    public string ConsoleName { get; set; } = "";
    public string TokenProtected { get; set; } = "";
    public string TunnelUrl { get; set; } = "";
    public string PublicUrl { get; set; } = "";
    public TunnelSpec Tunnel { get; set; } = new();      // PrivateKey kept protected, see TunnelPrivateKey
    public TransportPreference Transport { get; set; } = TransportPreference.Auto;
    public string OutputDevice { get; set; } = "";
    public string InputDevice { get; set; } = "";
    public string PttKey { get; set; } = "Space";
    public List<int> Monitored { get; set; } = new();
    public Dictionary<int, double> Volumes { get; set; } = new();
    public int SelectedTalkgroup { get; set; }
    public int SelectedZone { get; set; }

    public static string Folder => Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.ApplicationData), "KeyUp Dispatch Console");
    public static string FilePath => Path.Combine(Folder, "console.json");

    [JsonIgnore] public bool IsActivated => ConsoleId > 0 && TokenProtected.Length > 0;
    // decrypted views -- never written to the file
    [JsonIgnore] public string Token { get => Secret.Unprotect(TokenProtected); set => TokenProtected = Secret.Protect(value); }
    [JsonIgnore] public string TunnelPrivateKey { get => Secret.Unprotect(Tunnel.PrivateKey); set => Tunnel.PrivateKey = Secret.Protect(value); }

    public TunnelSpec PlainTunnel() => new()
    {
        PrivateKey = TunnelPrivateKey, Address = Tunnel.Address, ServerPublicKey = Tunnel.ServerPublicKey,
        Endpoint = Tunnel.Endpoint, AllowedIps = Tunnel.AllowedIps, PersistentKeepalive = Tunnel.PersistentKeepalive,
    };

    public void Apply(ActivationResult a, string server)
    {
        Server = server;
        ConsoleId = a.ConsoleId;
        ConsoleName = a.Name;
        Token = a.Token;
        TunnelUrl = a.Urls.GetValueOrDefault("tunnel", "");
        PublicUrl = a.Urls.GetValueOrDefault("public", "");
        Tunnel = new TunnelSpec
        {
            Address = a.WireGuard.Address, ServerPublicKey = a.WireGuard.ServerPublicKey, Endpoint = a.WireGuard.Endpoint,
            AllowedIps = a.WireGuard.AllowedIps, PersistentKeepalive = a.WireGuard.PersistentKeepalive,
        };
        TunnelPrivateKey = a.WireGuard.PrivateKey;
    }

    public static ConsoleSettings Load(string? path = null)
    {
        try
        {
            var p = path ?? FilePath;
            if (File.Exists(p)) return JsonSerializer.Deserialize<ConsoleSettings>(File.ReadAllText(p)) ?? new();
        }
        catch (Exception) { }
        return new ConsoleSettings();
    }

    public void Save(string? path = null)
    {
        var p = path ?? FilePath;
        Directory.CreateDirectory(Path.GetDirectoryName(p)!);
        File.WriteAllText(p, JsonSerializer.Serialize(this, new JsonSerializerOptions { WriteIndented = true }));
    }
}

/// <summary>DPAPI (CryptProtectData, current user) on Windows; marked plain text elsewhere (tests).</summary>
public static class Secret
{
    public static string Protect(string plain)
    {
        if (string.IsNullOrEmpty(plain)) return "";
        var data = Encoding.UTF8.GetBytes(plain);
        if (!OperatingSystem.IsWindows()) return "plain:" + Convert.ToBase64String(data);
        return "dpapi:" + Convert.ToBase64String(Dpapi(data, protect: true));
    }

    /// <summary>Machine-scope DPAPI: readable by the KeyUp Tunnel service (LocalSystem) on this PC only.</summary>
    public static string ProtectForMachine(string plain)
    {
        if (string.IsNullOrEmpty(plain)) return "";
        var data = Encoding.UTF8.GetBytes(plain);
        if (!OperatingSystem.IsWindows()) return "plain:" + Convert.ToBase64String(data);
        return "dpapim:" + Convert.ToBase64String(Dpapi(data, protect: true, machine: true));
    }

    public static string Unprotect(string stored)
    {
        if (string.IsNullOrEmpty(stored)) return "";
        if (stored.StartsWith("plain:")) return Encoding.UTF8.GetString(Convert.FromBase64String(stored[6..]));
        if (stored.StartsWith("dpapi:") && OperatingSystem.IsWindows())
        {
            try { return Encoding.UTF8.GetString(Dpapi(Convert.FromBase64String(stored[6..]), protect: false)); }
            catch (Exception) { return ""; }
        }
        if (stored.StartsWith("dpapim:") && OperatingSystem.IsWindows())
        {
            try { return Encoding.UTF8.GetString(Dpapi(Convert.FromBase64String(stored[7..]), protect: false, machine: true)); }
            catch (Exception) { return ""; }
        }
        return "";
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct DataBlob { public int cbData; public IntPtr pbData; }

    [DllImport("crypt32.dll", SetLastError = true, CharSet = CharSet.Unicode)]
    private static extern bool CryptProtectData(ref DataBlob dataIn, string? description, IntPtr entropy, IntPtr reserved,
                                                IntPtr prompt, int flags, out DataBlob dataOut);

    [DllImport("crypt32.dll", SetLastError = true, CharSet = CharSet.Unicode)]
    private static extern bool CryptUnprotectData(ref DataBlob dataIn, IntPtr description, IntPtr entropy, IntPtr reserved,
                                                  IntPtr prompt, int flags, out DataBlob dataOut);

    [DllImport("kernel32.dll")]
    private static extern IntPtr LocalFree(IntPtr mem);

    private static byte[] Dpapi(byte[] input, bool protect, bool machine = false)
    {
        var handle = GCHandle.Alloc(input, GCHandleType.Pinned);
        try
        {
            var inBlob = new DataBlob { cbData = input.Length, pbData = handle.AddrOfPinnedObject() };
            const int CRYPTPROTECT_UI_FORBIDDEN = 0x1, CRYPTPROTECT_LOCAL_MACHINE = 0x4;
            int flags = CRYPTPROTECT_UI_FORBIDDEN | (machine ? CRYPTPROTECT_LOCAL_MACHINE : 0);
            bool ok = protect
                ? CryptProtectData(ref inBlob, "KeyUp Dispatch Console", IntPtr.Zero, IntPtr.Zero, IntPtr.Zero, flags, out var outBlob)
                : CryptUnprotectData(ref inBlob, IntPtr.Zero, IntPtr.Zero, IntPtr.Zero, IntPtr.Zero, flags, out outBlob);
            if (!ok) throw new InvalidOperationException($"DPAPI failed ({Marshal.GetLastWin32Error()})");
            var result = new byte[outBlob.cbData];
            Marshal.Copy(outBlob.pbData, result, 0, outBlob.cbData);
            LocalFree(outBlob.pbData);
            return result;
        }
        finally { handle.Free(); }
    }
}
