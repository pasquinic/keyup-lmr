import assert from 'node:assert/strict';
import { test } from 'node:test';

import { AudioPort, PocClient } from '../src/client';

class FakeSocket {
  binaryType = 'blob';
  readyState = 0;
  sent: (string | ArrayBuffer)[] = [];
  onopen: (() => void) | null = null;
  onclose: ((e: { code: number }) => void) | null = null;
  onerror: ((e: unknown) => void) | null = null;
  onmessage: ((e: { data: unknown }) => void) | null = null;
  constructor(public url: string) {}
  send(d: string | ArrayBuffer | ArrayBufferView) {
    this.sent.push(typeof d === 'string' ? d : (d as ArrayBuffer));
  }
  close() {
    this.readyState = 3;
    this.onclose?.({ code: 1000 });
  }
  open() {
    this.readyState = 1;
    this.onopen?.();
  }
  srv(m: object) {
    this.onmessage?.({ data: JSON.stringify(m) });
  }
  json() {
    return this.sent.filter((x): x is string => typeof x === 'string').map((x) => JSON.parse(x));
  }
  binary() {
    return this.sent.filter((x): x is ArrayBuffer => typeof x !== 'string');
  }
}

class FakeAudio implements AudioPort {
  tones: string[] = [];
  played = 0;
  mic: ((f: Uint8Array) => void) | null = null;
  async startCapture(cb: (f: Uint8Array) => void) {
    this.mic = cb;
  }
  async stopCapture() {
    this.mic = null;
  }
  playUlaw(u: Uint8Array) {
    this.played += u.length / 160;
  }
  endPlayback() {}
  stopPlayback() {}
  playTone(n: 'talkPermit' | 'busy' | 'deny' | 'endOfTx' | 'emergency' | 'disconnected') {
    this.tones.push(n);
  }
}

const PROFILE = {
  t: 'auth_ok', proto: 1, unit: { id: 1001, alias: 'Engine 1' }, system: { name: 'KeyUp', max_call_s: 120 },
  talkgroups: [{ id: 100, name: 'Fire', priority: 5, emergency: true }, { id: 200, name: 'PW', priority: 3, emergency: false }],
  selected: 100, scan: [], scan_on: false, network: true,
};

function setup() {
  const sockets: FakeSocket[] = [];
  const audio = new FakeAudio();
  const c = new PocClient({ server: 'https://lmr.example.com', token: 'tok', unit: 1001, alias: 'E1' }, audio, 'test',
    (url) => {
      const s = new FakeSocket(url);
      sockets.push(s);
      return s;
    });
  c.start();
  const ws = sockets[0];
  ws.open();
  ws.srv(PROFILE);
  return { c, ws, audio, sockets };
}

test('authenticates over wss and loads the profile', () => {
  const { c, ws } = setup();
  assert.equal(ws.url, 'wss://lmr.example.com/poc/ws');
  assert.deepEqual(ws.json()[0], { t: 'auth', token: 'tok', app: '1.0.0', platform: 'test' });
  assert.equal(c.state.link, 'online');
  assert.equal(c.state.selected, 100);
  c.stop();
});

test('PTT: audio spoken before the grant is held, then sent after talk-permit', async () => {
  const { c, ws, audio } = setup();
  await c.pttDown();
  assert.deepEqual(ws.json().at(-1), { t: 'ptt', on: true, tg: 100, emergency: false });
  for (let i = 0; i < 4; i++) audio.mic?.(new Uint8Array(160).fill(i));
  assert.equal(ws.binary().length, 0); // nothing before the grant
  ws.srv({ t: 'grant', tg: 100, emergency: false });
  assert.equal(c.state.tx, 'transmitting');
  assert.ok(audio.tones.includes('talkPermit'));
  const sentFrames = ws.binary().reduce((n, b) => n + (b.byteLength - 1) / 160, 0);
  assert.equal(sentFrames, 4);
  for (let i = 0; i < 3; i++) audio.mic?.(new Uint8Array(160));
  assert.equal(ws.binary().at(-1)?.byteLength, 1 + 3 * 160); // batched 60 ms
  c.pttUp();
  assert.deepEqual(ws.json().at(-1), { t: 'ptt', on: false });
  assert.equal(c.state.tx, 'idle');
  assert.equal(audio.mic, null);
  c.stop();
});

test('busy deny plays the busy tone and returns to idle', async () => {
  const { c, ws, audio } = setup();
  await c.pttDown();
  ws.srv({ t: 'deny', tg: 100, reason: 'busy' });
  assert.equal(c.state.tx, 'idle');
  assert.ok(audio.tones.includes('busy'));
  assert.equal(c.state.notice, 'Channel busy');
  c.stop();
});

test('receive: half duplex, talker shown, playback fed', () => {
  const { c, ws, audio } = setup();
  ws.srv({ t: 'rx_start', tg: 100, tg_name: 'Fire', src: 2002, alias: 'Chief', emergency: false });
  const pkt = new Uint8Array(9 + 320);
  pkt[0] = 2;
  ws.onmessage?.({ data: pkt.buffer });
  assert.equal(audio.played, 2);
  assert.equal(c.state.rx?.alias, 'Chief');
  ws.srv({ t: 'rx_end', tg: 100 });
  assert.equal(c.state.rx, null);
  assert.equal(c.state.lastHeard?.src, 2002);
  c.stop();
});

test('inhibit stops the radio and does not reconnect', () => {
  const { c, ws, sockets } = setup();
  ws.srv({ t: 'inhibited', reason: 'inhibited' });
  ws.close();
  assert.equal(c.state.link, 'refused');
  assert.equal(sockets.length, 1);
  c.stop();
});

test('lost connection reconnects and restores the selected talkgroup', async () => {
  const { c, ws, sockets } = setup();
  c.select(200);
  ws.close();
  assert.equal(c.state.link, 'offline');
  await new Promise((r) => setTimeout(r, 1100));
  assert.equal(sockets.length, 2);
  sockets[1].open();
  sockets[1].srv({ ...PROFILE, selected: 100 });
  assert.equal(c.state.selected, 200);
  assert.deepEqual(sockets[1].json().at(-1), { t: 'select', tg: 200, scan: [], scan_on: false });
  c.stop();
});
