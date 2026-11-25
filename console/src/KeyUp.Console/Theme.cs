// SPDX-License-Identifier: GPL-2.0-only
using System.Windows;
using System.Windows.Controls;
using System.Windows.Media;

namespace KeyUp.Console;

/// <summary>Dark dispatch theme: colours and default control styles (no XAML).</summary>
public static class Theme
{
    public static readonly SolidColorBrush Bg = Brush("#0F1419"), Panel = Brush("#18202A"), Panel2 = Brush("#202B38"),
        Border = Brush("#2C3A4A"), Text = Brush("#E6EDF3"), Muted = Brush("#8B9BAD"), Accent = Brush("#3D9BFF"),
        Rx = Brush("#1F9D55"), Tx = Brush("#D64545"), Warn = Brush("#E0A030"), Emergency = Brush("#FF3B30"),
        Monitored = Brush("#1E2A38");

    public static SolidColorBrush Brush(string hex)
    {
        var b = (SolidColorBrush)new BrushConverter().ConvertFromString(hex)!;
        b.Freeze();
        return b;
    }

    public static void Apply(Application app)
    {
        var r = app.Resources;
        r[typeof(Window)] = WindowStyle();
        r[typeof(TextBlock)] = Style<TextBlock>((TextBlock.ForegroundProperty, Text), (TextBlock.FontFamilyProperty, new FontFamily("Segoe UI")));
        r[typeof(Button)] = ButtonStyle();
        r[typeof(TextBox)] = Style<TextBox>((Control.BackgroundProperty, Panel2), (Control.ForegroundProperty, Text),
            (Control.BorderBrushProperty, Border), (Control.PaddingProperty, new Thickness(6, 4, 6, 4)), (TextBox.CaretBrushProperty, Text));
        r[typeof(ComboBox)] = Style<ComboBox>((Control.PaddingProperty, new Thickness(6, 4, 6, 4)));
        r[typeof(ListBox)] = Style<ListBox>((Control.BackgroundProperty, Panel), (Control.ForegroundProperty, Text),
            (Control.BorderBrushProperty, Border));
        r[typeof(TabItem)] = Style<TabItem>((Control.FontSizeProperty, 14.0), (Control.PaddingProperty, new Thickness(14, 6, 14, 6)));
    }

    private static Style WindowStyle() => Style<Window>((Control.BackgroundProperty, Bg), (Control.ForegroundProperty, Text),
        (Control.FontFamilyProperty, new FontFamily("Segoe UI")), (Control.FontSizeProperty, 13.0));

    private static Style ButtonStyle() => Style<Button>((Control.BackgroundProperty, Panel2), (Control.ForegroundProperty, Text),
        (Control.BorderBrushProperty, Border), (Control.PaddingProperty, new Thickness(10, 5, 10, 5)), (Control.MarginProperty, new Thickness(3)));

    public static Style Style<T>(params (DependencyProperty P, object V)[] setters)
    {
        var s = new Style(typeof(T));
        foreach (var (p, v) in setters) s.Setters.Add(new Setter(p, v));
        return s;
    }
}
