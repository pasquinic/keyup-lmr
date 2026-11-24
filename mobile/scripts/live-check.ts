/**
 * Runs the app's real PocClient (no UI, synthetic audio) against a live KeyUp server.
 *   npx tsx scripts/live-check.ts http://127.0.0.1:8080 <device-token> [seconds]
 * Prints a JSON summary; used by tests/lab_dvm_e2e.py.
 */
import { AudioPort, PocClient } from '../src/client';
import { floatToUlaw } from '../src/g711';
import { toneSequence } from '../src/dsp';

const [server, token, secs = '2'] = process.argv.slice(2);
const tones: string[] = [];
let rxFrames = 0;
let timer: ReturnType<typeof setInterval> | null = null;

const audio: AudioPort = {
  async startCapture(onFrame) {
    let i = 0;
    const tone = floatToUlaw(toneSequence([[600, 20]], 0.4));
    timer = setInterval(() => onFrame(tone), 20);
    void i;
  },
  async stopCapture() {
    if (timer) clearInterval(timer);
    timer = null;
  },
  playUlaw(u) {
    rxFrames += u.length / 160;
  },
  endPlayback() {},
  stopPlayback() {},
  playTone(n) {
    tones.push(n);
  },
};

const c = new PocClient({ server, token, unit: 0, alias: '' }, audio, 'node',
  (url) => new WebSocket(url) as never);
const wait = (ms: number) => new Promise((r) => setTimeout(r, ms));

(async () => {
  c.start();
  for (let i = 0; i < 50 && c.state.link !== 'online' && c.state.link !== 'refused'; i++) await wait(100);
  const profile = { link: c.state.link, unit: c.state.unit, talkgroups: c.state.talkgroups.map((t) => t.id) };
  await c.pttDown();
  for (let i = 0; i < 50 && c.state.tx === 'requesting'; i++) await wait(50);
  const granted = c.state.tx === 'transmitting';
  await wait(Number(secs) * 1000);
  c.pttUp();
  await wait(800);
  c.stop();
  console.log(JSON.stringify({ ...profile, granted, tones, rxFrames, notice: c.state.notice }));
  process.exit(0);
})();
