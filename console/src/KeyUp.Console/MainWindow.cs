// SPDX-License-Identifier: GPL-2.0-only
using System.Security.Principal;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Input;
using System.Windows.Media;
using System.Windows.Threading;
using KeyUp.Console.Core;

namespace KeyUp.Console;

/// <summary>The dispatch console window: zones of channel tiles, PTT, activity, emergencies, tunnel.</summary>
public sealed class MainWindow : Window
{
    private readonly ConsoleSettings settings = ConsoleSettings.Load();
    private readonly Mixer mixer = new();
    private ConsoleClient? client;
    private SpeakerOutput? speaker;
    private MicrophoneInput? mic;
    private Profile? profile;
    private readonly Dictionary<int, List<ChannelTile>> tiles = new();
    private readonly Dictionary<long, (CallInfo Info, DateTime Start)> calls = new();
    private int selectedTg;
    private int? txTg, txPending;
    private bool connecting;
    private CancellationTokenSource? reconnect;
    private WireGuardTunnel? localTunnel;           // only when running as administrator without the service
    private string transportLabel = "offline";

    // UI
    private readonly TextBlock title = new() { FontSize = 18, FontWeight = FontWeights.Bold, Text = "KeyUp Dispatch Console" };
    private readonly TextBlock subtitle = new() { Foreground = Theme.Muted };
    private readonly TextBlock link = new() { FontWeight = FontWeights.SemiBold, Margin = new Thickness(16, 0, 16, 0), VerticalAlignment = VerticalAlignment.Center };
    private readonly TextBlock clock = new() { FontSize = 16, VerticalAlignment = VerticalAlignment.Center, Margin = new Thickness(12, 0, 4, 0) };
    private readonly Border emergencyBar = new() { Background = Theme.Emergency, Padding = new Thickness(12, 6, 12, 6), Visibility = Visibility.Collapsed };
    private readonly TextBlock emergencyText = new() { FontWeight = FontWeights.Bold, Foreground = Brushes.White, VerticalAlignment = VerticalAlignment.Center };
    private readonly TabControl zoneTabs = new() { Background = Theme.Bg, BorderBrush = Theme.Border, Margin = new Thickness(8) };
    private readonly ListBox activity = new() { Margin = new Thickness(0, 4, 0, 8) };
    private readonly TextBlock patchList = new() { Foreground = Theme.Muted, TextWrapping = TextWrapping.Wrap };
    private readonly Button pttButton = new() { Height = 64, FontSize = 20, FontWeight = FontWeights.Bold, Margin = new Thickness(8) };
    private readonly TextBlock status = new() { Foreground = Theme.Muted, VerticalAlignment = VerticalAlignment.Center, Margin = new Thickness(12, 0, 12, 0) };
    private readonly Button btnInhibit = new() { Content = "Inhibit…" }, btnPatch = new() { Content = "Patch…" };
    private readonly Dictionary<int, (int Src, string Alias)> emergencies = new();

    public MainWindow()
    {
        Title = "KeyUp Dispatch Console";
        Width = 1280;
        Height = 820;
        MinWidth = 900;
        MinHeight = 560;
        selectedTg = settings.SelectedTalkgroup;
        Content = BuildLayout();
        foreach (var (tg, v) in settings.Volumes) mixer.SetVolume(tg, v);

        var timer = new DispatcherTimer { Interval = TimeSpan.FromSeconds(1) };
        timer.Tick += (_, _) => { clock.Text = DateTime.Now.ToString("HH:mm:ss"); foreach (var l in tiles.Values) foreach (var t in l) t.Tick(); };
        timer.Start();
        PreviewKeyDown += OnKeyDown;
        PreviewKeyUp += OnKeyUp;
        Loaded += async (_, _) => await StartAsync();
        Closing += (_, _) => Shutdown();
    }

    // ================================================================== layout
    private UIElement BuildLayout()
    {
        var root = new DockPanel();

        var header = new Border { Background = Theme.Panel, BorderBrush = Theme.Border, BorderThickness = new Thickness(0, 0, 0, 1), Padding = new Thickness(14, 8, 14, 8) };
        var hp = new DockPanel();
        var right = new StackPanel { Orientation = Orientation.Horizontal };
        DockPanel.SetDock(right, Dock.Right);
        var btnConnect = new Button { Content = "Connect to KeyUp…" };
        btnConnect.Click += async (_, _) => await ActivateAsync();
        btnInhibit.Click += async (_, _) => await InhibitAsync();
        btnPatch.Click += async (_, _) => await PatchAsync();
        var btnSettings = new Button { Content = "Settings…" };
        btnSettings.Click += async (_, _) => await SettingsAsync();
        right.Children.Add(btnInhibit);
        right.Children.Add(btnPatch);
        right.Children.Add(btnSettings);
        right.Children.Add(btnConnect);
        right.Children.Add(clock);
        hp.Children.Add(right);
        var names = new StackPanel { Orientation = Orientation.Vertical };
        names.Children.Add(title);
        names.Children.Add(subtitle);
        var left = new StackPanel { Orientation = Orientation.Horizontal };
        left.Children.Add(names);
        left.Children.Add(link);
        hp.Children.Add(left);
        header.Child = hp;
        DockPanel.SetDock(header, Dock.Top);
        root.Children.Add(header);

        var ep = new DockPanel();
        var clear = new Button { Content = "Acknowledge / clear", Background = Brushes.White, Foreground = Brushes.Black };
        clear.Click += async (_, _) => await ClearEmergenciesAsync();
        DockPanel.SetDock(clear, Dock.Right);
        ep.Children.Add(clear);
        ep.Children.Add(emergencyText);
        emergencyBar.Child = ep;
        DockPanel.SetDock(emergencyBar, Dock.Top);
        root.Children.Add(emergencyBar);

        var footer = new DockPanel { Background = Theme.Panel };
        pttButton.PreviewMouseLeftButtonDown += async (_, e) => { e.Handled = true; pttButton.CaptureMouse(); await StartTxAsync(selectedTg); };
        pttButton.PreviewMouseLeftButtonUp += async (_, e) => { e.Handled = true; pttButton.ReleaseMouseCapture(); await StopTxAsync(); };
        pttButton.Width = 420;
        DockPanel.SetDock(pttButton, Dock.Left);
        footer.Children.Add(pttButton);
        footer.Children.Add(status);
        DockPanel.SetDock(footer, Dock.Bottom);
        root.Children.Add(footer);

        var side = new DockPanel { Width = 340, Margin = new Thickness(0, 8, 8, 8) };
        var actHead = new TextBlock { Text = "Activity", FontWeight = FontWeights.Bold, FontSize = 15 };
        DockPanel.SetDock(actHead, Dock.Top);
        side.Children.Add(actHead);
        var patchBox = new StackPanel();
        patchBox.Children.Add(new TextBlock { Text = "Patches", FontWeight = FontWeights.Bold, FontSize = 15 });
        patchBox.Children.Add(patchList);
        DockPanel.SetDock(patchBox, Dock.Bottom);
        side.Children.Add(patchBox);
        side.Children.Add(activity);
        DockPanel.SetDock(side, Dock.Right);
        root.Children.Add(side);
        root.Children.Add(zoneTabs);
        UpdatePttButton();
        return root;
    }

    // ================================================================== connection
    private async Task StartAsync()
    {
        try
        {
            speaker = new SpeakerOutput(mixer.Pull);
            speaker.Start(WinMm.DeviceIndex(WinMm.OutputDevices(), settings.OutputDevice));
        }
        catch (Exception e) { Log($"Speaker: {e.Message}"); }
        if (!settings.IsActivated)
        {
            SetLink("not activated", Theme.Warn);
            await ActivateAsync();
            return;
        }
        Reconnect();
    }

    private void Reconnect()
    {
        reconnect?.Cancel();
        reconnect = new CancellationTokenSource();
        var ct = reconnect.Token;
        _ = Task.Run(async () =>
        {
            while (!ct.IsCancellationRequested)
            {
                if (client is { Connected: true }) { await Task.Delay(1000, ct).ContinueWith(_ => { }); continue; }
                await Dispatcher.InvokeAsync(async () => await ConnectOnceAsync()).Task.Unwrap();
                if (client is { Connected: true }) continue;
                try { await Task.Delay(5000, ct); } catch (OperationCanceledException) { return; }
            }
        });
    }

    private async Task ConnectOnceAsync()
    {
        if (connecting) return;
        connecting = true;
        try
        {
            SetLink("connecting…", Theme.Warn);
            bool tunnelUp = settings.Transport != TransportPreference.Https && await EnsureTunnelAsync();
            foreach (var (url, transport) in ConnectionPlan.Order(settings, tunnelUp))
            {
                var c = new ConsoleClient();
                try
                {
                    var refused = await c.ConnectAsync(url, settings.Token, transport, Environment.MachineName,
                                                       TimeSpan.FromSeconds(transport == "wireguard" ? 5 : 10));
                    if (refused != null)
                    {
                        await c.DisposeAsync();
                        SetLink($"refused: {refused}", Theme.Tx);
                        Log($"The server refused this console ({refused}).");
                        if (refused is "console_revoked" or "invalid_token") { reconnect?.Cancel(); }
                        return;
                    }
                    await OnConnectedAsync(c, transport);
                    return;
                }
                catch (Exception e)
                {
                    await c.DisposeAsync();
                    Log($"{transport}: {e.Message}");
                }
            }
            SetLink("offline · retrying", Theme.Tx);
        }
        finally { connecting = false; }
    }

    /// <summary>Bring up the built-in WireGuard tunnel (service, or in-process when admin). True when up.</summary>
    private async Task<bool> EnsureTunnelAsync()
    {
        if (!settings.Tunnel.IsComplete && settings.Tunnel.Address.Length == 0) return false;
        try
        {
            if (TunnelService.IsInstalled())
            {
                ConnectionPlan.RequestTunnel(settings, true);
                for (int i = 0; i < 30; i++)
                {
                    var st = TunnelHandoff.ReadStatus();
                    if (st?.State == "up") return true;
                    if (st?.State == "error") { Log($"Tunnel: {st.Error}"); return false; }
                    await Task.Delay(200);
                }
                return false;
            }
            if (IsAdministrator())
            {
                localTunnel ??= new WireGuardTunnel();
                if (!localTunnel.IsUp) localTunnel.Up(settings.PlainTunnel());
                return true;
            }
        }
        catch (Exception e) { Log($"Tunnel: {e.Message}"); }
        return false;
    }

    private static bool IsAdministrator()
    {
        using var id = WindowsIdentity.GetCurrent();
        return new WindowsPrincipal(id).IsInRole(WindowsBuiltInRole.Administrator);
    }

    private async Task OnConnectedAsync(ConsoleClient c, string transport)
    {
        client = c;
        transportLabel = transport == "wireguard" ? "WireGuard" : "HTTPS";
        c.ProfileChanged += p => Dispatcher.Invoke(() => ApplyProfile(p));
        c.CallStarted += i => Dispatcher.Invoke(() => OnCallStarted(i));
        c.CallEnded += (id, tg, reason) => Dispatcher.Invoke(() => OnCallEnded(id, tg, reason));
        c.Audio += (tg, src, id, ulaw) => mixer.Add(id, tg, ulaw);
        c.Granted += tg => Dispatcher.Invoke(() => OnGranted(tg));
        c.Denied += (tg, reason) => Dispatcher.Invoke(() => OnDenied(tg, reason));
        c.TxEnded += (tg, reason) => Dispatcher.Invoke(() => OnTxEnded(tg, reason));
        c.Emergency += (tg, src, alias, on) => Dispatcher.Invoke(() => OnEmergency(tg, src, alias, on));
        c.InhibitResult += (unit, on, ok, reason) => Dispatcher.Invoke(() =>
            Log(ok ? $"Radio {unit} {(on ? "inhibited" : "uninhibited")}" : $"Inhibit {unit} failed: {reason}"));
        c.AuthFailed += reason => Dispatcher.Invoke(() => { SetLink($"refused: {reason}", Theme.Tx); Log($"Logged out by the server: {reason}"); });
        c.Disconnected += reason => Dispatcher.Invoke(() => OnDisconnected(c, reason));
        ApplyProfile(c.Profile!);
        SetLink($"online · {transportLabel}", Theme.Rx);
        Log($"Connected over {transportLabel}");
        await SendMonitorAsync();
    }

    private void OnDisconnected(ConsoleClient c, string reason)
    {
        if (client != c) return;
        client = null;
        _ = StopTxAsync();
        SetLink("offline · retrying", Theme.Tx);
        Log($"Disconnected: {reason}");
    }

    // ================================================================== profile / zones
    private void ApplyProfile(Profile p)
    {
        profile = p;
        title.Text = $"{p.SystemName} · {p.ConsoleName}";
        subtitle.Text = $"Radio ID {p.UnitId} ({p.UnitAlias})";
        btnInhibit.IsEnabled = p.Rights.Inhibit;
        btnPatch.IsEnabled = p.Rights.Patch;
        if (settings.Monitored.Count == 0) settings.Monitored = p.Talkgroups.Keys.ToList();
        if (!p.Talkgroups.ContainsKey(selectedTg)) selectedTg = p.Zones.SelectMany(z => z.Talkgroups).FirstOrDefault();
        int keepTab = zoneTabs.SelectedIndex;
        zoneTabs.Items.Clear();
        tiles.Clear();
        foreach (var z in p.Zones)
        {
            var wrap = new WrapPanel { Margin = new Thickness(4) };
            foreach (var id in z.Talkgroups)
            {
                if (!p.Talkgroups.TryGetValue(id, out var tg)) continue;
                var tile = new ChannelTile(tg, settings.Monitored.Contains(id), mixer.VolumeOf(id)) { Selected = id == selectedTg };
                tile.MonitorChanged += async (t, on) => await SetMonitoredAsync(t.Tg, on);
                tile.SelectRequested += t => Select(t.Tg);
                tile.PttPressed += async t => await StartTxAsync(t.Tg);
                tile.PttReleased += async _ => await StopTxAsync();
                tile.VolumeChanged += (t, v) => { mixer.SetVolume(t.Tg, v); settings.Volumes[t.Tg] = v; };
                if (!tiles.TryGetValue(id, out var list)) tiles[id] = list = new();
                list.Add(tile);
                wrap.Children.Add(tile);
            }
            var tab = new TabItem { Header = z.Name, Content = new ScrollViewer { Content = wrap, VerticalScrollBarVisibility = ScrollBarVisibility.Auto } };
            if (!string.IsNullOrEmpty(z.Color)) tab.Foreground = Theme.Brush(z.Color);
            zoneTabs.Items.Add(tab);
        }
        zoneTabs.SelectedIndex = keepTab >= 0 && keepTab < zoneTabs.Items.Count ? keepTab : 0;
        patchList.Text = p.Patches.Count == 0 ? "none" : string.Join("\n", p.Patches.Select(x =>
            $"{x.TgA} {p.Talkgroups.GetValueOrDefault(x.TgA)?.Name} + {x.TgB} {p.Talkgroups.GetValueOrDefault(x.TgB)?.Name}"));
        foreach (var (tg, e) in p.Emergencies) emergencies[tg] = e;
        foreach (var (id, c) in calls) SetTiles(c.Info.Tg, TileState.Receiving, $"{c.Info.Alias} ({c.Info.Src})");
        UpdateEmergencies();
        UpdatePttButton();
        settings.Save();
    }

    private async Task SetMonitoredAsync(int tg, bool on)
    {
        if (on && !settings.Monitored.Contains(tg)) settings.Monitored.Add(tg);
        if (!on) settings.Monitored.Remove(tg);
        foreach (var t in tiles.GetValueOrDefault(tg) ?? new()) t.Monitored = on;
        settings.Save();
        await SendMonitorAsync();
    }

    private async Task SendMonitorAsync()
    {
        if (client == null || profile == null) return;
        var want = settings.Monitored.Where(profile.Talkgroups.ContainsKey).ToList();
        if (txTg is int tx && !want.Contains(tx)) want.Add(tx);
        await client.MonitorAsync(want);
    }

    private void Select(int tg)
    {
        selectedTg = tg;
        settings.SelectedTalkgroup = tg;
        foreach (var (id, list) in tiles) foreach (var t in list) t.Selected = id == tg;
        UpdatePttButton();
        settings.Save();
    }

    private void UpdatePttButton()
    {
        var name = profile?.Talkgroups.GetValueOrDefault(selectedTg)?.Name;
        pttButton.Content = txTg != null ? $"● TRANSMITTING · TG {txTg}" : name != null ? $"PUSH TO TALK · {name}" : "PUSH TO TALK";
        pttButton.Background = txTg != null ? Theme.Tx : txPending != null ? Theme.Warn : Theme.Panel2;
        pttButton.IsEnabled = name != null;
    }

    // ================================================================== calls
    private void OnCallStarted(CallInfo i)
    {
        calls[i.CallId] = (i, DateTime.Now);
        if (txTg == i.Tg && i.Src == profile?.UnitId) return;
        SetTiles(i.Tg, TileState.Receiving, $"{i.Alias} ({i.Src})");
        if (i.Emergency) { emergencies[i.Tg] = (i.Src, i.Alias); UpdateEmergencies(); }
    }

    private void OnCallEnded(long id, int tg, string reason)
    {
        mixer.Drop(id);
        if (calls.Remove(id, out var c))
        {
            var secs = (DateTime.Now - c.Start).TotalSeconds;
            Log($"TG {tg} {profile?.Talkgroups.GetValueOrDefault(tg)?.Name}: {c.Info.Alias} ({c.Info.Src}) {secs:0}s");
            foreach (var t in tiles.GetValueOrDefault(tg) ?? new()) t.ShowLastHeard($"last: {c.Info.Alias} {DateTime.Now:HH:mm}");
        }
        if (txTg != tg && !calls.Values.Any(x => x.Info.Tg == tg)) SetTiles(tg, TileState.Idle);
    }

    private void SetTiles(int tg, TileState s, string text = "")
    {
        foreach (var t in tiles.GetValueOrDefault(tg) ?? new()) t.SetState(s, text);
    }

    // ================================================================== transmit
    private async Task StartTxAsync(int tg)
    {
        if (client == null || tg == 0 || txTg != null || txPending != null) return;
        txPending = tg;
        SetTiles(tg, TileState.Requesting);
        UpdatePttButton();
        await client.PttOnAsync(tg);
    }

    private void OnGranted(int tg)
    {
        if (txPending != tg) { _ = client?.PttOffAsync(); return; }   // released before the grant arrived
        txPending = null;
        txTg = tg;
        SetTiles(tg, TileState.Transmitting);
        mixer.Add(-1, 0, Tones.Grant);
        try
        {
            mic = new MicrophoneInput(frame => { var c = client; if (c != null && txTg != null) _ = c.SendAudioAsync(G711.Encode(frame)); });
            mic.Start(WinMm.DeviceIndex(WinMm.InputDevices(), settings.InputDevice));
        }
        catch (Exception e) { Log($"Microphone: {e.Message}"); }
        status.Text = $"Transmitting on TG {tg}";
        UpdatePttButton();
    }

    private void OnDenied(int tg, string reason)
    {
        txPending = null;
        SetTiles(tg, calls.Values.Any(x => x.Info.Tg == tg) ? TileState.Receiving : TileState.Idle);
        mixer.Add(-2, 0, Tones.Deny);
        status.Text = $"TG {tg}: {Describe(reason)}";
        UpdatePttButton();
    }

    private void OnTxEnded(int tg, string reason)
    {
        if (txTg == tg) EndTxLocal();
        if (reason is not ("released" or "")) status.Text = $"Transmission ended: {Describe(reason)}";
    }

    private async Task StopTxAsync()
    {
        if (txPending != null) { txPending = null; UpdatePttButton(); }
        if (txTg == null) return;
        EndTxLocal();
        if (client != null) await client.PttOffAsync();
    }

    private void EndTxLocal()
    {
        mic?.Dispose();
        mic = null;
        if (txTg is int tg) SetTiles(tg, calls.Values.Any(x => x.Info.Tg == tg) ? TileState.Receiving : TileState.Idle);
        txTg = null;
        status.Text = "";
        UpdatePttButton();
    }

    private static string Describe(string reason) => reason switch
    {
        "busy" => "busy, a higher-priority call is in progress",
        "not_permitted" => "this radio ID may not use that talkgroup",
        "unit_disabled" or "inhibited" => "this console's radio ID is inhibited",
        "preempted" => "taken over by a higher-priority call",
        "max_call_time" => "maximum call time reached",
        _ => reason.Replace('_', ' '),
    };

    private Key PttKey => Enum.TryParse<Key>(settings.PttKey, true, out var k) ? k : Key.Space;

    private async void OnKeyDown(object sender, KeyEventArgs e)
    {
        var key = e.Key == Key.System ? e.SystemKey : e.Key;
        if (key != PttKey || e.IsRepeat || Keyboard.FocusedElement is TextBox) return;
        e.Handled = true;
        await StartTxAsync(selectedTg);
    }

    private async void OnKeyUp(object sender, KeyEventArgs e)
    {
        var key = e.Key == Key.System ? e.SystemKey : e.Key;
        if (key != PttKey || Keyboard.FocusedElement is TextBox) return;
        e.Handled = true;
        await StopTxAsync();
    }

    // ================================================================== emergencies / rights
    private void OnEmergency(int tg, int src, string alias, bool on)
    {
        if (on) { emergencies[tg] = (src, alias); mixer.Add(-3, 0, Tones.Emergency); Log($"⚠ EMERGENCY TG {tg} by {alias} ({src})"); }
        else { emergencies.Remove(tg); Log($"Emergency on TG {tg} cleared"); }
        UpdateEmergencies();
    }

    private void UpdateEmergencies()
    {
        foreach (var (id, list) in tiles) foreach (var t in list) t.Emergency = emergencies.ContainsKey(id);
        emergencyBar.Visibility = emergencies.Count > 0 ? Visibility.Visible : Visibility.Collapsed;
        emergencyText.Text = "⚠ EMERGENCY  " + string.Join("   ", emergencies.Select(e =>
            $"TG {e.Key} {profile?.Talkgroups.GetValueOrDefault(e.Key)?.Name}: {e.Value.Alias} ({e.Value.Src})"));
    }

    private async Task ClearEmergenciesAsync()
    {
        foreach (var tg in emergencies.Keys.ToList())
            if (client != null) await client.ClearEmergencyAsync(tg);
        emergencies.Clear();
        UpdateEmergencies();
    }

    private async Task InhibitAsync()
    {
        if (client == null || profile?.Rights.Inhibit != true) return;
        var d = new InhibitDialog { Owner = this };
        if (d.ShowDialog() != true) return;
        await client.InhibitAsync(d.Unit, d.Inhibit);
    }

    private async Task PatchAsync()
    {
        if (client == null || profile?.Rights.Patch != true) return;
        var d = new PatchDialog(profile.Talkgroups.Values, profile.Patches) { Owner = this };
        if (d.ShowDialog() != true) return;
        await client.PatchAsync(d.TgA, d.TgB, d.Link);
        Log($"{(d.Link ? "Patch" : "Unpatch")} TG {d.TgA} + {d.TgB}");
    }

    // ================================================================== activation / settings
    private async Task ActivateAsync()
    {
        var d = new ActivateDialog(settings.Server) { Owner = this };
        if (d.ShowDialog() != true) return;
        try
        {
            Cursor = Cursors.Wait;
            var server = Activation.NormalizeServer(d.Server);
            var a = await Activation.ActivateAsync(server, d.Code, Environment.MachineName);
            settings.Apply(a, server);
            settings.Monitored.Clear();
            settings.Save();
            Log($"Activated as console \"{a.Name}\"");
            if (!TunnelService.IsInstalled() && !IsAdministrator())
            {
                var r = MessageBox.Show(this, "This console reaches KeyUp best through its own WireGuard tunnel. " +
                    "Install the KeyUp Tunnel service now? (Windows asks for administrator approval once.)\n\n" +
                    "Without it the console connects over HTTPS.", Title, MessageBoxButton.YesNo, MessageBoxImage.Question);
                if (r == MessageBoxResult.Yes && !TunnelService.RunElevated("--install-service"))
                    Log("Tunnel service not installed; using HTTPS");
            }
            if (client != null) { await client.DisposeAsync(); client = null; }
            Reconnect();
        }
        catch (Exception e)
        {
            MessageBox.Show(this, $"Could not activate this console.\n\n{e.Message}", Title, MessageBoxButton.OK, MessageBoxImage.Error);
        }
        finally { Cursor = null; }
    }

    private async Task SettingsAsync()
    {
        var d = new SettingsDialog(settings, TunnelService.IsInstalled()) { Owner = this };
        if (d.ShowDialog() != true) return;
        bool audioChanged = d.OutputDevice != settings.OutputDevice;
        bool transportChanged = d.Transport != settings.Transport;
        settings.OutputDevice = d.OutputDevice;
        settings.InputDevice = d.InputDevice;
        settings.PttKey = d.PttKey.Length > 0 ? d.PttKey : "Space";
        settings.Transport = d.Transport;
        settings.Save();
        if (audioChanged)
        {
            speaker?.Dispose();
            speaker = new SpeakerOutput(mixer.Pull);
            try { speaker.Start(WinMm.DeviceIndex(WinMm.OutputDevices(), settings.OutputDevice)); } catch (Exception e) { Log($"Speaker: {e.Message}"); }
        }
        if (transportChanged && client != null)
        {
            if (settings.Transport == TransportPreference.Https && TunnelService.IsInstalled()) ConnectionPlan.RequestTunnel(settings, false);
            await client.DisposeAsync();
            client = null;
        }
    }

    // ================================================================== helpers
    private void SetLink(string text, Brush color)
    {
        link.Text = "● " + text;
        link.Foreground = color;
    }

    private void Log(string text)
    {
        activity.Items.Insert(0, $"{DateTime.Now:HH:mm:ss}  {text}");
        while (activity.Items.Count > 500) activity.Items.RemoveAt(activity.Items.Count - 1);
    }

    private void Shutdown()
    {
        reconnect?.Cancel();
        settings.Save();
        mic?.Dispose();
        speaker?.Dispose();
        if (client != null) _ = client.DisposeAsync();
        localTunnel?.Dispose();
    }
}

/// <summary>Local alert tones (mu-law), mixed into the speaker output.</summary>
public static class Tones
{
    public static readonly byte[] Grant = Make((1200, 60), (0, 40), (1200, 60));
    public static readonly byte[] Deny = Make((420, 350));
    public static readonly byte[] Emergency = Make((1000, 150), (700, 150), (1000, 150), (700, 150));

    private static byte[] Make(params (double Hz, int Ms)[] parts)
    {
        var pcm = new List<short>();
        foreach (var (hz, ms) in parts)
            for (int i = 0; i < ms * 8; i++)
                pcm.Add((short)(hz == 0 ? 0 : 7000 * Math.Sin(2 * Math.PI * hz * i / 8000)));
        while (pcm.Count % 160 != 0) pcm.Add(0);
        return G711.Encode(pcm.ToArray());
    }
}
