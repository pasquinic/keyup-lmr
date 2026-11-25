// SPDX-License-Identifier: GPL-2.0-only
namespace KeyUp.Console.Core;

/// <summary>Which URLs to try, in order, for a console's settings and tunnel state.</summary>
public static class ConnectionPlan
{
    public static List<(string Url, string Transport)> Order(ConsoleSettings s, bool tunnelUp)
    {
        var o = new List<(string, string)>();
        bool tunnel = tunnelUp && s.TunnelUrl.Length > 0;
        if (s.Transport != TransportPreference.Https && tunnel) o.Add((s.TunnelUrl, "wireguard"));
        if (s.Transport != TransportPreference.Tunnel && s.PublicUrl.Length > 0) o.Add((s.PublicUrl, "https"));
        return o;
    }

    /// <summary>Hand the tunnel to the KeyUp Tunnel service (private key encrypted for this machine).</summary>
    public static long RequestTunnel(ConsoleSettings s, bool enabled)
    {
        var spec = s.PlainTunnel();
        spec.PrivateKey = Secret.ProtectForMachine(spec.PrivateKey);
        var serial = DateTime.UtcNow.Ticks;
        TunnelHandoff.WriteRequest(new TunnelHandoff.Request { Enabled = enabled && spec.IsComplete, Spec = spec,
                                                              Serial = serial });
        return serial;
    }
}
