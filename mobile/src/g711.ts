/** G.711 mu-law <-> linear PCM (8 kHz voice), matching the KeyUp server (lmrcommon/audio.py). */

const BIAS = 0x84;
const CLIP = 32635;

const DECODE = new Int16Array(256);
for (let i = 0; i < 256; i++) {
  const u = ~i & 0xff;
  const sign = u & 0x80;
  const exponent = (u >> 4) & 0x07;
  const mantissa = u & 0x0f;
  const sample = (((mantissa << 3) + BIAS) << exponent) - BIAS;
  DECODE[i] = sign ? -sample : sample;
}

export function linearToUlaw(sample: number): number {
  let s = Math.max(-32768, Math.min(32767, Math.round(sample)));
  const sign = s < 0 ? 0x80 : 0;
  if (s < 0) s = -s;
  s = Math.min(s, CLIP) + BIAS;
  let exponent = 7;
  for (let mask = 0x4000; exponent > 0 && !(s & mask); mask >>= 1) exponent--;
  const mantissa = (s >> (exponent + 3)) & 0x0f;
  return ~(sign | (exponent << 4) | mantissa) & 0xff;
}

/** Float32 samples in [-1, 1] -> mu-law bytes. */
export function floatToUlaw(input: Float32Array): Uint8Array {
  const out = new Uint8Array(input.length);
  for (let i = 0; i < input.length; i++) out[i] = linearToUlaw(input[i] * 32767);
  return out;
}

/** mu-law bytes -> Float32 samples in [-1, 1]. */
export function ulawToFloat(input: Uint8Array): Float32Array {
  const out = new Float32Array(input.length);
  for (let i = 0; i < input.length; i++) out[i] = DECODE[input[i]] / 32768;
  return out;
}

export function ulawToLinear(u: number): number {
  return DECODE[u & 0xff];
}
