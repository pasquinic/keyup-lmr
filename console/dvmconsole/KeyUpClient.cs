// SPDX-License-Identifier: AGPL-3.0-only
/**
* KeyUp Dispatch Console -- fork of the Digital Voice Modem Desktop Dispatch Console
* AGPLv3 Open Source. Use is subject to license terms.
*
* KeyUp provisioning: activate this console against a KeyUp central server with a one-time code,
* receive its codeplug, radio aliases and WireGuard tunnel, and re-sync them later.
*
*   Copyright (C) 2026 KeyUp contributors
*
*/

using System;
using System.IO;
using System.Net;
using System.Net.Http;
using System.Net.Http.Headers;
using System.Threading.Tasks;
using System.Text;
using System.Text.Json;

namespace dvmconsole
{
    /// <summary>
    /// Saved link between this PC and a KeyUp server (stored next to the console's settings).
    /// </summary>
    public class KeyUpProfile
    {
        public string Server { get; set; }
        public string Token { get; set; }
        public int ConsoleId { get; set; }
        public string Name { get; set; }
        public string Version { get; set; }
        public string TunnelIp { get; set; }
        public string Fne { get; set; }
    } // public class KeyUpProfile

    /// <summary>
    /// The KeyUp server refused this console (revoked, disabled or a bad code).
    /// </summary>
    public class KeyUpRefusedException : Exception
    {
        public KeyUpRefusedException(string message) : base(message) { }
    } // public class KeyUpRefusedException

    /// <summary>
    /// Talks to the KeyUp central server and keeps the console's KeyUp files up to date.
    /// </summary>
    public static class KeyUpClient
    {
        private static readonly HttpClient http = new HttpClient { Timeout = TimeSpan.FromSeconds(15) };

        /// <summary>Folder that holds the KeyUp profile, codeplug, aliases and tunnel file.</summary>
        public static string ProfileDir { get; set; } = null;

        public static string ProfilePath => Path.Combine(Dir(), "keyup.json");
        public static string CodeplugPath => Path.Combine(Dir(), "codeplug.yml");
        public static string AliasPath => Path.Combine(Dir(), "alias.yml");
        public static string WireGuardPath => Path.Combine(Dir(), "keyup-wireguard.conf");

        private static string Dir()
        {
            string dir = ProfileDir ?? Path.Combine(SettingsManager.UserAppDataPath, "KeyUp");
            Directory.CreateDirectory(dir);
            return dir;
        }

        /// <summary>
        /// Turn what the user typed ("lmr.example.com", "https://lmr.example.com/") into a base URL.
        /// </summary>
        public static string NormalizeServer(string server)
        {
            string s = (server ?? string.Empty).Trim().TrimEnd('/');
            if (s.Length == 0)
                throw new ArgumentException("Enter the KeyUp server address.");
            if (!s.StartsWith("http://", StringComparison.OrdinalIgnoreCase) && !s.StartsWith("https://", StringComparison.OrdinalIgnoreCase))
                s = "https://" + s;
            return s;
        }

        public static KeyUpProfile LoadProfile()
        {
            try
            {
                if (!File.Exists(ProfilePath))
                    return null;
                KeyUpProfile p = JsonSerializer.Deserialize<KeyUpProfile>(File.ReadAllText(ProfilePath));
                return (p != null && !string.IsNullOrEmpty(p.Server) && !string.IsNullOrEmpty(p.Token)) ? p : null;
            }
            catch (Exception)
            {
                return null;
            }
        }

        public static void SaveProfile(KeyUpProfile profile)
        {
            File.WriteAllText(ProfilePath, JsonSerializer.Serialize(profile, new JsonSerializerOptions { WriteIndented = true }));
        }

        public static void ForgetProfile()
        {
            if (File.Exists(ProfilePath))
                File.Delete(ProfilePath);
        }

        /// <summary>
        /// Trade a one-time activation code for this console's token, codeplug, aliases and WireGuard tunnel.
        /// </summary>
        public static async Task<KeyUpProfile> ActivateAsync(string server, string code, string appVersion)
        {
            string baseUrl = NormalizeServer(server);
            string body = JsonSerializer.Serialize(new { code = (code ?? string.Empty).Trim(), machine = Environment.MachineName, app_version = appVersion ?? string.Empty });
            using HttpResponseMessage resp = await http.PostAsync(baseUrl + "/api/console/activate",
                new StringContent(body, Encoding.UTF8, "application/json")).ConfigureAwait(false);
            string text = await resp.Content.ReadAsStringAsync().ConfigureAwait(false);
            if (resp.StatusCode == HttpStatusCode.Forbidden)
                throw new KeyUpRefusedException(Detail(text, "The activation code is invalid or has expired. Issue a new one in the KeyUp web UI."));
            if (!resp.IsSuccessStatusCode)
                throw new Exception($"KeyUp server answered {(int)resp.StatusCode}: {Detail(text, resp.ReasonPhrase)}");

            using JsonDocument doc = JsonDocument.Parse(text);
            JsonElement root = doc.RootElement;
            KeyUpProfile profile = new KeyUpProfile
            {
                Server = baseUrl,
                Token = root.GetProperty("token").GetString(),
                ConsoleId = root.GetProperty("console_id").GetInt32(),
                Name = Str(root, "name"),
                TunnelIp = Str(root, "tunnel_ip"),
                Fne = Str(root, "fne"),
            };
            File.WriteAllText(WireGuardPath, Str(root, "wireguard_conf"));
            profile.Version = WriteCodeplug(Str(root, "codeplug"), Str(root, "alias"), Str(root, "version"));
            SaveProfile(profile);
            return profile;
        }

        /// <summary>
        /// Fetch the current codeplug and aliases. Returns true when they changed since the last sync.
        /// </summary>
        public static async Task<bool> SyncAsync(KeyUpProfile profile)
        {
            using HttpRequestMessage req = new HttpRequestMessage(HttpMethod.Get, profile.Server + "/api/console/codeplug");
            req.Headers.Authorization = new AuthenticationHeaderValue("Bearer", profile.Token);
            using HttpResponseMessage resp = await http.SendAsync(req).ConfigureAwait(false);
            string text = await resp.Content.ReadAsStringAsync().ConfigureAwait(false);
            if (resp.StatusCode == HttpStatusCode.Forbidden)
                throw new KeyUpRefusedException(Detail(text, "This console was revoked or disabled on the KeyUp server."));
            if (!resp.IsSuccessStatusCode)
                throw new Exception($"KeyUp server answered {(int)resp.StatusCode}: {Detail(text, resp.ReasonPhrase)}");

            using JsonDocument doc = JsonDocument.Parse(text);
            JsonElement root = doc.RootElement;
            string version = Str(root, "version");
            bool changed = version != profile.Version || !File.Exists(CodeplugPath);
            profile.Version = WriteCodeplug(Str(root, "codeplug"), Str(root, "alias"), version);
            profile.Name = Str(root, "name") ?? profile.Name;
            SaveProfile(profile);
            return changed;
        }

        /// <summary>
        /// Write the codeplug and alias files, pointing the codeplug at the alias file's full path.
        /// </summary>
        public static string WriteCodeplug(string codeplug, string alias, string version)
        {
            if (string.IsNullOrEmpty(codeplug))
                throw new Exception("The KeyUp server sent an empty codeplug.");
            File.WriteAllText(AliasPath, alias ?? "[]\n");
            // single-quoted YAML keeps Windows backslashes literal; a quote is escaped by doubling it
            string aliasLine = "aliasPath: '" + AliasPath.Replace("'", "''") + "'";
            StringBuilder sb = new StringBuilder();
            foreach (string line in codeplug.Replace("\r\n", "\n").Split('\n'))
            {
                string trimmed = line.TrimStart();
                if (trimmed.StartsWith("aliasPath:"))
                    sb.Append(line.Substring(0, line.Length - trimmed.Length)).Append(aliasLine).Append('\n');
                else
                    sb.Append(line).Append('\n');
            }
            File.WriteAllText(CodeplugPath, sb.ToString().TrimEnd('\n') + "\n");
            return version;
        }

        private static string Str(JsonElement root, string name)
        {
            return root.TryGetProperty(name, out JsonElement v) && v.ValueKind == JsonValueKind.String ? v.GetString() : null;
        }

        private static string Detail(string body, string fallback)
        {
            try
            {
                using JsonDocument doc = JsonDocument.Parse(body);
                if (doc.RootElement.TryGetProperty("detail", out JsonElement d) && d.ValueKind == JsonValueKind.String)
                    return d.GetString();
            }
            catch (Exception) { /* not JSON */ }
            return fallback;
        }
    } // public static class KeyUpClient
} // namespace dvmconsole
