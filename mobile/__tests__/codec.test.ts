import assert from 'node:assert/strict';
import { test } from 'node:test';

import { Framer, NET_RATE, Resampler, toneSequence, TONES } from '../src/dsp';
import { floatToUlaw, linearToUlaw, ulawToFloat, ulawToLinear } from '../src/g711';
import { decodeDownlink, encodeUplink, parseActivationUri } from '../src/protocol';

test('mu-law matches the server encoder byte for byte', () => {
  const samples = [0, 1, -1, 100, -100, 1000, -1000, 8000, -8000, 32767, -32768, 12345, -12345];
  // reference values from lmrcommon/audio.py pcm_to_ulaw
  assert.deepEqual(samples.map(linearToUlaw), [255, 255, 127, 242, 114, 206, 78, 160, 32, 128, 0, 151, 23]);
});

test('mu-law round trip stays within companding error', () => {
  for (const s of [-30000, -5000, -50, 0, 50, 5000, 30000]) {
    const back = ulawToLinear(linearToUlaw(s));
    assert.ok(Math.abs(back - s) <= Math.max(16, Math.abs(s) * 0.07), `${s} -> ${back}`);
  }
  const tone = toneSequence([[1000, 20]], 0.5);
  const rt = ulawToFloat(floatToUlaw(tone));
  assert.equal(rt.length, 160);
});

test('resampler 48k -> 8k keeps rate across odd block sizes', () => {
  const r = new Resampler(48000);
  let out = 0;
  for (const n of [1000, 437, 2048, 515]) out += r.process(new Float32Array(n).fill(0.5)).length;
  assert.ok(Math.abs(out - 4000 / 6) <= 1, String(out));
  const r2 = new Resampler(44100);
  const y = r2.process(new Float32Array(44100).fill(0.25));
  assert.ok(Math.abs(y.length - NET_RATE) <= 1);
  assert.ok(Math.abs(y[100] - 0.25) < 1e-6);
});

test('framer emits exact 20 ms frames', () => {
  const f = new Framer();
  const frames: Float32Array[] = [];
  for (const n of [100, 100, 250, 30]) f.push(new Float32Array(n), (x) => frames.push(x));
  assert.equal(frames.length, 3);
  assert.ok(frames.every((x) => x.length === 160));
});

test('uplink / downlink framing', () => {
  const up = encodeUplink([new Uint8Array(160).fill(1), new Uint8Array(160).fill(2)]);
  assert.equal(up.length, 321);
  assert.equal(up[0], 0x01);
  assert.equal(up[161], 2);
  const down = new Uint8Array(9 + 160);
  down[0] = 0x02;
  new DataView(down.buffer).setUint32(1, 100);
  new DataView(down.buffer).setUint32(5, 9200003);
  const d = decodeDownlink(down.buffer);
  assert.deepEqual([d?.tg, d?.src, d?.ulaw.length], [100, 9200003, 160]);
  assert.equal(decodeDownlink(new Uint8Array([1, 2, 3])), null);
});

test('activation QR / deep link parsing', () => {
  assert.deepEqual(parseActivationUri('keyup://activate?s=https://lmr.example.com/&u=1001&c=ABCD2345'), {
    server: 'https://lmr.example.com', unit: 1001, code: 'ABCD2345',
  });
  assert.equal(parseActivationUri('https://example.com'), null);
  assert.equal(parseActivationUri('keyup://activate?s=x&u=abc&c=1'), null);
  assert.ok(Object.keys(TONES).includes('talkPermit'));
});
