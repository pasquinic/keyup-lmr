// SPDX-License-Identifier: GPL-2.0-only
// Headless check of the KeyUp Dispatch Console core against a live KeyUp server.
//
//   HeadlessCheck <server> <activation-code>
//
// Activates like the console's "Connect to KeyUp", checks the settings store and the WireGuard
// configuration blob, connects over the public URL, then follows commands on stdin (one JSON line
// out per command):  monitor <tg,...> | talk <tg> <seconds> | rx | inhibit <unit> <0|1> | quit
using System.Text.Json;
using KeyUp.Console.Core;

static void Emit(object o) { Console.Out.WriteLine(JsonSerializer.Serialize(o)); Console.Out.Flush(); }

// G.711 must match the server byte for byte (reference values from lmrcommon/audio.py)
short[] samples = { 0, 1, -1, 100, -100, 1000, -1000, 8000, -8000, 32767, -32768, 12345, -12345 };
byte[] expect = { 255, 255, 127, 242, 114, 206, 78, 160, 32, 128, 0, 151, 23 };
bool g711 = G711.Encode(samples).SequenceEqual(expect) && G711.Decode(G711.Encode(new short[] { 12345 }))[0] is > 11800 and < 12900;

var act = await Activation.ActivateAsync(args[0], args[1], "headless-check");
var settingsPath = Path.Combine(Path.GetTempPath(), $"keyup-console-{Environment.ProcessId}.json");
var st = new ConsoleSettings();
st.Apply(act, Activation.NormalizeServer(args[0]));
st.Save(settingsPath);
var reloaded = ConsoleSettings.Load(settingsPath);
bool storeOk = reloaded.Token == act.Token && reloaded.TunnelPrivateKey == act.WireGuard.PrivateKey
               && !File.ReadAllText(settingsPath).Contains(act.Token);
var spec = reloaded.PlainTunnel();
spec.Endpoint = "127.0.0.1:" + spec.Endpoint.Split(':').Last();   // the lab's endpoint name is a placeholder
var blob = WireGuardTunnel.BuildConfig(spec);
bool blobOk = blob.Length == WireGuardNt.InterfaceSize + WireGuardNt.PeerSize + WireGuardNt.AllowedIpSize
              && blob.AsSpan(6, 32).SequenceEqual(Convert.FromBase64String(act.WireGuard.PrivateKey))
              && blob.AsSpan(WireGuardNt.InterfaceSize + 8, 32).SequenceEqual(Convert.FromBase64String(act.WireGuard.ServerPublicKey));
var plan = ConnectionPlan.Order(reloaded, tunnelUp: true);
Emit(new { @event = "activated", console = act.ConsoleId, g711, store_ok = storeOk, blob_ok = blobOk, blob_bytes = blob.Length,
           plan = plan.Select(p => p.Transport).ToArray(), tunnel_url = reloaded.TunnelUrl });

await using var client = new ConsoleClient();
var mixer = new Mixer();
var calls = new List<object>();
var rxFrames = new Dictionary<int, int>();
var events = new List<string>();
client.CallStarted += c => { lock (calls) calls.Add(new { c.Tg, c.Src, c.Alias, c.CallId, c.Emergency }); };
client.Audio += (tg, src, call, ulaw) => { lock (rxFrames) rxFrames[tg] = rxFrames.GetValueOrDefault(tg) + 1; mixer.Add(call, tg, ulaw); };
client.Granted += tg => { lock (events) events.Add($"grant:{tg}"); };
client.Denied += (tg, r) => { lock (events) events.Add($"deny:{tg}:{r}"); };
client.InhibitResult += (u, on, ok, r) => { lock (events) events.Add($"inhibit:{u}:{on}:{ok}"); };
client.AuthFailed += r => { lock (events) events.Add($"auth_fail:{r}"); };

var publicUrl = plan.First(p => p.Transport == "https").Url;
var refused = await client.ConnectAsync(publicUrl, reloaded.Token, "https", "headless", TimeSpan.FromSeconds(10));
var prof = client.Profile;
Emit(new { @event = "connected", refused, zones = prof?.Zones.Select(z => z.Name).ToArray(),
           talkgroups = prof?.Talkgroups.Keys.OrderBy(x => x).ToArray(), rights = prof?.Rights });

string? line;
while ((line = Console.In.ReadLine()) != null)
{
    var cmd = line.Trim().Split(' ', StringSplitOptions.RemoveEmptyEntries);
    if (cmd.Length == 0) continue;
    if (cmd[0] == "quit") break;
    try
    {
        switch (cmd[0])
        {
            case "monitor":
                await client.MonitorAsync(cmd[1].Split(',').Select(int.Parse));
                await Task.Delay(300);
                Emit(new { @event = "monitor" });
                break;
            case "rx":
                // pull what the mixer has, like the audio output would, and report the level
                double peak = 0; int pulled = 0;
                for (int i = 0; i < 400 && mixer.ActiveStreams > 0; i++)
                {
                    var f = mixer.Pull(); pulled++;
                    peak = Math.Max(peak, f.Max(s => Math.Abs((int)s)));
                }
                lock (rxFrames) lock (calls)
                    Emit(new { @event = "rx", frames = rxFrames.ToDictionary(k => k.Key.ToString(), v => v.Value), calls, peak, pulled });
                lock (rxFrames) rxFrames.Clear();
                lock (calls) calls.Clear();
                break;
            case "talk":
            {
                int tg = int.Parse(cmd[1]);
                double secs = double.Parse(cmd[2], System.Globalization.CultureInfo.InvariantCulture);
                lock (events) events.Clear();
                await client.PttOnAsync(tg);
                for (int w = 0; w < 50 && events.Count == 0; w++) await Task.Delay(50);
                string res; lock (events) res = events.FirstOrDefault() ?? "timeout";
                int sent = 0;
                if (res.StartsWith("grant"))
                {
                    var t0 = DateTime.UtcNow;
                    for (int i = 0; i < secs * 50; i++)
                    {
                        var pcm = new short[160];
                        for (int k = 0; k < 160; k++) pcm[k] = (short)(9000 * Math.Sin(2 * Math.PI * 700 * (i * 160 + k) / 8000.0));
                        await client.SendAudioAsync(G711.Encode(pcm));
                        sent++;
                        var due = t0.AddMilliseconds((i + 1) * 20) - DateTime.UtcNow;
                        if (due > TimeSpan.Zero) await Task.Delay(due);
                    }
                    await client.PttOffAsync();
                }
                Emit(new { @event = "talk", result = res, sent });
                break;
            }
            case "inhibit":
                lock (events) events.Clear();
                await client.InhibitAsync(int.Parse(cmd[1]), cmd[2] == "1");
                for (int w = 0; w < 40 && events.Count == 0; w++) await Task.Delay(50);
                lock (events) Emit(new { @event = "inhibit", result = events.FirstOrDefault() ?? "timeout" });
                break;
            case "events":
                lock (events) Emit(new { @event = "events", events });
                break;
        }
    }
    catch (Exception e) { Emit(new { @event = "error", message = e.Message }); }
}
File.Delete(settingsPath);
