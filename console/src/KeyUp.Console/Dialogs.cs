// SPDX-License-Identifier: GPL-2.0-only
using System.Windows;
using System.Windows.Controls;
using KeyUp.Console.Core;

namespace KeyUp.Console;

/// <summary>Small modal dialogs, built in code.</summary>
public class DialogBase : Window
{
    protected readonly StackPanel Body = new() { Margin = new Thickness(18) };

    protected DialogBase(string title, double width = 440)
    {
        Title = title;
        Width = width;
        SizeToContent = SizeToContent.Height;
        ResizeMode = ResizeMode.NoResize;
        WindowStartupLocation = WindowStartupLocation.CenterOwner;
        ShowInTaskbar = false;
        Content = Body;
    }

    protected TextBlock Note(string text) =>
        Add(new TextBlock { Text = text, TextWrapping = TextWrapping.Wrap, Foreground = Theme.Muted, Margin = new Thickness(0, 0, 0, 12) });

    protected TextBox Field(string label, string value = "")
    {
        Add(new TextBlock { Text = label, Margin = new Thickness(0, 0, 0, 4) });
        return Add(new TextBox { Text = value, Margin = new Thickness(0, 0, 0, 10) });
    }

    protected ComboBox Choice(string label, IEnumerable<string> items, string selected)
    {
        Add(new TextBlock { Text = label, Margin = new Thickness(0, 0, 0, 4) });
        var c = Add(new ComboBox { Margin = new Thickness(0, 0, 0, 10) });
        foreach (var i in items) c.Items.Add(i);
        c.SelectedItem = c.Items.Contains(selected) ? selected : (c.Items.Count > 0 ? c.Items[0] : null);
        return c;
    }

    protected T Add<T>(T e) where T : UIElement { Body.Children.Add(e); return e; }

    protected StackPanel Buttons(params (string Text, bool IsDefault, Action OnClick)[] buttons)
    {
        var row = Add(new StackPanel { Orientation = Orientation.Horizontal, HorizontalAlignment = HorizontalAlignment.Right,
                                       Margin = new Thickness(0, 8, 0, 0) });
        foreach (var (text, def, click) in buttons)
        {
            var b = new Button { Content = text, IsDefault = def, MinWidth = 90 };
            b.Click += (_, _) => click();
            row.Children.Add(b);
        }
        return row;
    }
}

public sealed class ActivateDialog : DialogBase
{
    public string Server => server.Text.Trim();
    public string Code => code.Text.Trim();
    private readonly TextBox server, code;

    public ActivateDialog(string currentServer) : base("Connect to KeyUp")
    {
        Note("In the KeyUp web UI open Dispatch Consoles, press Activate on this console, and enter the server and the code shown there.");
        server = Field("Server", currentServer);
        code = Field("Activation code");
        code.CharacterCasing = CharacterCasing.Upper;
        Buttons(("Connect", true, () =>
        {
            if (Server.Length == 0 || Code.Length == 0)
            {
                MessageBox.Show(this, "Enter the server and the activation code.", Title);
                return;
            }
            DialogResult = true;
        }), ("Cancel", false, () => DialogResult = false));
        Loaded += (_, _) => (server.Text.Length == 0 ? server : code).Focus();
    }
}

public sealed class InputDialog : DialogBase
{
    private readonly TextBox box;
    public string Value => box.Text.Trim();

    public InputDialog(string title, string note, string label, string ok, string value = "") : base(title, 380)
    {
        Note(note);
        box = Field(label, value);
        Buttons((ok, true, () => DialogResult = true), ("Cancel", false, () => DialogResult = false));
        Loaded += (_, _) => box.Focus();
    }
}

public sealed class InhibitDialog : DialogBase
{
    private readonly TextBox unit;
    public int Unit => int.TryParse(unit.Text.Trim(), out var u) ? u : 0;
    public bool Inhibit { get; private set; }

    public InhibitDialog(string unitText = "") : base("Inhibit / uninhibit a radio", 400)
    {
        Note("Inhibiting a radio ID stops it from talking anywhere on the system: radios, PoC phones and consoles using it. " +
             "Uninhibit restores it.");
        unit = Field("Radio ID", unitText);
        Buttons(("Inhibit", false, () => { Inhibit = true; DialogResult = Unit > 0; }),
                ("Uninhibit", false, () => { Inhibit = false; DialogResult = Unit > 0; }),
                ("Cancel", true, () => DialogResult = false));
    }
}

public sealed class PatchDialog : DialogBase
{
    private readonly ComboBox a, b;
    private readonly List<Talkgroup> tgs;
    public int TgA => tgs.ElementAtOrDefault(a.SelectedIndex)?.Id ?? 0;
    public int TgB => tgs.ElementAtOrDefault(b.SelectedIndex)?.Id ?? 0;
    public bool Link { get; private set; }

    public PatchDialog(IEnumerable<Talkgroup> talkgroups, IEnumerable<Patch> patches) : base("Patch talkgroups", 420)
    {
        tgs = talkgroups.OrderBy(t => t.Id).ToList();
        var names = tgs.Select(t => $"{t.Id} - {t.Name}").ToList();
        Note("A patch links two talkgroups: a call on either is heard on both, across sites and modes.");
        a = Choice("Talkgroup", names, names.FirstOrDefault() ?? "");
        b = Choice("Patched with", names, names.Skip(1).FirstOrDefault() ?? "");
        var current = patches.ToList();
        if (current.Count > 0)
            Note("Current patches: " + string.Join(", ", current.Select(p => $"{p.TgA}+{p.TgB}")));
        Buttons(("Patch", true, () => { Link = true; DialogResult = TgA != TgB; }),
                ("Unpatch", false, () => { Link = false; DialogResult = TgA != TgB; }),
                ("Cancel", false, () => DialogResult = false));
    }
}

public sealed class SettingsDialog : DialogBase
{
    private readonly ComboBox output, input, transport;
    private readonly TextBox pttKey;
    public string OutputDevice => (string?)output.SelectedItem == "Default" ? "" : (string?)output.SelectedItem ?? "";
    public string InputDevice => (string?)input.SelectedItem == "Default" ? "" : (string?)input.SelectedItem ?? "";
    public string PttKey => pttKey.Text.Trim();
    public TransportPreference Transport => Enum.TryParse<TransportPreference>((string?)transport.SelectedItem, out var t) ? t : TransportPreference.Auto;

    public SettingsDialog(ConsoleSettings s, bool serviceInstalled) : base("Settings", 460)
    {
        output = Choice("Speaker", new[] { "Default" }.Concat(WinMm.OutputDevices()), s.OutputDevice.Length > 0 ? s.OutputDevice : "Default");
        input = Choice("Microphone", new[] { "Default" }.Concat(WinMm.InputDevices()), s.InputDevice.Length > 0 ? s.InputDevice : "Default");
        pttKey = Field("Push-to-talk key (e.g. Space, F12, RightCtrl, Pause)", s.PttKey);
        transport = Choice("Connection", Enum.GetNames<TransportPreference>(), s.Transport.ToString());
        Note("Auto: through the built-in WireGuard tunnel when it is up, otherwise over HTTPS. " +
             (serviceInstalled ? "The KeyUp Tunnel service is installed." :
              "The KeyUp Tunnel service is not installed; install it once (administrator) to use the tunnel."));
        if (!serviceInstalled)
        {
            var btn = Add(new Button { Content = "Install tunnel service…", HorizontalAlignment = HorizontalAlignment.Left });
            btn.Click += (_, _) =>
            {
                bool ok = TunnelService.RunElevated("--install-service");
                MessageBox.Show(this, ok ? "The KeyUp Tunnel service is installed and running." : "The service was not installed.", Title);
            };
        }
        Buttons(("Save", true, () => DialogResult = true), ("Cancel", false, () => DialogResult = false));
    }
}
