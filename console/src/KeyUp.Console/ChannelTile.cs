// SPDX-License-Identifier: GPL-2.0-only
using System.Windows;
using System.Windows.Controls;
using System.Windows.Controls.Primitives;
using System.Windows.Input;
using System.Windows.Media;
using KeyUp.Console.Core;

namespace KeyUp.Console;

public enum TileState { Idle, Receiving, Transmitting, Requesting }

/// <summary>One talkgroup on the console: monitor, select, instant PTT, volume, who is talking.</summary>
public sealed class ChannelTile : Border
{
    public Talkgroup Talkgroup { get; }
    public int Tg => Talkgroup.Id;
    private readonly TextBlock name, activity, idText;
    private readonly ToggleButton monitor;
    private readonly Button select, ptt;
    private readonly Slider volume;
    private bool selected, emergency;
    private TileState state = TileState.Idle;
    private DateTime activitySince;
    private string activityText = "";

    public event Action<ChannelTile, bool>? MonitorChanged;
    public event Action<ChannelTile>? SelectRequested;
    public event Action<ChannelTile>? PttPressed;
    public event Action<ChannelTile>? PttReleased;
    public event Action<ChannelTile, double>? VolumeChanged;

    public ChannelTile(Talkgroup tg, bool monitored, double vol)
    {
        Talkgroup = tg;
        Width = 236;
        Height = 132;
        Margin = new Thickness(6);
        CornerRadius = new CornerRadius(8);
        BorderThickness = new Thickness(2);
        Padding = new Thickness(10, 8, 10, 8);
        Cursor = Cursors.Hand;

        var grid = new Grid();
        grid.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
        grid.RowDefinitions.Add(new RowDefinition { Height = new GridLength(1, GridUnitType.Star) });
        grid.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
        grid.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });

        var top = new DockPanel();
        idText = new TextBlock { Text = $"TG {tg.Id}", Foreground = Theme.Muted, FontSize = 11, VerticalAlignment = VerticalAlignment.Center };
        DockPanel.SetDock(idText, Dock.Right);
        top.Children.Add(idText);
        name = new TextBlock { Text = tg.Name, FontSize = 16, FontWeight = FontWeights.SemiBold, TextTrimming = TextTrimming.CharacterEllipsis };
        top.Children.Add(name);
        grid.Children.Add(top);

        activity = new TextBlock { FontSize = 13, Margin = new Thickness(0, 4, 0, 4), TextTrimming = TextTrimming.CharacterEllipsis };
        Grid.SetRow(activity, 1);
        grid.Children.Add(activity);

        volume = new Slider { Minimum = 0, Maximum = 2, Value = vol, Height = 18, Margin = new Thickness(0, 0, 0, 4),
                              ToolTip = "Volume", IsMoveToPointEnabled = true };
        volume.ValueChanged += (_, e) => VolumeChanged?.Invoke(this, e.NewValue);
        Grid.SetRow(volume, 2);
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
        Grid.SetRow(buttons, 3);
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

    public bool Selected { get => selected; set { selected = value; Refresh(); } }
    public bool Emergency { get => emergency; set { emergency = value; Refresh(); } }

    public void SetState(TileState s, string text = "")
    {
        if (s != state || text != activityText) activitySince = DateTime.Now;
        state = s;
        activityText = text;
        Refresh();
    }

    public void ShowLastHeard(string text)
    {
        if (state == TileState.Idle) { activityText = text; activitySince = DateTime.MinValue; Refresh(); }
    }

    public void Tick() { if (state != TileState.Idle) Refresh(); }

    private void Refresh()
    {
        Background = state switch
        {
            TileState.Receiving => Theme.Rx,
            TileState.Transmitting => Theme.Tx,
            TileState.Requesting => Theme.Warn,
            _ => Monitored ? Theme.Monitored : Theme.Panel,
        };
        BorderBrush = emergency ? Theme.Emergency : selected ? Theme.Accent : Theme.Border;
        BorderThickness = new Thickness(emergency || selected ? 3 : 1);
        name.Opacity = Monitored || state != TileState.Idle ? 1.0 : 0.6;
        string secs = activitySince == DateTime.MinValue ? "" : $"  {(int)(DateTime.Now - activitySince).TotalSeconds}s";
        activity.Text = state switch
        {
            TileState.Receiving => "◉ " + activityText + secs,
            TileState.Transmitting => "● TX" + secs,
            TileState.Requesting => "requesting…",
            _ => activityText.Length > 0 ? activityText : (Monitored ? "monitoring" : "not monitored"),
        };
        activity.Foreground = state == TileState.Idle ? Theme.Muted : Brushes.White;
        if (emergency) activity.Text = "⚠ EMERGENCY  " + activity.Text;
    }
}
