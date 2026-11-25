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

// Mixer routing, side overrides, priority ducking and meters (no server needed)
static byte[] Tone(double hz, int frames, double amp = 9000)
{
    var pcm = new short[160 * frames];
    for (int k = 0; k < pcm.Length; k++) pcm[k] = (short)(amp * Math.Sin(2 * Math.PI * hz * k / 8000.0));
    return G711.Encode(pcm);
}
static (double L, double R) Peaks(short[] st)
{
    int l = 0, r = 0;
    for (int i = 0; i < st.Length; i += 2) { l = Math.Max(l, Math.Abs((int)st[i])); r = Math.Max(r, Math.Abs((int)st[i + 1])); }
    return (l / 32768.0, r / 32768.0);
}
var mix = new Dictionary<string, bool>();
{
    var m = new Mixer { SelectedTg = 100, SelectRoute = new AudioRoute(0, Side.Left), UnselectRoute = new AudioRoute(1, Side.Right), DuckDb = 12 };
    m.Add(1, 100, Tone(500, 20));
    m.Add(2, 300, Tone(800, 20));
    short[][] f = m.PullStereo();
    for (int i = 0; i < 4; i++) f = m.PullStereo();          // past the jitter-buffer prime
    var (s0, s1) = (Peaks(f[0]), Peaks(f[1]));
    mix["select_left_on_output0"] = s0.L > 0.1 && s0.R == 0;
    mix["unselect_right_on_output1"] = s1.R > 0.1 && s1.L == 0;
    m.SetSide(300, Side.Left);
    f = m.PullStereo();
    mix["per_talkgroup_side"] = Peaks(f[1]).L > 0.1 && Peaks(f[1]).R == 0;
    double plain = Peaks(f[0]).L;
    m.SetPriority(300, true);
    f = m.PullStereo();
    double ducked = Peaks(f[0]).L;
    mix["priority_ducks_others"] = m.PriorityActive && Math.Abs(20 * Math.Log10(ducked / plain) + 12) < 1.5
                                   && Peaks(f[1]).L > 0.1;
    mix["talkgroup_meter"] = m.TakeTalkgroupLevel(100) > 0.2 && m.TakeTalkgroupLevel(100) == 0;
    mix["output_meter"] = m.TakeOutputLevel(1) > 0.1 && m.TakeOutputLevel(1) == 0;
    m.SetPriority(300, false);
    m.ToneVolume = 0;
    m.Add(-1, 0, Tone(1200, 3));
    f = m.PullStereo();
    mix["tone_volume_zero_is_silent"] = Math.Abs(Peaks(f[0]).L - plain) < 0.02;
    m.MasterVolume = 0.5;
    f = m.PullStereo();
    mix["master_volume"] = Math.Abs(Peaks(f[0]).L - plain / 2) < 0.02;
    var st0 = new ConsoleSettings { OutputDevice = "Headset", UnselectDevice = "Speakers", SelectSide = Side.Right };
    var m2 = new Mixer();
    st0.ApplyTo(m2);
    mix["settings_routes"] = m2.UnselectRoute.Output == 1 && m2.SelectRoute == new AudioRoute(0, Side.Right)
                             && new ConsoleSettings { OutputDevice = "A", UnselectDevice = "A" }.SeparateUnselectDevice == false;
}
Emit(new { @event = "mixer", checks = mix, ok = mix.Values.All(x => x) });

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
client.PatchResult += (ok, r) => { lock (events) events.Add($"patch:{ok}:{r}"); };
client.ProfileChanged += pr => { lock (events) events.Add("profile:" + string.Join(";", pr.Patches.Select(x => $"{x.Id}={string.Join(",", x.Talkgroups)}"))); };

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
            case "patch":       // patch <tg,tg,...> [id]   |   unpatch <id>
            case "unpatch":
            {
                lock (events) events.Clear();
                if (cmd[0] == "patch")
                    await client.PatchAsync(cmd[1].Split(',').Select(int.Parse), true, cmd.Length > 2 ? int.Parse(cmd[2]) : null, "headless");
                else
                    await client.PatchAsync(Array.Empty<int>(), false, int.Parse(cmd[1]));
                for (int w = 0; w < 60 && !events.Any(e => e.StartsWith("profile:")); w++) await Task.Delay(50);
                lock (events) Emit(new { @event = "patch", events, patches = client.Profile?.Patches.Select(x => new { x.Id, x.Talkgroups }) });
                break;
            }
            case "events":
                lock (events) Emit(new { @event = "events", events });
                break;
        }
    }
    catch (Exception e) { Emit(new { @event = "error", message = e.Message }); }
}
File.Delete(settingsPath);
