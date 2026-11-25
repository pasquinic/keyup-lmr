// SPDX-License-Identifier: GPL-2.0-only
using System.Diagnostics;
using System.IO;
using System.ServiceProcess;
using System.Text.Json;
using KeyUp.Console.Core;

namespace KeyUp.Console;

/// <summary>
/// "KeyUp Tunnel" Windows service (LocalSystem). It owns the console's WireGuard adapter so the console
/// itself runs without administrator rights: it brings up the tunnel the console asks for in
/// %ProgramData%\KeyUp Dispatch Console\tunnel.json and reports in tunnel-status.json.
/// </summary>
public sealed class TunnelService : ServiceBase
{
    public const string Name = "KeyUpTunnel";
    private Thread? worker;
    private volatile bool stopping;
    private readonly WireGuardTunnel tunnel = new();

    public TunnelService() { ServiceName = Name; CanStop = true; CanShutdown = true; }

    protected override void OnStart(string[] args)
    {
        stopping = false;
        worker = new Thread(Run) { IsBackground = true, Name = "tunnel" };
        worker.Start();
    }

    protected override void OnStop() => Shutdown();
    protected override void OnShutdown() => Shutdown();

    private void Shutdown()
    {
        stopping = true;
        worker?.Join(5000);
        try { tunnel.Down(); } catch (Exception) { }
        TunnelHandoff.WriteStatus(new TunnelHandoff.Status { State = "down", UpdatedUtc = DateTime.UtcNow });
    }

    /// <summary>Also used in-process when the console runs as administrator without the service.</summary>
    public static void Apply(WireGuardTunnel tunnel, TunnelHandoff.Request? req, ref string appliedKey)
    {
        if (req == null || !req.Enabled)
        {
            if (tunnel.IsUp) tunnel.Down();
            appliedKey = "";
            TunnelHandoff.WriteStatus(new TunnelHandoff.Status { Serial = req?.Serial ?? 0, State = "down", UpdatedUtc = DateTime.UtcNow });
            return;
        }
        var spec = req.Spec;
        var plain = new TunnelSpec { PrivateKey = Secret.Unprotect(spec.PrivateKey), Address = spec.Address,
                                     ServerPublicKey = spec.ServerPublicKey, Endpoint = spec.Endpoint,
                                     AllowedIps = spec.AllowedIps, PersistentKeepalive = spec.PersistentKeepalive };
        if (plain.PrivateKey.Length == 0) throw new InvalidOperationException("tunnel key unreadable on this PC; activate the console again");
        var key = JsonSerializer.Serialize(spec);
        if (!tunnel.IsUp || key != appliedKey)
        {
            tunnel.Up(plain);
            appliedKey = key;
        }
        TunnelHandoff.WriteStatus(new TunnelHandoff.Status { Serial = req.Serial, State = "up",
                                                             LastHandshakeUtc = tunnel.LastHandshake(), UpdatedUtc = DateTime.UtcNow });
    }

    private void Run()
    {
        string applied = "";
        while (!stopping)
        {
            try { Apply(tunnel, TunnelHandoff.ReadRequest(), ref applied); }
            catch (Exception e)
            {
                applied = "";
                TunnelHandoff.WriteStatus(new TunnelHandoff.Status { State = "error", Error = e.Message, UpdatedUtc = DateTime.UtcNow });
            }
            for (int i = 0; i < 20 && !stopping; i++) Thread.Sleep(100);
        }
    }

    // ------------------------------------------------------------------ install / uninstall (run elevated)
    public static bool IsInstalled()
    {
        try { return ServiceController.GetServices().Any(s => s.ServiceName == Name); } catch (Exception) { return false; }
    }

    public static int Install()
    {
        var exe = Environment.ProcessPath!;
        Sc($"create {Name} binPath= \"\\\"{exe}\\\" --tunnel-service\" start= auto DisplayName= \"KeyUp Tunnel\"");
        Sc($"description {Name} \"WireGuard tunnel for the KeyUp Dispatch Console\"");
        Sc($"failure {Name} reset= 86400 actions= restart/5000/restart/5000/restart/30000");
        Directory.CreateDirectory(TunnelHandoff.Folder);
        Run("icacls", $"\"{TunnelHandoff.Folder}\" /grant *S-1-5-32-545:(OI)(CI)M");   // Users: modify
        return Sc($"start {Name}");
    }

    public static int Uninstall()
    {
        Sc($"stop {Name}");
        return Sc($"delete {Name}");
    }

    private static int Sc(string args) => Run("sc.exe", args);

    private static int Run(string file, string args)
    {
        var p = Process.Start(new ProcessStartInfo(file, args) { CreateNoWindow = true, UseShellExecute = false })!;
        p.WaitForExit(15000);
        return p.ExitCode;
    }

    /// <summary>Start this program elevated (UAC) with an argument, e.g. --install-service.</summary>
    public static bool RunElevated(string arg)
    {
        try
        {
            var p = Process.Start(new ProcessStartInfo(Environment.ProcessPath!, arg) { UseShellExecute = true, Verb = "runas" });
            p?.WaitForExit(30000);
            return p?.ExitCode == 0;
        }
        catch (Exception) { return false; }   // the user said no to UAC
    }
}
