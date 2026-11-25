// SPDX-License-Identifier: GPL-2.0-only
using System.Runtime.InteropServices;
using KeyUp.Console.Core;

namespace KeyUp.Console;

/// <summary>8 kHz / 16-bit speaker output (stereo, so audio can be routed left / right) and mono microphone
/// input through WinMM (waveOut / waveIn).
/// A worker thread polls the buffers' DONE flags every few milliseconds; 20 ms per buffer.</summary>
public static class WinMm
{
    public const int WAVE_MAPPER = -1;
    public const int WHDR_DONE = 0x1;

    [StructLayout(LayoutKind.Sequential)]
    public struct WaveFormat
    {
        public ushort wFormatTag, nChannels;
        public uint nSamplesPerSec, nAvgBytesPerSec;
        public ushort nBlockAlign, wBitsPerSample, cbSize;
        public static WaveFormat Pcm8k => new() { wFormatTag = 1, nChannels = 1, nSamplesPerSec = 8000, nAvgBytesPerSec = 16000,
                                                  nBlockAlign = 2, wBitsPerSample = 16, cbSize = 0 };
        public static WaveFormat Stereo8k => new() { wFormatTag = 1, nChannels = 2, nSamplesPerSec = 8000, nAvgBytesPerSec = 32000,
                                                     nBlockAlign = 4, wBitsPerSample = 16, cbSize = 0 };
    }

    [StructLayout(LayoutKind.Sequential)]
    public struct WaveHdr
    {
        public IntPtr lpData;
        public uint dwBufferLength, dwBytesRecorded;
        public IntPtr dwUser;
        public uint dwFlags, dwLoops;
        public IntPtr lpNext, reserved;
    }

    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
    public struct WaveOutCaps
    {
        public ushort wMid, wPid;
        public uint vDriverVersion;
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 32)] public string szPname;
        public uint dwFormats;
        public ushort wChannels, wReserved1;
        public uint dwSupport;
    }

    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
    public struct WaveInCaps
    {
        public ushort wMid, wPid;
        public uint vDriverVersion;
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 32)] public string szPname;
        public uint dwFormats;
        public ushort wChannels, wReserved1;
    }

    [DllImport("winmm.dll")] public static extern int waveOutOpen(out IntPtr hwo, int deviceId, ref WaveFormat fmt, IntPtr cb, IntPtr inst, int flags);
    [DllImport("winmm.dll")] public static extern int waveOutPrepareHeader(IntPtr hwo, IntPtr hdr, int size);
    [DllImport("winmm.dll")] public static extern int waveOutUnprepareHeader(IntPtr hwo, IntPtr hdr, int size);
    [DllImport("winmm.dll")] public static extern int waveOutWrite(IntPtr hwo, IntPtr hdr, int size);
    [DllImport("winmm.dll")] public static extern int waveOutReset(IntPtr hwo);
    [DllImport("winmm.dll")] public static extern int waveOutClose(IntPtr hwo);
    [DllImport("winmm.dll")] public static extern int waveOutGetNumDevs();
    [DllImport("winmm.dll", CharSet = CharSet.Unicode)] public static extern int waveOutGetDevCaps(IntPtr id, ref WaveOutCaps caps, int size);
    [DllImport("winmm.dll")] public static extern int waveInOpen(out IntPtr hwi, int deviceId, ref WaveFormat fmt, IntPtr cb, IntPtr inst, int flags);
    [DllImport("winmm.dll")] public static extern int waveInPrepareHeader(IntPtr hwi, IntPtr hdr, int size);
    [DllImport("winmm.dll")] public static extern int waveInUnprepareHeader(IntPtr hwi, IntPtr hdr, int size);
    [DllImport("winmm.dll")] public static extern int waveInAddBuffer(IntPtr hwi, IntPtr hdr, int size);
    [DllImport("winmm.dll")] public static extern int waveInStart(IntPtr hwi);
    [DllImport("winmm.dll")] public static extern int waveInReset(IntPtr hwi);
    [DllImport("winmm.dll")] public static extern int waveInClose(IntPtr hwi);
    [DllImport("winmm.dll")] public static extern int waveInGetNumDevs();
    [DllImport("winmm.dll", CharSet = CharSet.Unicode)] public static extern int waveInGetDevCaps(IntPtr id, ref WaveInCaps caps, int size);

    public static List<string> OutputDevices()
    {
        var l = new List<string>();
        for (int i = 0; i < waveOutGetNumDevs(); i++)
        {
            var c = new WaveOutCaps();
            l.Add(waveOutGetDevCaps((IntPtr)i, ref c, Marshal.SizeOf<WaveOutCaps>()) == 0 ? c.szPname : $"Device {i}");
        }
        return l;
    }

    public static List<string> InputDevices()
    {
        var l = new List<string>();
        for (int i = 0; i < waveInGetNumDevs(); i++)
        {
            var c = new WaveInCaps();
            l.Add(waveInGetDevCaps((IntPtr)i, ref c, Marshal.SizeOf<WaveInCaps>()) == 0 ? c.szPname : $"Device {i}");
        }
        return l;
    }

    public static int DeviceIndex(List<string> names, string wanted) => string.IsNullOrEmpty(wanted) ? WAVE_MAPPER : Math.Max(names.IndexOf(wanted), WAVE_MAPPER);
}

/// <summary>A ring of prepared 20 ms buffers (header + data in unmanaged memory).</summary>
internal sealed class WaveBuffers : IDisposable
{
    public readonly IntPtr[] Headers;
    private readonly IntPtr[] data;
    public static readonly int HeaderSize = Marshal.SizeOf<WinMm.WaveHdr>();
    private static readonly int FlagsOffset = (int)Marshal.OffsetOf<WinMm.WaveHdr>(nameof(WinMm.WaveHdr.dwFlags));
    private static readonly int RecordedOffset = (int)Marshal.OffsetOf<WinMm.WaveHdr>(nameof(WinMm.WaveHdr.dwBytesRecorded));
    public readonly int Bytes;

    public WaveBuffers(int count, int bytes = 320)
    {
        Bytes = bytes;
        Headers = new IntPtr[count];
        data = new IntPtr[count];
        for (int i = 0; i < count; i++)
        {
            data[i] = Marshal.AllocHGlobal(Bytes);
            Headers[i] = Marshal.AllocHGlobal(HeaderSize);
            Marshal.StructureToPtr(new WinMm.WaveHdr { lpData = data[i], dwBufferLength = Bytes }, Headers[i], false);
        }
    }

    public bool Done(int i) => (Marshal.ReadInt32(Headers[i], FlagsOffset) & WinMm.WHDR_DONE) != 0;
    public int Recorded(int i) => Marshal.ReadInt32(Headers[i], RecordedOffset);
    public void ClearDone(int i) => Marshal.WriteInt32(Headers[i], FlagsOffset, Marshal.ReadInt32(Headers[i], FlagsOffset) & ~WinMm.WHDR_DONE);

    public void Write(int i, short[] pcm)
    {
        var bytes = new byte[Bytes];
        Buffer.BlockCopy(pcm, 0, bytes, 0, Math.Min(Bytes, pcm.Length * 2));
        Marshal.Copy(bytes, 0, data[i], Bytes);
    }

    public short[] Read(int i)
    {
        var bytes = new byte[Bytes];
        Marshal.Copy(data[i], bytes, 0, Bytes);
        var pcm = new short[Bytes / 2];
        Buffer.BlockCopy(bytes, 0, pcm, 0, Bytes);
        return pcm;
    }

    public void Dispose()
    {
        foreach (var p in Headers) Marshal.FreeHGlobal(p);
        foreach (var p in data) Marshal.FreeHGlobal(p);
    }
}

/// <summary>Stereo 8 kHz output. <c>pull</c> returns 20 ms as 320 interleaved samples (L, R).</summary>
public sealed class SpeakerOutput : IDisposable
{
    private IntPtr hwo;
    private readonly WaveBuffers bufs = new(6, 640);
    private readonly Func<short[]> pull;
    private Thread? thread;
    private volatile bool running;

    public SpeakerOutput(Func<short[]> pull) => this.pull = pull;

    public void Start(int device)
    {
        var fmt = WinMm.WaveFormat.Stereo8k;
        int r = WinMm.waveOutOpen(out hwo, device, ref fmt, IntPtr.Zero, IntPtr.Zero, 0);
        if (r != 0) throw new InvalidOperationException($"cannot open the speaker device (MMSYSERR {r})");
        for (int i = 0; i < bufs.Headers.Length; i++)
        {
            WinMm.waveOutPrepareHeader(hwo, bufs.Headers[i], WaveBuffers.HeaderSize);
            bufs.Write(i, new short[320]);
            WinMm.waveOutWrite(hwo, bufs.Headers[i], WaveBuffers.HeaderSize);
        }
        running = true;
        thread = new Thread(Loop) { IsBackground = true, Priority = ThreadPriority.AboveNormal, Name = "speaker" };
        thread.Start();
    }

    private void Loop()
    {
        while (running)
        {
            for (int i = 0; i < bufs.Headers.Length; i++)
            {
                if (!bufs.Done(i)) continue;
                bufs.ClearDone(i);
                bufs.Write(i, pull());
                WinMm.waveOutWrite(hwo, bufs.Headers[i], WaveBuffers.HeaderSize);
            }
            Thread.Sleep(4);
        }
    }

    public void Dispose()
    {
        running = false;
        thread?.Join(500);
        if (hwo != IntPtr.Zero)
        {
            WinMm.waveOutReset(hwo);
            foreach (var h in bufs.Headers) WinMm.waveOutUnprepareHeader(hwo, h, WaveBuffers.HeaderSize);
            WinMm.waveOutClose(hwo);
            hwo = IntPtr.Zero;
        }
        bufs.Dispose();
    }
}

public sealed class MicrophoneInput : IDisposable
{
    private IntPtr hwi;
    private readonly WaveBuffers bufs = new(6);
    private readonly Action<short[]> onFrame;
    private Thread? thread;
    private volatile bool running;

    public MicrophoneInput(Action<short[]> onFrame) => this.onFrame = onFrame;

    public void Start(int device)
    {
        var fmt = WinMm.WaveFormat.Pcm8k;
        int r = WinMm.waveInOpen(out hwi, device, ref fmt, IntPtr.Zero, IntPtr.Zero, 0);
        if (r != 0) throw new InvalidOperationException($"cannot open the microphone (MMSYSERR {r})");
        foreach (var h in bufs.Headers)
        {
            WinMm.waveInPrepareHeader(hwi, h, WaveBuffers.HeaderSize);
            WinMm.waveInAddBuffer(hwi, h, WaveBuffers.HeaderSize);
        }
        WinMm.waveInStart(hwi);
        running = true;
        thread = new Thread(Loop) { IsBackground = true, Priority = ThreadPriority.AboveNormal, Name = "microphone" };
        thread.Start();
    }

    private void Loop()
    {
        while (running)
        {
            for (int i = 0; i < bufs.Headers.Length; i++)
            {
                if (!bufs.Done(i)) continue;
                var frame = bufs.Recorded(i) >= bufs.Bytes ? bufs.Read(i) : null;
                bufs.ClearDone(i);
                WinMm.waveInAddBuffer(hwi, bufs.Headers[i], WaveBuffers.HeaderSize);
                if (frame != null) onFrame(frame);
            }
            Thread.Sleep(4);
        }
    }

    public void Dispose()
    {
        running = false;
        thread?.Join(500);
        if (hwi != IntPtr.Zero)
        {
            WinMm.waveInReset(hwi);
            foreach (var h in bufs.Headers) WinMm.waveInUnprepareHeader(hwi, h, WaveBuffers.HeaderSize);
            WinMm.waveInClose(hwi);
            hwi = IntPtr.Zero;
        }
        bufs.Dispose();
    }
}

/// <summary>
/// The console's speakers: the mixer's output 0 (selected audio and tones) on the main device and, when
/// unselected audio has a device of its own, output 1 on that device. The main device's clock drives the
/// mixer; the second device plays from a short queue so the two cannot drift apart by more than ~100 ms.
/// </summary>
public sealed class AudioEngine : IDisposable
{
    private readonly Mixer mixer;
    private SpeakerOutput? main, second;
    private readonly Queue<short[]> secondQueue = new();
    private readonly object gate = new();
    private static readonly short[] Silence = new short[320];

    public AudioEngine(Mixer mixer) => this.mixer = mixer;

    /// <summary>(Re)open the devices. Returns an error to show, or null.</summary>
    public string? Start(string mainDevice, string secondDevice, bool separate)
    {
        Stop();
        var outs = WinMm.OutputDevices();
        string? err = null;
        try
        {
            main = new SpeakerOutput(PullMain);
            main.Start(WinMm.DeviceIndex(outs, mainDevice));
        }
        catch (Exception e) { err = $"Speaker: {e.Message}"; main = null; }
        if (separate)
        {
            try
            {
                second = new SpeakerOutput(PullSecond);
                second.Start(WinMm.DeviceIndex(outs, secondDevice));
            }
            catch (Exception e) { err = $"Unselect speaker: {e.Message}"; second = null; }
        }
        return err;
    }

    private short[] PullMain()
    {
        var f = mixer.PullStereo();
        if (second != null)
            lock (gate)
            {
                secondQueue.Enqueue(f[1]);
                while (secondQueue.Count > 5) secondQueue.Dequeue();
            }
        return f[0];
    }

    private short[] PullSecond()
    {
        lock (gate) return secondQueue.Count > 0 ? secondQueue.Dequeue() : Silence;
    }

    public void Stop()
    {
        main?.Dispose();
        second?.Dispose();
        main = second = null;
        lock (gate) secondQueue.Clear();
    }

    public void Dispose() => Stop();
}
