// SPDX-License-Identifier: AGPL-3.0-only
/**
* KeyUp Dispatch Console -- fork of the Digital Voice Modem Desktop Dispatch Console
* AGPLv3 Open Source. Use is subject to license terms.
*
* KeyUp menu commands and start-up sync. Kept in its own partial class file so the upstream
* MainWindow sources change as little as possible.
*
*   Copyright (C) 2026 KeyUp contributors
*
*/

using System.Diagnostics;
using System.Reflection;
using System.Windows;
using MessageBox = System.Windows.MessageBox;

namespace dvmconsole
{
    public partial class MainWindow : Window
    {
        private const string KEYUP_TITLE = "KeyUp Dispatch Console";

        private static string KeyUpAppVersion()
        {
            Version v = Assembly.GetExecutingAssembly().GetName().Version;
            return v != null ? $"{v.Major}.{v.Minor}.{v.Build}" : "1.0";
        }

        /// <summary>
        /// Before the last codeplug loads: if this PC is linked to KeyUp, fetch the latest codeplug
        /// (a few seconds at most; the cached copy is used when the server can't be reached).
        /// </summary>
        private void KeyUpStartupSync()
        {
            KeyUpProfile profile = KeyUpClient.LoadProfile();
            menuKeyUpSync.IsEnabled = profile != null;
            if (profile == null)
                return;

            UpdateKeyUpTitle(profile);
            try
            {
                Task<bool> sync = Task.Run(() => KeyUpClient.SyncAsync(profile));
                if (!sync.Wait(TimeSpan.FromSeconds(6)))
                    Log.WriteWarning("KeyUp: server did not answer in time, using the cached codeplug");
            }
            catch (AggregateException ex) when (ex.InnerException is KeyUpRefusedException)
            {
                MessageBox.Show(ex.InnerException.Message + "\n\nUse File > Connect to KeyUp server to activate this console again.",
                    KEYUP_TITLE, MessageBoxButton.OK, MessageBoxImage.Warning);
            }
            catch (Exception ex)
            {
                Log.WriteWarning($"KeyUp: sync failed, using the cached codeplug ({ex.GetBaseException().Message})");
            }

            if (File.Exists(KeyUpClient.CodeplugPath))
            {
                settingsManager.LastCodeplugPath = KeyUpClient.CodeplugPath;
                noSaveSettingsOnClose = false;
                settingsManager.SaveSettings();
            }
        }

        private void UpdateKeyUpTitle(KeyUpProfile profile)
        {
            Title = profile != null ? $"{KEYUP_TITLE} - {profile.Name} ({profile.Server})" : KEYUP_TITLE;
        }

        /// <summary>
        /// File > Connect to KeyUp server...
        /// </summary>
        private async void KeyUpConnect_Click(object sender, RoutedEventArgs e)
        {
            KeyUpProfile existing = KeyUpClient.LoadProfile();
            KeyUpConnectWindow dlg = new KeyUpConnectWindow(existing?.Server) { Owner = this };
            if (dlg.ShowDialog() != true)
                return;

            Cursor = System.Windows.Input.Cursors.Wait;
            try
            {
                KeyUpProfile profile = await KeyUpClient.ActivateAsync(dlg.Server, dlg.Code, KeyUpAppVersion());
                LoadCodeplug(KeyUpClient.CodeplugPath);
                settingsManager.LastCodeplugPath = KeyUpClient.CodeplugPath;
                noSaveSettingsOnClose = false;
                settingsManager.SaveSettings();
                menuKeyUpSync.IsEnabled = true;
                UpdateKeyUpTitle(profile);

                MessageBox.Show(this,
                    $"This PC is now the KeyUp console \"{profile.Name}\".\n\n" +
                    "One more step: the console reaches the FNE through a WireGuard tunnel.\n" +
                    "1. Install WireGuard for Windows (wireguard.com/install) if needed.\n" +
                    "2. In WireGuard choose Import tunnel(s) from file and pick keyup-wireguard.conf (the folder opens next).\n" +
                    "3. Press Activate in WireGuard.\n\n" +
                    $"The console then connects to the FNE at {profile.Fne}.",
                    KEYUP_TITLE, MessageBoxButton.OK, MessageBoxImage.Information);
                Process.Start(new ProcessStartInfo("explorer.exe", $"/select,\"{KeyUpClient.WireGuardPath}\"") { UseShellExecute = true });
            }
            catch (Exception ex)
            {
                MessageBox.Show(this, $"Could not activate this console.\n\n{ex.GetBaseException().Message}", KEYUP_TITLE,
                    MessageBoxButton.OK, MessageBoxImage.Error);
            }
            finally
            {
                Cursor = null;
            }
        }

        /// <summary>
        /// File > Sync with KeyUp
        /// </summary>
        private async void KeyUpSync_Click(object sender, RoutedEventArgs e)
        {
            KeyUpProfile profile = KeyUpClient.LoadProfile();
            if (profile == null)
            {
                MessageBox.Show(this, "This console is not linked to a KeyUp server yet. Use File > Connect to KeyUp server.", KEYUP_TITLE,
                    MessageBoxButton.OK, MessageBoxImage.Information);
                return;
            }

            Cursor = System.Windows.Input.Cursors.Wait;
            try
            {
                bool changed = await KeyUpClient.SyncAsync(profile);
                UpdateKeyUpTitle(profile);
                if (changed)
                {
                    LoadCodeplug(KeyUpClient.CodeplugPath);
                    settingsManager.LastCodeplugPath = KeyUpClient.CodeplugPath;
                    noSaveSettingsOnClose = false;
                    settingsManager.SaveSettings();
                    MessageBox.Show(this, "Talkgroups and radio aliases were updated from KeyUp.", KEYUP_TITLE, MessageBoxButton.OK, MessageBoxImage.Information);
                }
                else
                    MessageBox.Show(this, "The console is already up to date.", KEYUP_TITLE, MessageBoxButton.OK, MessageBoxImage.Information);
            }
            catch (Exception ex)
            {
                MessageBox.Show(this, $"Could not sync with KeyUp.\n\n{ex.GetBaseException().Message}", KEYUP_TITLE,
                    MessageBoxButton.OK, MessageBoxImage.Error);
            }
            finally
            {
                Cursor = null;
            }
        }

        /// <summary>
        /// File > Open KeyUp folder
        /// </summary>
        private void KeyUpOpenFolder_Click(object sender, RoutedEventArgs e)
        {
            Process.Start(new ProcessStartInfo("explorer.exe", $"\"{Path.GetDirectoryName(KeyUpClient.ProfilePath)}\"") { UseShellExecute = true });
        }
    } // public partial class MainWindow : Window
} // namespace dvmconsole
