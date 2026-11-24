/**
 * PoC client: one WebSocket to the KeyUp server, behaving like a subscriber radio.
 * Framework-free (no React) so it can be unit-tested; the UI subscribes to `state`.
 */
import {
  ClientMessage,
  DENY_TEXT,
  Profile,
  ServerMessage,
  Talkgroup,
  decodeDownlink,
  encodeUplink,
} from './protocol';

export type LinkState = 'idle' | 'connecting' | 'online' | 'offline' | 'refused';
export type TxState = 'idle' | 'requesting' | 'transmitting';

export interface RadioState {
  link: LinkState;
  network: boolean; // radio system (FNE) reachable from the server
  unit: { id: number; alias: string } | null;
  systemName: string;
  talkgroups: Talkgroup[];
  selected: number | null;
  scan: number[];
  scanOn: boolean;
  tx: TxState;
  txTg: number | null;
  txStarted: number;
  rx: { tg: number; tgName: string; src: number; alias: string; emergency: boolean } | null;
  lastHeard: { tg: number; src: number; alias: string; at: number } | null;
  emergencies: Record<number, { src: number; alias: string }>;
  notice: string | null;
  refusedReason: string | null;
}

export interface AudioPort {
  startCapture(onFrame: (ulaw: Uint8Array) => void): Promise<void>;
  stopCapture(): Promise<void>;
  playUlaw(ulaw: Uint8Array): void;
  endPlayback(): void;
  stopPlayback(): void;
  playTone(name: 'talkPermit' | 'busy' | 'deny' | 'endOfTx' | 'emergency' | 'disconnected'): void;
}

export interface Credentials {
  server: string; // https://host
  token: string;
  unit: number;
  alias: string;
}

type WSLike = {
  binaryType: string;
  readyState: number;
  send(data: string | ArrayBuffer | ArrayBufferView): void;
  close(code?: number): void;
  onopen: (() => void) | null;
  onclose: ((e: { code: number }) => void) | null;
  onerror: ((e: unknown) => void) | null;
  onmessage: ((e: { data: unknown }) => void) | null;
};

export const APP_VERSION = '1.0.0';
const UPLINK_BATCH = 3; // 60 ms per WebSocket message
const PRE_GRANT_MAX = 50; // keep up to 1 s of audio said before the grant arrived

export class PocClient {
  state: RadioState = {
    link: 'idle', network: false, unit: null, systemName: 'KeyUp', talkgroups: [], selected: null,
    scan: [], scanOn: false, tx: 'idle', txTg: null, txStarted: 0, rx: null, lastHeard: null,
    emergencies: {}, notice: null, refusedReason: null,
  };
  private listeners = new Set<(s: RadioState) => void>();
  private ws: WSLike | null = null;
  private backoff = 1000;
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  private pingTimer: ReturnType<typeof setInterval> | null = null;
  private lastPong = 0;
  private stopped = true;
  private uplink: Uint8Array[] = [];
  private preGrant: Uint8Array[] = [];
  private pttHeld = false;

  constructor(
    private creds: Credentials,
    private audio: AudioPort,
    private platform: string,
    private makeSocket: (url: string) => WSLike = (url) => new WebSocket(url) as unknown as WSLike,
  ) {}

  // ------------------------------------------------------------------ state
  subscribe(fn: (s: RadioState) => void): () => void {
    this.listeners.add(fn);
    fn(this.state);
    return () => this.listeners.delete(fn);
  }

  private set(patch: Partial<RadioState>): void {
    this.state = { ...this.state, ...patch };
    this.listeners.forEach((fn) => fn(this.state));
  }

  private notice(text: string): void {
    this.set({ notice: text });
    setTimeout(() => this.state.notice === text && this.set({ notice: null }), 3500);
  }

  // ------------------------------------------------------------------ connection
  start(): void {
    this.stopped = false;
    this.connect();
  }

  stop(): void {
    this.stopped = true;
    if (this.reconnectTimer) clearTimeout(this.reconnectTimer);
    this.release();
    this.ws?.close(1000);
    this.ws = null;
    this.set({ link: 'idle' });
  }

  private wsUrl(): string {
    return this.creds.server.replace(/^http/i, 'ws').replace(/\/+$/, '') + '/poc/ws';
  }

  private connect(): void {
    if (this.stopped) return;
    this.set({ link: 'connecting' });
    const ws = this.makeSocket(this.wsUrl());
    ws.binaryType = 'arraybuffer';
    this.ws = ws;
    ws.onopen = () => {
      this.send({ t: 'auth', token: this.creds.token, app: APP_VERSION, platform: this.platform });
    };
    ws.onmessage = (e) => {
      if (typeof e.data === 'string') this.onMessage(JSON.parse(e.data) as ServerMessage);
      else if (e.data instanceof ArrayBuffer) this.onAudio(e.data);
    };
    ws.onerror = () => {
      /* onclose follows */
    };
    ws.onclose = () => {
      if (this.ws !== ws) return;
      this.ws = null;
      if (this.pingTimer) clearInterval(this.pingTimer);
      const wasTx = this.state.tx !== 'idle';
      this.release(false);
      if (this.state.link === 'online') this.audio.playTone('disconnected');
      if (this.state.link === 'refused' || this.stopped) return;
      this.set({ link: 'offline', rx: null });
      if (wasTx) this.notice('Connection lost');
      this.reconnectTimer = setTimeout(() => this.connect(), this.backoff);
      this.backoff = Math.min(this.backoff * 2, 15000);
    };
  }

  private send(msg: ClientMessage): void {
    if (this.ws && this.ws.readyState === 1) this.ws.send(JSON.stringify(msg));
  }

  private startPing(): void {
    if (this.pingTimer) clearInterval(this.pingTimer);
    this.lastPong = Date.now();
    this.pingTimer = setInterval(() => {
      if (Date.now() - this.lastPong > 20000) {
        this.ws?.close(4001); // dead link (cell handover etc.): reconnect
        return;
      }
      this.send({ t: 'ping', ts: Date.now() });
    }, 5000);
  }

  // ------------------------------------------------------------------ server messages
  private onMessage(m: ServerMessage): void {
    switch (m.t) {
      case 'auth_ok':
      case 'profile':
        this.applyProfile(m);
        if (m.t === 'auth_ok') {
          this.backoff = 1000;
          this.startPing();
          // restore the user's channel / scan choice after a reconnect
          if (this.state.selected && m.talkgroups.some((t) => t.id === this.state.selected)) {
            this.send({ t: 'select', tg: this.state.selected, scan: this.state.scan, scan_on: this.state.scanOn });
          }
        }
        break;
      case 'auth_fail':
      case 'inhibited':
        this.set({ link: 'refused', refusedReason: m.reason });
        this.release(false);
        break;
      case 'selected':
        this.set({ selected: m.tg, scan: m.scan, scanOn: m.scan_on });
        break;
      case 'grant':
        if (this.state.tx === 'requesting' && this.pttHeld) {
          this.audio.playTone('talkPermit');
          this.set({ tx: 'transmitting', txTg: m.tg, txStarted: Date.now() });
          // audio spoken while waiting for the grant goes out first
          const early = this.preGrant.splice(0);
          for (let i = 0; i < early.length; i += UPLINK_BATCH) this.sendAudio(early.slice(i, i + UPLINK_BATCH));
        } else {
          this.send({ t: 'ptt', on: false }); // released before the grant arrived
        }
        break;
      case 'deny':
        this.audio.playTone(m.reason === 'busy' ? 'busy' : 'deny');
        this.notice(DENY_TEXT[m.reason] ?? m.reason);
        this.release(false);
        break;
      case 'tx_end':
        if (m.reason !== 'released') {
          this.audio.playTone('deny');
          this.notice(DENY_TEXT[m.reason] ?? m.reason);
        }
        this.release(false);
        break;
      case 'rx_start':
        this.set({ rx: { tg: m.tg, tgName: m.tg_name, src: m.src, alias: m.alias, emergency: m.emergency } });
        break;
      case 'rx_end': {
        const rx = this.state.rx;
        this.audio.endPlayback();
        this.set({ rx: null, lastHeard: rx ? { tg: rx.tg, src: rx.src, alias: rx.alias, at: Date.now() } : this.state.lastHeard });
        break;
      }
      case 'emergency': {
        const em = { ...this.state.emergencies };
        if (m.on) {
          em[m.tg] = { src: m.src, alias: m.alias };
          this.audio.playTone('emergency');
        } else delete em[m.tg];
        this.set({ emergencies: em });
        break;
      }
      case 'status':
        this.set({ network: m.network });
        break;
      case 'pong':
        this.lastPong = Date.now();
        if (m.network !== this.state.network) this.set({ network: m.network });
        break;
    }
  }

  private applyProfile(p: Profile): void {
    const keep = this.state.selected && p.talkgroups.some((t) => t.id === this.state.selected);
    this.set({
      link: 'online', network: p.network, unit: p.unit, systemName: p.system.name, talkgroups: p.talkgroups,
      selected: keep ? this.state.selected : p.selected, refusedReason: null,
      scan: this.state.scan.filter((id) => p.talkgroups.some((t) => t.id === id)),
    });
  }

  private onAudio(data: ArrayBuffer): void {
    if (this.state.tx !== 'idle') return; // half duplex
    const d = decodeDownlink(data);
    if (d) this.audio.playUlaw(d.ulaw);
  }

  // ------------------------------------------------------------------ user actions
  select(tg: number): void {
    if (this.state.tx !== 'idle') return;
    this.set({ selected: tg });
    this.send({ t: 'select', tg });
  }

  setScan(scan: number[], on: boolean): void {
    this.set({ scan, scanOn: on });
    this.send({ t: 'select', scan, scan_on: on });
  }

  /** PTT pressed. Capture starts immediately; audio before the grant is held, then sent. */
  async pttDown(emergency = false): Promise<void> {
    const tg = this.state.selected;
    if (this.pttHeld || tg == null) return;
    if (this.state.link !== 'online') {
      this.audio.playTone('deny');
      this.notice('Not connected');
      return;
    }
    this.pttHeld = true;
    this.preGrant = [];
    this.uplink = [];
    this.audio.stopPlayback();
    this.set({ tx: 'requesting', txTg: tg, rx: null });
    this.send({ t: 'ptt', on: true, tg, emergency });
    try {
      await this.audio.startCapture((frame) => this.onMicFrame(frame));
    } catch {
      this.notice('Microphone unavailable');
      this.pttUp();
    }
  }

  pttUp(): void {
    if (!this.pttHeld) return;
    this.pttHeld = false;
    if (this.uplink.length) this.sendAudio(this.uplink.splice(0));
    const wasTx = this.state.tx === 'transmitting';
    this.send({ t: 'ptt', on: false });
    this.release(false);
    if (wasTx) this.audio.playTone('endOfTx');
  }

  /** PTT toggle for a headset / Bluetooth button. */
  pttToggle(): void {
    if (this.pttHeld) this.pttUp();
    else void this.pttDown();
  }

  emergency(on: boolean): void {
    const tg = this.state.selected;
    if (tg == null) return;
    this.send({ t: 'emergency', tg, on });
  }

  private onMicFrame(frame: Uint8Array): void {
    if (this.state.tx === 'requesting') {
      this.preGrant.push(frame);
      if (this.preGrant.length > PRE_GRANT_MAX) this.preGrant.shift();
      return;
    }
    if (this.state.tx !== 'transmitting') return;
    this.uplink.push(frame);
    if (this.uplink.length >= UPLINK_BATCH) this.sendAudio(this.uplink.splice(0));
  }

  private sendAudio(frames: Uint8Array[]): void {
    if (!frames.length || !this.ws || this.ws.readyState !== 1) return;
    const msg = encodeUplink(frames);
    this.ws.send(msg.buffer.slice(msg.byteOffset, msg.byteOffset + msg.byteLength) as ArrayBuffer);
  }

  private release(sendOff = true): void {
    if (sendOff && this.pttHeld) this.send({ t: 'ptt', on: false });
    this.pttHeld = false;
    this.preGrant = [];
    this.uplink = [];
    void this.audio.stopCapture();
    if (this.state.tx !== 'idle') this.set({ tx: 'idle', txTg: null, txStarted: 0 });
  }
}
