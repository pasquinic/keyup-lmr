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
        MaxHeight = SystemParameters.WorkArea.Height - 40;
        Content = new ScrollViewer { Content = Body, VerticalScrollBarVisibility = ScrollBarVisibility.Auto };
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

/// <summary>What the patch dialog asks the server to do.</summary>
public sealed record PatchRequest(int[] Talkgroups, bool On, int? PatchId, string Name, string Text)
{
    public string Describe() => Text;
}

/// <summary>Patch any number of talkgroups together, add talkgroups to a patch, take them out, or remove a patch.</summary>
public sealed class PatchDialog : DialogBase
{
    private readonly List<(Talkgroup Tg, CheckBox Box)> boxes = new();
    private readonly TextBox name;
    public PatchRequest? Result { get; private set; }

    private int[] Checked => boxes.Where(b => b.Box.IsChecked == true).Select(b => b.Tg.Id).ToArray();

    public PatchDialog(IEnumerable<Talkgroup> talkgroups, IEnumerable<Patch> patches) : base("Patch talkgroups", 480)
    {
        var tgs = talkgroups.OrderBy(t => t.Id).ToList();
        var byId = tgs.ToDictionary(t => t.Id);
        string TgLabel(int id) => byId.TryGetValue(id, out var t) ? $"{id} {t.Name}" : $"{id}";
        Note("A patch links two or more talkgroups: a call on any of them is heard on all, across sites and modes. " +
             "Tick the talkgroups, then choose what to do.");

        var list = new StackPanel();
        foreach (var t in tgs)
        {
            var cb = new CheckBox { Content = $"{t.Id} - {t.Name}", Foreground = Theme.Text, Margin = new Thickness(2, 3, 2, 3) };
            boxes.Add((t, cb));
            list.Children.Add(cb);
        }
        Add(new Border
        {
            BorderBrush = Theme.Border, BorderThickness = new Thickness(1), Background = Theme.Panel, Padding = new Thickness(6),
            Margin = new Thickness(0, 0, 0, 10),
            Child = new ScrollViewer { Content = list, MaxHeight = 220, VerticalScrollBarVisibility = ScrollBarVisibility.Auto },
        });
        name = Field("Name for a new patch (optional)");

        var actions = Add(new WrapPanel { Margin = new Thickness(0, 0, 0, 10) });
        var create = new Button { Content = "Patch ticked together", IsDefault = true };
        create.Click += (_, _) =>
        {
            var sel = Checked;
            if (sel.Length < 2) { MessageBox.Show(this, "Tick at least two talkgroups.", Title); return; }
            Done(new PatchRequest(sel, true, null, name.Text.Trim(),
                $"Patch {string.Join(" + ", sel)}{(name.Text.Trim().Length > 0 ? $" ({name.Text.Trim()})" : "")}"));
        };
        var remove = new Button { Content = "Take ticked out of patches" };
        remove.Click += (_, _) =>
        {
            var sel = Checked;
            if (sel.Length == 0) { MessageBox.Show(this, "Tick the talkgroups to take out.", Title); return; }
            Done(new PatchRequest(sel, false, null, "", $"Unpatch TG {string.Join(", ", sel)}"));
        };
        actions.Children.Add(create);
        actions.Children.Add(remove);

        var current = patches.ToList();
        Add(new TextBlock { Text = current.Count == 0 ? "No patches." : "Current patches", FontWeight = FontWeights.Bold,
                            Margin = new Thickness(0, 4, 0, 6) });
        foreach (var p in current)
        {
            var row = new DockPanel { Margin = new Thickness(0, 0, 0, 6) };
            var add = new Button { Content = "Add ticked", ToolTip = "Add the ticked talkgroups to this patch" };
            add.Click += (_, _) =>
            {
                var sel = Checked.Where(t => !p.Talkgroups.Contains(t)).ToArray();
                if (sel.Length == 0) { MessageBox.Show(this, "Tick talkgroups that are not in this patch yet.", Title); return; }
                Done(new PatchRequest(sel, true, p.Id, p.Name, $"Add TG {string.Join(", ", sel)} to patch {p.Name}"));
            };
            var drop = new Button { Content = "Unpatch", ToolTip = "Remove this patch" };
            drop.Click += (_, _) => Done(new PatchRequest(Array.Empty<int>(), false, p.Id, p.Name, $"Remove patch {p.Name}"));
            DockPanel.SetDock(drop, Dock.Right);
            DockPanel.SetDock(add, Dock.Right);
            row.Children.Add(drop);
            row.Children.Add(add);
            row.Children.Add(new TextBlock
            {
                Text = $"{p.Name}{(p.Enabled ? "" : " (off)")}\n" + string.Join(" + ", p.Talkgroups.Select(TgLabel)),
                TextWrapping = TextWrapping.Wrap, VerticalAlignment = VerticalAlignment.Center,
            });
            Add(row);
        }
        Buttons(("Close", false, () => DialogResult = false));
    }

    private void Done(PatchRequest r) { Result = r; DialogResult = true; }
}

public sealed class SettingsDialog : DialogBase
{
    private static readonly string[] SideNames = { "Both ears", "Left ear", "Right ear" };
    private static readonly (string Name, double Db)[] Ducks =
        { ("No change", 0), ("6 dB quieter", 6), ("12 dB quieter", 12), ("18 dB quieter", 18), ("24 dB quieter", 24), ("Muted", 60) };
    private readonly ComboBox output, unselectOutput, selectSide, unselectSide, input, transport, duck;
    private readonly CheckBox txTones, rxTones;
    private readonly Slider toneVolume;
    private readonly TextBox pttKey;
    public string OutputDevice => (string?)output.SelectedItem == "Default" ? "" : (string?)output.SelectedItem ?? "";
    public string UnselectDevice => unselectOutput.SelectedIndex <= 0 ? "" : (string?)unselectOutput.SelectedItem ?? "";
    public Side SelectSide => ToSide(selectSide.SelectedIndex);
    public Side UnselectSide => ToSide(unselectSide.SelectedIndex);
    public string InputDevice => (string?)input.SelectedItem == "Default" ? "" : (string?)input.SelectedItem ?? "";
    public string PttKey => pttKey.Text.Trim();
    public TransportPreference Transport => Enum.TryParse<TransportPreference>((string?)transport.SelectedItem, out var t) ? t : TransportPreference.Auto;
    public bool TxTones => txTones.IsChecked == true;
    public bool RxTones => rxTones.IsChecked == true;
    public double ToneVolume => toneVolume.Value;
    public double PriorityDuckDb => Ducks[Math.Max(0, duck.SelectedIndex)].Db;

    private static Side ToSide(int i) => i switch { 1 => Side.Left, 2 => Side.Right, _ => Side.Both };
    private static string SideName(Side s) => s switch { Side.Left => SideNames[1], Side.Right => SideNames[2], _ => SideNames[0] };

    private TextBlock Heading(string text) =>
        Add(new TextBlock { Text = text, FontWeight = FontWeights.Bold, FontSize = 14, Margin = new Thickness(0, 6, 0, 8) });

    public SettingsDialog(ConsoleSettings s, bool serviceInstalled) : base("Settings", 500)
    {
        var outs = WinMm.OutputDevices();
        Heading("Audio routing");
        Note("Selected audio is the talkgroup you talk on, plus tones. Unselected audio is every other monitored talkgroup. " +
             "Give unselected audio its own device (e.g. a desk speaker) or send each to one ear of a headset. " +
             "Each talkgroup tile can also pick its own ear (L / R / L+R).");
        output = Choice("Selected audio: device (headset)", new[] { "Default" }.Concat(outs), s.OutputDevice.Length > 0 ? s.OutputDevice : "Default");
        selectSide = Choice("Selected audio: ear", SideNames, SideName(s.SelectSide));
        unselectOutput = Choice("Unselected audio: device", new[] { "Same as selected audio" }.Concat(outs),
                                s.UnselectDevice.Length > 0 ? s.UnselectDevice : "Same as selected audio");
        unselectSide = Choice("Unselected audio: ear", SideNames, SideName(s.UnselectSide));

        Heading("Tones");
        txTones = Add(new CheckBox { Content = "TX tones: talk-permit and busy / deny", IsChecked = s.TxTones, Foreground = Theme.Text,
                                     Margin = new Thickness(0, 0, 0, 6) });
        rxTones = Add(new CheckBox { Content = "RX tones: emergency alert (the red banner still shows)", IsChecked = s.RxTones,
                                     Foreground = Theme.Text, Margin = new Thickness(0, 0, 0, 8) });
        Add(new TextBlock { Text = "Tone volume", Margin = new Thickness(0, 0, 0, 4) });
        toneVolume = Add(new Slider { Minimum = 0, Maximum = 1.5, Value = s.ToneVolume, IsMoveToPointEnabled = true, Margin = new Thickness(0, 0, 0, 10) });

        Heading("Priority talkgroups");
        Note("Mark a talkgroup as priority with the ☆ on its tile. While it is active, other talkgroups are:");
        duck = Choice("", Ducks.Select(d => d.Name), Ducks.OrderBy(d => Math.Abs(d.Db - s.PriorityDuckDb)).First().Name);

        Heading("Microphone and PTT");
        input = Choice("Microphone", new[] { "Default" }.Concat(WinMm.InputDevices()), s.InputDevice.Length > 0 ? s.InputDevice : "Default");
        pttKey = Field("Push-to-talk key (e.g. Space, F12, RightCtrl, Pause)", s.PttKey);

        Heading("Connection");
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
