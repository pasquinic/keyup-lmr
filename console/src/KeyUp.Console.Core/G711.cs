// SPDX-License-Identifier: GPL-2.0-only
namespace KeyUp.Console.Core;

/// <summary>ITU-T G.711 mu-law, byte-for-byte the same as KeyUp's server (lmrcommon/audio.py).</summary>
public static class G711
{
    private const int Bias = 0x84, Clip = 32635;
    private static readonly short[] DecodeTable = BuildDecode();

    private static short[] BuildDecode()
    {
        var t = new short[256];
        for (int i = 0; i < 256; i++)
        {
            int u = ~i & 0xFF;
            int t2 = ((u & 0x0F) << 3) + Bias;
            t2 <<= (u & 0x70) >> 4;
            t[i] = (short)((u & 0x80) != 0 ? Bias - t2 : t2 - Bias);
        }
        return t;
    }

    public static byte Encode(short sample)
    {
        int s = sample;
        int sign = (s >> 8) & 0x80;
        if (sign != 0) s = -s;
        if (s > Clip) s = Clip;
        s += Bias;
        int exponent = 7;
        for (int mask = 0x4000; (s & mask) == 0 && exponent > 0; mask >>= 1) exponent--;
        int mantissa = (s >> (exponent + 3)) & 0x0F;
        return (byte)~(sign | (exponent << 4) | mantissa);
    }

    public static short Decode(byte u) => DecodeTable[u];

    public static byte[] Encode(ReadOnlySpan<short> pcm)
    {
        var o = new byte[pcm.Length];
        for (int i = 0; i < pcm.Length; i++) o[i] = Encode(pcm[i]);
        return o;
    }

    public static short[] Decode(ReadOnlySpan<byte> ulaw)
    {
        var o = new short[ulaw.Length];
        for (int i = 0; i < ulaw.Length; i++) o[i] = DecodeTable[ulaw[i]];
        return o;
    }
}
