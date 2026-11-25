// SPDX-License-Identifier: GPL-2.0-only
using System.Buffers.Binary;
using System.Diagnostics;
using System.Net;
using System.Net.Sockets;
using System.Runtime.InteropServices;

namespace KeyUp.Console.Core;

/// <summary>
/// The console's own WireGuard tunnel to KeyUp, using WireGuardNT (the official wireguard.dll from
/// download.wireguard.com, shipped next to the program). Only the KeyUp server's tunnel address is
/// routed through it (split tunnel). Creating the adapter needs administrator rights, so the installed
/// "KeyUp Tunnel" service normally does this; the console can also do it itself when run as admin.
/// </summary>
public static class WireGuardNt
{
    private const string Dll = "wireguard.dll";

    [DllImport(Dll, EntryPoint = "WireGuardCreateAdapter", CharSet = CharSet.Unicode, SetLastError = true)]
    public static extern IntPtr CreateAdapter(string name, string tunnelType, ref Guid requestedGuid);

    [DllImport(Dll, EntryPoint = "WireGuardOpenAdapter", CharSet = CharSet.Unicode, SetLastError = true)]
    public static extern IntPtr OpenAdapter(string name);

    [DllImport(Dll, EntryPoint = "WireGuardCloseAdapter")]
    public static extern void CloseAdapter(IntPtr adapter);

    [DllImport(Dll, EntryPoint = "WireGuardSetConfiguration", SetLastError = true)]
    public static extern bool SetConfiguration(IntPtr adapter, byte[] config, uint bytes);

    [DllImport(Dll, EntryPoint = "WireGuardSetAdapterState", SetLastError = true)]
    public static extern bool SetAdapterState(IntPtr adapter, int state);

    [DllImport(Dll, EntryPoint = "WireGuardGetConfiguration", SetLastError = true)]
    public static extern bool GetConfiguration(IntPtr adapter, byte[] config, ref uint bytes);

    public const int StateDown = 0, StateUp = 1;

    // ---------------------------------------------------------------- configuration blob (wireguard.h)
    // WIREGUARD_INTERFACE  (align 8, 80 bytes):  Flags@0 ListenPort@4 PrivateKey@6 PublicKey@38 PeersCount@72
    // WIREGUARD_PEER       (align 8, 136 bytes): Flags@0 Reserved@4 PublicKey@8 PresharedKey@40
    //                       PersistentKeepalive@72 Endpoint(SOCKADDR_INET, 28)@76 TxBytes@104 RxBytes@112
    //                       LastHandshake@120 AllowedIPsCount@128
    // WIREGUARD_ALLOWED_IP (align 8, 24 bytes):  Address@0 (16) AddressFamily@16 Cidr@18 Flags@20
    public const int InterfaceSize = 80, PeerSize = 136, AllowedIpSize = 24;
    private const uint IfHasPrivateKey = 1 << 1, IfReplacePeers = 1 << 3;
    private const uint PeerHasPublicKey = 1 << 0, PeerHasKeepalive = 1 << 2, PeerHasEndpoint = 1 << 3, PeerReplaceAllowed = 1 << 5;
    private const ushort AF_INET = 2;

    public static byte[] BuildConfig(byte[] privateKey, byte[] peerPublicKey, IPEndPoint endpoint, ushort keepalive,
                                     IEnumerable<(IPAddress Address, byte Cidr)> allowed)
    {
        if (privateKey.Length != 32 || peerPublicKey.Length != 32) throw new ArgumentException("WireGuard keys are 32 bytes");
        if (endpoint.AddressFamily != AddressFamily.InterNetwork) throw new ArgumentException("IPv4 endpoint required");
        var ips = allowed.ToList();
        var buf = new byte[InterfaceSize + PeerSize + AllowedIpSize * ips.Count];
        var s = buf.AsSpan();
        BinaryPrimitives.WriteUInt32LittleEndian(s[0..], IfHasPrivateKey | IfReplacePeers);
        privateKey.CopyTo(s[6..]);
        BinaryPrimitives.WriteUInt32LittleEndian(s[72..], 1);
        var p = s[InterfaceSize..];
        BinaryPrimitives.WriteUInt32LittleEndian(p[0..], PeerHasPublicKey | PeerHasEndpoint | PeerHasKeepalive | PeerReplaceAllowed);
        peerPublicKey.CopyTo(p[8..]);
        BinaryPrimitives.WriteUInt16LittleEndian(p[72..], keepalive);
        // SOCKADDR_IN: family (host order), port (network order), address
        BinaryPrimitives.WriteUInt16LittleEndian(p[76..], AF_INET);
        BinaryPrimitives.WriteUInt16BigEndian(p[78..], (ushort)endpoint.Port);
        endpoint.Address.GetAddressBytes().CopyTo(p[80..]);
        BinaryPrimitives.WriteUInt32LittleEndian(p[128..], (uint)ips.Count);
        for (int i = 0; i < ips.Count; i++)
        {
            var a = s[(InterfaceSize + PeerSize + AllowedIpSize * i)..];
            ips[i].Address.GetAddressBytes().CopyTo(a[0..]);
            BinaryPrimitives.WriteUInt16LittleEndian(a[16..], AF_INET);
            a[18] = ips[i].Cidr;
        }
        return buf;
    }

    /// <summary>Last handshake (UTC) of the first peer, or null if none yet.</summary>
    public static DateTime? LastHandshake(IntPtr adapter)
    {
        uint size = 4096;
        var buf = new byte[size];
        if (!GetConfiguration(adapter, buf, ref size)) return null;
        if (BinaryPrimitives.ReadUInt32LittleEndian(buf.AsSpan(72)) == 0) return null;
        long ticks = (long)BinaryPrimitives.ReadUInt64LittleEndian(buf.AsSpan(InterfaceSize + 120));
        return ticks == 0 ? null : DateTime.FromFileTimeUtc(ticks);
    }
}

public sealed class WireGuardTunnel : IDisposable
{
    public const string AdapterName = "KeyUp";
    private static readonly Guid AdapterGuid = new("6b2b3d5e-0c4f-4b7a-9a52-4b65795570aa");
    private IntPtr adapter;
    public TunnelSpec? Spec { get; private set; }
    public bool IsUp => adapter != IntPtr.Zero;

    public static (IPAddress Address, byte Cidr) ParseCidr(string s)
    {
        var parts = s.Trim().Split('/');
        return (IPAddress.Parse(parts[0]), parts.Length > 1 ? byte.Parse(parts[1]) : (byte)32);
    }

    public static IPEndPoint ResolveEndpoint(string hostPort)
    {
        int i = hostPort.LastIndexOf(':');
        if (i < 0) throw new ArgumentException($"endpoint '{hostPort}' needs host:port");
        string host = hostPort[..i].Trim('[', ']');
        int port = int.Parse(hostPort[(i + 1)..]);
        var addr = IPAddress.TryParse(host, out var ip) ? ip
            : Dns.GetHostAddresses(host).FirstOrDefault(a => a.AddressFamily == AddressFamily.InterNetwork)
              ?? throw new InvalidOperationException($"cannot resolve {host}");
        return new IPEndPoint(addr, port);
    }

    public static byte[] BuildConfig(TunnelSpec spec) =>
        WireGuardNt.BuildConfig(Convert.FromBase64String(spec.PrivateKey), Convert.FromBase64String(spec.ServerPublicKey),
            ResolveEndpoint(spec.Endpoint), (ushort)spec.PersistentKeepalive,
            spec.AllowedIps.Split(',', StringSplitOptions.RemoveEmptyEntries).Select(ParseCidr));

    /// <summary>Create (or reuse) the adapter, configure it, give it its address and route, bring it up.</summary>
    public void Up(TunnelSpec spec)
    {
        if (!OperatingSystem.IsWindows()) throw new PlatformNotSupportedException("the WireGuard tunnel needs Windows");
        var config = BuildConfig(spec);
        var guid = AdapterGuid;
        if (adapter == IntPtr.Zero)
        {
            adapter = WireGuardNt.OpenAdapter(AdapterName);
            if (adapter == IntPtr.Zero) adapter = WireGuardNt.CreateAdapter(AdapterName, "KeyUp", ref guid);
            if (adapter == IntPtr.Zero)
                throw new InvalidOperationException($"could not create the WireGuard adapter (error {Marshal.GetLastWin32Error()}; administrator rights needed)");
        }
        if (!WireGuardNt.SetConfiguration(adapter, config, (uint)config.Length))
            throw new InvalidOperationException($"WireGuard configuration refused (error {Marshal.GetLastWin32Error()})");
        if (!WireGuardNt.SetAdapterState(adapter, WireGuardNt.StateUp))
            throw new InvalidOperationException($"could not bring the WireGuard adapter up (error {Marshal.GetLastWin32Error()})");
        var (addr, _) = ParseCidr(spec.Address);
        Netsh($"interface ipv4 set address name=\"{AdapterName}\" static {addr} 255.255.255.255");
        foreach (var allowed in spec.AllowedIps.Split(',', StringSplitOptions.RemoveEmptyEntries))
        {
            var (net, cidr) = ParseCidr(allowed);
            Netsh($"interface ipv4 add route {net}/{cidr} \"{AdapterName}\" store=active", ignoreErrors: true);
        }
        Spec = spec;
    }

    public DateTime? LastHandshake() => adapter == IntPtr.Zero ? null : WireGuardNt.LastHandshake(adapter);

    public void Down()
    {
        if (adapter == IntPtr.Zero) return;
        WireGuardNt.SetAdapterState(adapter, WireGuardNt.StateDown);
        WireGuardNt.CloseAdapter(adapter);   // closing the last handle removes the adapter
        adapter = IntPtr.Zero;
        Spec = null;
    }

    private static void Netsh(string args, bool ignoreErrors = false)
    {
        var psi = new ProcessStartInfo("netsh", args) { CreateNoWindow = true, UseShellExecute = false,
                                                         RedirectStandardOutput = true, RedirectStandardError = true };
        using var p = Process.Start(psi)!;
        string output = p.StandardOutput.ReadToEnd() + p.StandardError.ReadToEnd();
        p.WaitForExit(10000);
        if (p.ExitCode != 0 && !ignoreErrors)
            throw new InvalidOperationException($"netsh {args}: {output.Trim()}");
    }

    public void Dispose() => Down();
}

/// <summary>
/// Hand-off between the console (per user) and the KeyUp Tunnel service (LocalSystem):
/// %ProgramData%\KeyUp Dispatch Console\tunnel.json holds the wanted tunnel, tunnel-status.json the
/// service's answer. The installer makes the folder writable by the console's users.
/// </summary>
public static class TunnelHandoff
{
    public static string Folder => Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.CommonApplicationData), "KeyUp Dispatch Console");
    public static string RequestPath => Path.Combine(Folder, "tunnel.json");
    public static string StatusPath => Path.Combine(Folder, "tunnel-status.json");

    public sealed class Request
    {
        public bool Enabled { get; set; }
        public TunnelSpec Spec { get; set; } = new();
        public long Serial { get; set; }
    }

    public sealed class Status
    {
        public long Serial { get; set; }
        public string State { get; set; } = "down";      // up | down | error
        public string Error { get; set; } = "";
        public DateTime? LastHandshakeUtc { get; set; }
        public DateTime UpdatedUtc { get; set; }
    }

    public static void WriteRequest(Request r)
    {
        Directory.CreateDirectory(Folder);
        var tmp = RequestPath + ".tmp";
        File.WriteAllText(tmp, System.Text.Json.JsonSerializer.Serialize(r));
        File.Move(tmp, RequestPath, true);
    }

    public static Request? ReadRequest() => ReadJson<Request>(RequestPath);
    public static Status? ReadStatus() => ReadJson<Status>(StatusPath);

    public static void WriteStatus(Status s)
    {
        Directory.CreateDirectory(Folder);
        var tmp = StatusPath + ".tmp";
        File.WriteAllText(tmp, System.Text.Json.JsonSerializer.Serialize(s));
        File.Move(tmp, StatusPath, true);
    }

    private static T? ReadJson<T>(string path) where T : class
    {
        try { return File.Exists(path) ? System.Text.Json.JsonSerializer.Deserialize<T>(File.ReadAllText(path)) : null; }
        catch (Exception) { return null; }
    }
}
