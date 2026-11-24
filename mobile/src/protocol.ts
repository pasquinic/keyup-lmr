/** KeyUp PoC protocol v1 (see docs/poc-protocol.md and central/app/services/poc.py). */

export const AUDIO_UP = 0x01;
export const AUDIO_DOWN = 0x02;
export const ULAW_FRAME_BYTES = 160;

export interface Talkgroup {
  id: number;
  name: string;
  priority: number;
  emergency: boolean;
  mode?: string;
}

export interface Profile {
  t: 'auth_ok' | 'profile';
  proto: number;
  unit: { id: number; alias: string };
  system: { name: string; max_call_s: number };
  talkgroups: Talkgroup[];
  selected: number | null;
  scan: number[];
  scan_on: boolean;
  network: boolean;
}

export type ServerMessage =
  | Profile
  | { t: 'auth_fail'; reason: string }
  | { t: 'inhibited'; reason: string }
  | { t: 'selected'; tg: number | null; scan: number[]; scan_on: boolean }
  | { t: 'grant'; tg: number; emergency: boolean }
  | { t: 'deny'; tg: number; reason: string }
  | { t: 'tx_end'; tg: number; reason: string }
  | { t: 'rx_start'; tg: number; tg_name: string; src: number; alias: string; emergency: boolean }
  | { t: 'rx_end'; tg: number }
  | { t: 'emergency'; tg: number; src: number; alias: string; on: boolean }
  | { t: 'status'; network: boolean }
  | { t: 'pong'; ts: number; network: boolean };

export type ClientMessage =
  | { t: 'auth'; token: string; app: string; platform: string }
  | { t: 'select'; tg?: number; scan?: number[]; scan_on?: boolean }
  | { t: 'ptt'; on: true; tg: number; emergency?: boolean }
  | { t: 'ptt'; on: false }
  | { t: 'emergency'; tg: number; on: boolean }
  | { t: 'ping'; ts: number };

/** One binary uplink message: 0x01 followed by whole 160-byte mu-law frames. */
export function encodeUplink(frames: Uint8Array[]): Uint8Array {
  const out = new Uint8Array(1 + frames.length * ULAW_FRAME_BYTES);
  out[0] = AUDIO_UP;
  frames.forEach((f, i) => out.set(f.subarray(0, ULAW_FRAME_BYTES), 1 + i * ULAW_FRAME_BYTES));
  return out;
}

export interface Downlink {
  tg: number;
  src: number;
  ulaw: Uint8Array;
}

/** 0x02, tg (u32 BE), src (u32 BE), mu-law bytes. */
export function decodeDownlink(data: ArrayBuffer | Uint8Array): Downlink | null {
  const b = data instanceof Uint8Array ? data : new Uint8Array(data);
  if (b.length < 9 || b[0] !== AUDIO_DOWN) return null;
  const v = new DataView(b.buffer, b.byteOffset, b.byteLength);
  return { tg: v.getUint32(1), src: v.getUint32(5), ulaw: b.subarray(9) };
}

export const DENY_TEXT: Record<string, string> = {
  busy: 'Channel busy',
  not_permitted: 'Not permitted on this talkgroup',
  network_down: 'System unavailable',
  no_interconnect: 'Talkgroup not available',
  inhibited: 'Radio inhibited',
  timeout: 'Transmit timeout',
  device_revoked: 'This phone was deactivated',
  invalid_token: 'Activation no longer valid',
  poc_disabled: 'PoC disabled for this radio ID',
  system_not_ready: 'System not ready',
  poc_requires_dvm_backend: 'Server not configured for PoC',
};

/** Parse keyup://activate?s=<server>&u=<unit>&c=<code> (QR code / deep link). */
export function parseActivationUri(uri: string): { server: string; unit: number; code: string } | null {
  const m = /^keyup:\/\/activate\?(.*)$/i.exec(uri.trim());
  if (!m) return null;
  const params: Record<string, string> = {};
  for (const part of m[1].split('&')) {
    const [k, v = ''] = part.split('=');
    params[decodeURIComponent(k)] = decodeURIComponent(v);
  }
  const unit = parseInt(params.u, 10);
  if (!params.s || !params.c || !Number.isFinite(unit)) return null;
  return { server: params.s.replace(/\/+$/, ''), unit, code: params.c };
}
