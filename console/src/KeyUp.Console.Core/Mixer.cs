// SPDX-License-Identifier: GPL-2.0-only
namespace KeyUp.Console.Core;

/// <summary>Where a sound goes on its output: the left ear, the right ear or both.
/// Auto (per talkgroup only) means "use the select / unselect route's side".</summary>
public enum Side { Auto, Left, Right, Both }

/// <summary>An audio route: which output device (0 = primary, 1 = second device) and which side.</summary>
public sealed record AudioRoute(int Output, Side Side);

/// <summary>
/// Mixes the received calls (one stream per call) into up to two stereo 8 kHz outputs.
///
/// Routing: audio of the selected talkgroup follows <see cref="SelectRoute"/>, every other monitored
/// talkgroup follows <see cref="UnselectRoute"/>; a talkgroup can override the side (left / right / both).
/// Local tones (negative call ids) follow the select route on both sides.
///
/// Gain: per-talkgroup volume × select or unselect volume × master. While a priority-marked talkgroup is
/// playing, every other talkgroup is ducked by <see cref="DuckDb"/>.
///
/// Levels: the peak of each talkgroup's received audio (before any volume) and of each output (after
/// mixing) are kept until read, for VU meters.
///
/// Each stream has a small jitter buffer: it starts playing once PrimeFrames are queued and is dropped after
/// IdleFrames of silence. Thread-safe: the network thread adds, the audio thread pulls, the UI reads levels.
/// </summary>
public sealed class Mixer
{
    public const int FrameSamples = 160;
    public const int Outputs = 2;
    public int PrimeFrames { get; set; } = 3;
    public int MaxQueueFrames { get; set; } = 25;
    private const int IdleFrames = 25;

    private sealed class Stream
    {
        public int Tg;
        public readonly Queue<short[]> Frames = new();
        public bool Playing;
        public int Idle;
    }

    private readonly Dictionary<long, Stream> streams = new();
    private readonly object gate = new();
    private readonly Dictionary<int, double> volume = new();
    private readonly HashSet<int> muted = new();
    private readonly HashSet<int> priority = new();
    private readonly Dictionary<int, Side> sides = new();
    private readonly Dictionary<int, double> tgPeak = new();
    private readonly double[] outPeak = new double[Outputs];

    public double MasterVolume { get; set; } = 1.0;
    public double SelectVolume { get; set; } = 1.0;
    public double UnselectVolume { get; set; } = 1.0;
    public double ToneVolume { get; set; } = 1.0;
    /// <summary>How far other talkgroups drop while a priority talkgroup plays (dB, 0 = no ducking).</summary>
    public double DuckDb { get; set; } = 12;
    public int SelectedTg { get; set; }
    public AudioRoute SelectRoute { get; set; } = new(0, Side.Both);
    public AudioRoute UnselectRoute { get; set; } = new(0, Side.Both);
    /// <summary>True during the last pulled frame if a priority talkgroup was playing.</summary>
    public bool PriorityActive { get; private set; }

    public void SetVolume(int tg, double v) { lock (gate) volume[tg] = Math.Clamp(v, 0, 2); }
    public void SetMuted(int tg, bool m) { lock (gate) { if (m) muted.Add(tg); else muted.Remove(tg); } }
    public double VolumeOf(int tg) { lock (gate) return volume.TryGetValue(tg, out var v) ? v : 1.0; }
    public void SetPriority(int tg, bool on) { lock (gate) { if (on) priority.Add(tg); else priority.Remove(tg); } }
    public bool IsPriority(int tg) { lock (gate) return priority.Contains(tg); }
    public void SetSide(int tg, Side s) { lock (gate) { if (s == Side.Auto) sides.Remove(tg); else sides[tg] = s; } }
    public Side SideOf(int tg) { lock (gate) return sides.TryGetValue(tg, out var s) ? s : Side.Auto; }

    /// <summary>Queue audio for a call (any whole number of 20 ms frames). Negative ids are local sounds
    /// (tones): they are not latency-capped and play as soon as they arrive.</summary>
    public void Add(long callId, int tg, byte[] ulaw)
    {
        var pcm = G711.Decode(ulaw);
        lock (gate)
        {
            if (!streams.TryGetValue(callId, out var s)) streams[callId] = s = new Stream { Tg = tg };
            s.Tg = tg;
            for (int i = 0; i + FrameSamples <= pcm.Length; i += FrameSamples)
                s.Frames.Enqueue(pcm[i..(i + FrameSamples)]);
            if (callId >= 0)
                while (s.Frames.Count > MaxQueueFrames) s.Frames.Dequeue();   // late burst: keep latency bounded
            else
                s.Playing = true;
            s.Idle = 0;
        }
    }

    public void Drop(long callId) { lock (gate) streams.Remove(callId); }

    public static double DbToGain(double db) => Math.Pow(10, db / 20.0);

    /// <summary>Next 20 ms for each output: <see cref="Outputs"/> arrays of 320 interleaved stereo samples (L, R).</summary>
    public short[][] PullStereo()
    {
        var acc = new int[Outputs][];
        for (int o = 0; o < Outputs; o++) acc[o] = new int[FrameSamples * 2];
        lock (gate)
        {
            // take this tick's frame from every stream first, so ducking knows what is playing
            var due = new List<(long Id, Stream S, short[] F)>();
            foreach (var (id, s) in streams.ToArray())
            {
                if (!s.Playing && s.Frames.Count >= PrimeFrames) s.Playing = true;
                if (!s.Playing) continue;
                if (s.Frames.Count == 0)
                {
                    if (++s.Idle > IdleFrames) streams.Remove(id);
                    else s.Playing = s.Idle < 3;     // re-prime after a gap
                    continue;
                }
                s.Idle = 0;
                due.Add((id, s, s.Frames.Dequeue()));
            }
            PriorityActive = due.Any(d => d.Id >= 0 && priority.Contains(d.S.Tg) && !muted.Contains(d.S.Tg));
            double duck = DuckDb > 0 ? DbToGain(-DuckDb) : 1.0;

            foreach (var (id, s, f) in due)
            {
                AudioRoute route;
                Side side;
                double g;
                if (id < 0)
                {
                    route = SelectRoute;
                    side = Side.Both;
                    g = ToneVolume * MasterVolume;
                }
                else
                {
                    int peak = 0;
                    foreach (var x in f) peak = Math.Max(peak, Math.Abs((int)x));
                    tgPeak[s.Tg] = Math.Max(tgPeak.GetValueOrDefault(s.Tg), peak / 32768.0);
                    if (muted.Contains(s.Tg)) continue;
                    bool sel = s.Tg == SelectedTg;
                    route = sel ? SelectRoute : UnselectRoute;
                    side = sides.TryGetValue(s.Tg, out var ov) ? ov : route.Side;
                    g = (volume.TryGetValue(s.Tg, out var v) ? v : 1.0) * (sel ? SelectVolume : UnselectVolume) * MasterVolume;
                    if (PriorityActive && !priority.Contains(s.Tg)) g *= duck;
                }
                if (g <= 0) continue;
                var a = acc[Math.Clamp(route.Output, 0, Outputs - 1)];
                bool left = side is Side.Left or Side.Both or Side.Auto, right = side is Side.Right or Side.Both or Side.Auto;
                for (int i = 0; i < FrameSamples && i < f.Length; i++)
                {
                    int x = (int)(f[i] * g);
                    if (left) a[2 * i] += x;
                    if (right) a[2 * i + 1] += x;
                }
            }

            var outs = new short[Outputs][];
            for (int o = 0; o < Outputs; o++)
            {
                outs[o] = new short[FrameSamples * 2];
                int peak = 0;
                for (int i = 0; i < outs[o].Length; i++)
                {
                    var y = SoftClip(acc[o][i]);
                    outs[o][i] = y;
                    peak = Math.Max(peak, Math.Abs((int)y));
                }
                outPeak[o] = Math.Max(outPeak[o], peak / 32768.0);
            }
            return outs;
        }
    }

    /// <summary>Next 20 ms as one mono frame (both outputs and sides summed). For tests and simple use.</summary>
    public short[] Pull()
    {
        var st = PullStereo();
        var o = new short[FrameSamples];
        for (int i = 0; i < FrameSamples; i++)
        {
            int sum = 0;
            for (int k = 0; k < Outputs; k++)
            {
                int l = st[k][2 * i], r = st[k][2 * i + 1];
                sum += Math.Abs(l) >= Math.Abs(r) ? l : r;    // a one-sided route still counts once
            }
            o[i] = SoftClip(sum);
        }
        return o;
    }

    /// <summary>Peak level (0..1) of a talkgroup's received audio since the last call, then reset.</summary>
    public double TakeTalkgroupLevel(int tg)
    {
        lock (gate) { var v = tgPeak.GetValueOrDefault(tg); tgPeak.Remove(tg); return v; }
    }

    /// <summary>Peak level (0..1) of an output since the last call, then reset.</summary>
    public double TakeOutputLevel(int output)
    {
        lock (gate) { var v = outPeak[output]; outPeak[output] = 0; return v; }
    }

    private static short SoftClip(int x) =>
        // soft clip so two loud talkers do not wrap around
        (short)(x > 24000 ? Math.Min(short.MaxValue, 24000 + (x - 24000) / 4)
              : x < -24000 ? Math.Max(short.MinValue, -24000 + (x + 24000) / 4)
              : x);

    public int ActiveStreams { get { lock (gate) return streams.Count; } }
}

/// <summary>Peak level helpers for meters.</summary>
public static class Levels
{
    public static double Peak(short[] pcm)
    {
        int p = 0;
        foreach (var x in pcm) p = Math.Max(p, Math.Abs((int)x));
        return p / 32768.0;
    }

    /// <summary>Linear 0..1 to dBFS, floored at -60.</summary>
    public static double ToDb(double level) => level <= 0.001 ? -60 : Math.Max(-60, 20 * Math.Log10(level));

    /// <summary>Apply a gain to 16-bit audio with clipping.</summary>
    public static short[] Gain(short[] pcm, double g)
    {
        if (Math.Abs(g - 1.0) < 1e-6) return pcm;
        var o = new short[pcm.Length];
        for (int i = 0; i < pcm.Length; i++) o[i] = (short)Math.Clamp((int)(pcm[i] * g), short.MinValue, short.MaxValue);
        return o;
    }
}
