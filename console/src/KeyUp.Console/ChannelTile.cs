// SPDX-License-Identifier: GPL-2.0-only
using System.Windows;
using System.Windows.Controls;
using System.Windows.Controls.Primitives;
using System.Windows.Input;
using System.Windows.Media;
using KeyUp.Console.Core;

namespace KeyUp.Console;

public enum TileState { Idle, Receiving, Transmitting, Requesting }

/// <summary>One talkgroup on the console: monitor, select, instant PTT, volume, who is talking, a receive
/// VU meter, a priority marker and which ear its audio plays in.</summary>
public sealed class ChannelTile : Border
{
    public Talkgroup Talkgroup { get; }
    public int Tg => Talkgroup.Id;
    private readonly TextBlock name, activity, idText;
    private readonly ToggleButton monitor, star;
    private readonly Button select, ptt, sideButton;
    private readonly Slider volume;
    private readonly VuMeter meter = new() { Margin = new Thickness(0, 0, 0, 5) };
    private bool selected, emergency;
    private Side side;
    private TileState state = TileState.Idle;
    private DateTime activitySince;
    private string activityText = "";

    public static readonly Brush PriorityBrush = Theme.Brush("#FFC83D");

    public event Action<ChannelTile, bool>? MonitorChanged;
    public event Action<ChannelTile>? SelectRequested;
    public event Action<ChannelTile>? PttPressed;
    public event Action<ChannelTile>? PttReleased;
    public event Action<ChannelTile, double>? VolumeChanged;
    public event Action<ChannelTile, bool>? PriorityChanged;
    public event Action<ChannelTile, Side>? SideChanged;

    public ChannelTile(Talkgroup tg, bool monitored, double vol, bool priority, Side side)
    {
        Talkgroup = tg;
        this.side = side;
        Width = 244;
        Height = 160;
        Margin = new Thickness(6);
        CornerRadius = new CornerRadius(8);
        BorderThickness = new Thickness(2);
        Padding = new Thickness(10, 6, 10, 8);
        Cursor = Cursors.Hand;

        var grid = new Grid();
        foreach (var h in new[] { GridLength.Auto, new GridLength(1, GridUnitType.Star), GridLength.Auto, GridLength.Auto, GridLength.Auto })
            grid.RowDefinitions.Add(new RowDefinition { Height = h });

        // top row: priority marker, name, side, TG number
        var top = new DockPanel();
        star = new ToggleButton
        {
            IsChecked = priority, Width = 24, Height = 22, Padding = new Thickness(0), Margin = new Thickness(-4, 0, 4, 0),
            FontSize = 15, Background = Brushes.Transparent, BorderThickness = new Thickness(0), Focusable = false,
            ToolTip = "Priority talkgroup: when it is active, other talkgroups get quieter so it is heard over them",
        };
        star.Checked += (_, _) => { PriorityChanged?.Invoke(this, true); Refresh(); };
        star.Unchecked += (_, _) => { PriorityChanged?.Invoke(this, false); Refresh(); };
        DockPanel.SetDock(star, Dock.Left);
        top.Children.Add(star);
        idText = new TextBlock { Text = $"TG {tg.Id}", Foreground = Theme.Muted, FontSize = 11, VerticalAlignment = VerticalAlignment.Center };
        DockPanel.SetDock(idText, Dock.Right);
        top.Children.Add(idText);
        sideButton = new Button
        {
            MinWidth = 34, Height = 20, Padding = new Thickness(4, 0, 4, 0), Margin = new Thickness(4, 0, 6, 0), FontSize = 10,
            Focusable = false, ToolTip = "Which ear this talkgroup plays in. Auto follows the Selected / Unselected setting.",
        };
        sideButton.Click += (_, _) =>
        {
            this.side = this.side switch { Side.Auto => Side.Left, Side.Left => Side.Right, Side.Right => Side.Both, _ => Side.Auto };
            SideChanged?.Invoke(this, this.side);
            Refresh();
        };
        DockPanel.SetDock(sideButton, Dock.Right);
        top.Children.Add(sideButton);
        name = new TextBlock { Text = tg.Name, FontSize = 16, FontWeight = FontWeights.SemiBold, TextTrimming = TextTrimming.CharacterEllipsis,
                               VerticalAlignment = VerticalAlignment.Center };
        top.Children.Add(name);
        grid.Children.Add(top);

        activity = new TextBlock { FontSize = 13, Margin = new Thickness(0, 4, 0, 4), TextTrimming = TextTrimming.CharacterEllipsis };
        Grid.SetRow(activity, 1);
        grid.Children.Add(activity);

        meter.ToolTip = "Receive level";
        Grid.SetRow(meter, 2);
        grid.Children.Add(meter);

        volume = new Slider { Minimum = 0, Maximum = 2, Value = vol, Height = 18, Margin = new Thickness(0, 0, 0, 4),
                              ToolTip = "Volume", IsMoveToPointEnabled = true };
        volume.ValueChanged += (_, e) => VolumeChanged?.Invoke(this, e.NewValue);
        Grid.SetRow(volume, 3);
        grid.Children.Add(volume);

        var buttons = new UniformGrid { Columns = 3 };
        monitor = new ToggleButton { Content = "Monitor", IsChecked = monitored, Margin = new Thickness(0, 0, 3, 0), Padding = new Thickness(2),
                                     ToolTip = "Hear this talkgroup" };
        monitor.Checked += (_, _) => { MonitorChanged?.Invoke(this, true); Refresh(); };
        monitor.Unchecked += (_, _) => { MonitorChanged?.Invoke(this, false); Refresh(); };
        select = new Button { Content = "Select", Margin = new Thickness(3, 0, 3, 0), Padding = new Thickness(2),
                              ToolTip = "Talk on this talkgroup with the main PTT / PTT key" };
        select.Click += (_, _) => SelectRequested?.Invoke(this);
        ptt = new Button { Content = "PTT", Margin = new Thickness(3, 0, 0, 0), Padding = new Thickness(2), FontWeight = FontWeights.Bold,
                           ToolTip = "Hold to talk on this talkgroup" };
        ptt.PreviewMouseLeftButtonDown += (_, e) => { e.Handled = true; ptt.CaptureMouse(); PttPressed?.Invoke(this); };
        ptt.PreviewMouseLeftButtonUp += (_, e) => { e.Handled = true; ptt.ReleaseMouseCapture(); PttReleased?.Invoke(this); };
        buttons.Children.Add(monitor);
        buttons.Children.Add(select);
        buttons.Children.Add(ptt);
        Grid.SetRow(buttons, 4);
        grid.Children.Add(buttons);
        Child = grid;
        MouseLeftButtonUp += (_, e) => { if (e.OriginalSource is not ButtonBase) SelectRequested?.Invoke(this); };
        Refresh();
    }

    public bool Monitored
    {
        get => monitor.IsChecked == true;
        set { if (monitor.IsChecked != value) monitor.IsChecked = value; }
    }

    public bool Priority
    {
        get => star.IsChecked == true;
        set { if (star.IsChecked != value) star.IsChecked = value; }
    }

    public Side Side { get => side; set { side = value; Refresh(); } }
    public bool Selected { get => selected; set { selected = value; Refresh(); } }
    public bool Emergency { get => emergency; set { emergency = value; Refresh(); } }

    /// <summary>Receive level (0..1 peak) for the meter.</summary>
    public void PushLevel(double level) => meter.Push(level);

    public void SetState(TileState s, string text = "")
    {
        if (s != state || text != activityText) activitySince = DateTime.Now;
        state = s;
        activityText = text;
        if (s == TileState.Idle) meter.Reset();
        Refresh();
    }

    public void ShowLastHeard(string text)
    {
        if (state == TileState.Idle) { activityText = text; activitySince = DateTime.MinValue; Refresh(); }
    }

    public void Tick() { if (state != TileState.Idle) Refresh(); }

    private void Refresh()
    {
        bool prio = Priority;
        Background = state switch
        {
            TileState.Receiving => Theme.Rx,
            TileState.Transmitting => Theme.Tx,
            TileState.Requesting => Theme.Warn,
            _ => Monitored ? Theme.Monitored : Theme.Panel,
        };
        BorderBrush = emergency ? Theme.Emergency : selected ? Theme.Accent : prio ? PriorityBrush : Theme.Border;
        BorderThickness = new Thickness(emergency || selected ? 3 : prio ? 2 : 1);
        star.Content = prio ? "★" : "☆";
        star.Foreground = prio ? PriorityBrush : Theme.Muted;
        sideButton.Content = side switch { Side.Left => "L", Side.Right => "R", Side.Both => "L+R", _ => "Auto" };
        sideButton.Foreground = side == Side.Auto ? Theme.Muted : Theme.Text;
        name.Opacity = Monitored || state != TileState.Idle ? 1.0 : 0.6;
        string secs = activitySince == DateTime.MinValue ? "" : $"  {(int)(DateTime.Now - activitySince).TotalSeconds}s";
        activity.Text = state switch
        {
            TileState.Receiving => (prio ? "★ PRIORITY  " : "◉ ") + activityText + secs,
            TileState.Transmitting => "● TX" + secs,
            TileState.Requesting => "requesting…",
            _ => activityText.Length > 0 ? activityText : (Monitored ? "monitoring" : "not monitored"),
        };
        activity.Foreground = state == TileState.Idle ? Theme.Muted : Brushes.White;
        if (emergency) activity.Text = "⚠ EMERGENCY  " + activity.Text;
    }
}
