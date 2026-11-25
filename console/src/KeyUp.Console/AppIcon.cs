// SPDX-License-Identifier: GPL-2.0-only
using System.Windows.Controls;
using System.Windows.Media;
using System.Windows.Media.Imaging;

namespace KeyUp.Console;

/// <summary>The KeyUp logo, embedded as WPF resources (Assets/).</summary>
public static class AppIcon
{
    public static ImageSource? WindowIcon()
    {
        try { return BitmapFrame.Create(new Uri("pack://application:,,,/Assets/keyup.ico", UriKind.Absolute)); }
        catch (Exception) { return null; }
    }

    public static Image? Logo(double size)
    {
        try
        {
            var src = new BitmapImage(new Uri("pack://application:,,,/Assets/keyup-96.png", UriKind.Absolute));
            return new Image { Source = src, Width = size, Height = size, VerticalAlignment = System.Windows.VerticalAlignment.Center };
        }
        catch (Exception) { return null; }
    }
}
