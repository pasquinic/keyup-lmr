// SPDX-License-Identifier: GPL-2.0-only
namespace KeyUp.Console.Core;

/// <summary>
/// Mixes the received calls (one stream per call) into one 8 kHz output, with a per-talkgroup volume
/// and mute. Each stream has a small jitter buffer: it starts playing once PrimeFrames are queued and is
/// dropped after IdleFrames of silence. Thread-safe: the network thread adds, the audio thread pulls.
/// </summary>
public sealed class Mixer
{
    public const int FrameSamples = 160;
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
    public double MasterVolume { get; set; } = 1.0;

    public void SetVolume(int tg, double v) { lock (gate) volume[tg] = Math.Clamp(v, 0, 2); }
    public void SetMuted(int tg, bool m) { lock (gate) { if (m) muted.Add(tg); else muted.Remove(tg); } }
    public double VolumeOf(int tg) { lock (gate) return volume.TryGetValue(tg, out var v) ? v : 1.0; }

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

    /// <summary>Next 20 ms of mixed audio (160 samples).</summary>
    public short[] Pull()
    {
        var acc = new int[FrameSamples];
        lock (gate)
        {
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
                var f = s.Frames.Dequeue();
                if (muted.Contains(s.Tg)) continue;
                double g = (volume.TryGetValue(s.Tg, out var v) ? v : 1.0) * MasterVolume;
                for (int i = 0; i < FrameSamples && i < f.Length; i++) acc[i] += (int)(f[i] * g);
            }
        }
        var o = new short[FrameSamples];
        for (int i = 0; i < FrameSamples; i++)
        {
            int x = acc[i];
            // soft clip so two loud talkers do not wrap around
            o[i] = (short)(x > 24000 ? 24000 + (x - 24000) / 4 > short.MaxValue ? short.MaxValue : 24000 + (x - 24000) / 4
                         : x < -24000 ? -24000 + (x + 24000) / 4 < short.MinValue ? short.MinValue : -24000 + (x + 24000) / 4
                         : x);
        }
        return o;
    }

    public int ActiveStreams { get { lock (gate) return streams.Count; } }
}
