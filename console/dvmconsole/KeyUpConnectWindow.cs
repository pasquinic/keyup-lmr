// SPDX-License-Identifier: AGPL-3.0-only
/**
* KeyUp Dispatch Console -- fork of the Digital Voice Modem Desktop Dispatch Console
* AGPLv3 Open Source. Use is subject to license terms.
*
*   Copyright (C) 2026 KeyUp contributors
*
*/

using System.Windows;
using System.Windows.Controls;
using Button = System.Windows.Controls.Button;
using HorizontalAlignment = System.Windows.HorizontalAlignment;
using MessageBox = System.Windows.MessageBox;
using Orientation = System.Windows.Controls.Orientation;
using TextBox = System.Windows.Controls.TextBox;

namespace dvmconsole
{
    /// <summary>
    /// Asks for the KeyUp server address and the one-time activation code.
    /// </summary>
    public class KeyUpConnectWindow : Window
    {
        private readonly TextBox serverBox;
        private readonly TextBox codeBox;

        public string Server => serverBox.Text.Trim();
        public string Code => codeBox.Text.Trim();

        public KeyUpConnectWindow(string server)
        {
            Title = "Connect to KeyUp server";
            Width = 460;
            SizeToContent = SizeToContent.Height;
            ResizeMode = ResizeMode.NoResize;
            WindowStartupLocation = WindowStartupLocation.CenterOwner;

            StackPanel panel = new StackPanel { Margin = new Thickness(16) };
            panel.Children.Add(new TextBlock
            {
                Text = "In the KeyUp web UI open Dispatch Consoles, press Activate on this console, and enter the server and code shown there.",
                TextWrapping = TextWrapping.Wrap,
                Margin = new Thickness(0, 0, 0, 12)
            });

            panel.Children.Add(new TextBlock { Text = "Server", Margin = new Thickness(0, 0, 0, 4) });
            serverBox = new TextBox { Text = server ?? string.Empty, Margin = new Thickness(0, 0, 0, 10) };
            panel.Children.Add(serverBox);

            panel.Children.Add(new TextBlock { Text = "Activation code", Margin = new Thickness(0, 0, 0, 4) });
            codeBox = new TextBox { Margin = new Thickness(0, 0, 0, 16), CharacterCasing = CharacterCasing.Upper };
            panel.Children.Add(codeBox);

            StackPanel buttons = new StackPanel { Orientation = Orientation.Horizontal, HorizontalAlignment = HorizontalAlignment.Right };
            Button ok = new Button { Content = "Connect", IsDefault = true, MinWidth = 90, Margin = new Thickness(0, 0, 8, 0) };
            ok.Click += (s, e) =>
            {
                if (Server.Length == 0 || Code.Length == 0)
                {
                    MessageBox.Show(this, "Enter both the server and the activation code.", Title, MessageBoxButton.OK, MessageBoxImage.Warning);
                    return;
                }
                DialogResult = true;
            };
            Button cancel = new Button { Content = "Cancel", IsCancel = true, MinWidth = 90 };
            buttons.Children.Add(ok);
            buttons.Children.Add(cancel);
            panel.Children.Add(buttons);

            Content = panel;
            Loaded += (s, e) => (serverBox.Text.Length == 0 ? serverBox : codeBox).Focus();
        }
    } // public class KeyUpConnectWindow
} // namespace dvmconsole
