/** Small DSP helpers: resampling to the 8 kHz network rate, 20 ms framing, tones, levels. */

export const NET_RATE = 8000;
export const FRAME_SAMPLES = 160; // 20 ms @ 8 kHz

/**
 * Streaming resampler to the 8 kHz network rate. Downsampling (the usual case: mics run
 * at 16-48 kHz) averages each output period (box filter) to limit aliasing; upsampling
 * interpolates linearly. Phase is kept across blocks so 20 ms framing stays seamless.
 */
export class Resampler {
  private phase = 0;
  private acc = 0;
  private count = 0;
  private last = 0;
  constructor(private readonly inRate: number, private readonly outRate: number = NET_RATE) {}

  process(input: Float32Array): Float32Array {
    if (this.inRate === this.outRate) return input;
    const step = this.inRate / this.outRate;
    const out: number[] = [];
    if (step > 1) {
      for (let i = 0; i < input.length; i++) {
        this.acc += input[i];
        this.count++;
        this.phase += 1;
        if (this.phase >= step) {
          out.push(this.acc / this.count);
          this.phase -= step;
          this.acc = 0;
          this.count = 0;
        }
      }
      return Float32Array.from(out);
    }
    // upsample: phase is the fractional read position relative to the previous sample
    for (let i = 0; i < input.length; i++) {
      const a = this.last;
      const b = input[i];
      while (this.phase < 1) {
        out.push(a + (b - a) * this.phase);
        this.phase += step;
      }
      this.phase -= 1;
      this.last = b;
    }
    return Float32Array.from(out);
  }
}

/** Collects arbitrary-size sample blocks and emits exact 20 ms frames. */
export class Framer {
  private buf = new Float32Array(FRAME_SAMPLES * 8);
  private len = 0;

  push(samples: Float32Array, emit: (frame: Float32Array) => void): void {
    let off = 0;
    while (off < samples.length) {
      const n = Math.min(FRAME_SAMPLES - this.len, samples.length - off);
      this.buf.set(samples.subarray(off, off + n), this.len);
      this.len += n;
      off += n;
      if (this.len === FRAME_SAMPLES) {
        emit(this.buf.slice(0, FRAME_SAMPLES));
        this.len = 0;
      }
    }
  }

  reset(): void {
    this.len = 0;
  }
}

/** Tone sequence [(freqHz or 0 for silence, ms)] at the network rate. */
export function toneSequence(spec: [number, number][], amp = 0.25): Float32Array {
  const total = spec.reduce((n, [, ms]) => n + Math.round((NET_RATE * ms) / 1000), 0);
  const out = new Float32Array(total);
  let o = 0;
  for (const [f, ms] of spec) {
    const n = Math.round((NET_RATE * ms) / 1000);
    for (let i = 0; i < n; i++) {
      // short fade in/out avoids clicks
      const env = Math.min(1, i / 40, (n - i) / 40);
      out[o + i] = f ? amp * env * Math.sin((2 * Math.PI * f * i) / NET_RATE) : 0;
    }
    o += n;
  }
  return out;
}

export const TONES: Record<string, [number, number][]> = {
  talkPermit: [[910, 60], [0, 20], [910, 60]],
  busy: [[480, 250], [0, 250], [480, 250], [0, 250], [480, 250]],
  deny: [[1200, 150], [600, 150], [1200, 150], [600, 150]],
  endOfTx: [[1500, 40]],
  emergency: [[1500, 200], [1000, 200], [1500, 200], [1000, 200]],
  disconnected: [[400, 300], [0, 100], [300, 400]],
};

export function rms(samples: Float32Array): number {
  let s = 0;
  for (let i = 0; i < samples.length; i++) s += samples[i] * samples[i];
  return samples.length ? Math.sqrt(s / samples.length) : 0;
}
