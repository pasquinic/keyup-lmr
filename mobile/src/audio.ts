/**
 * Audio engine on react-native-audio-api (Web Audio API for React Native).
 *
 *  - capture: AudioRecorder -> resample to 8 kHz -> 20 ms frames -> mu-law (only while keyed)
 *  - playback: mu-law frames -> 8 kHz AudioBuffers -> AudioBufferQueueSourceNode, with a
 *    small jitter buffer (cellular delivers in bursts)
 *  - tones: talk-permit, busy, deny ... played through the same context
 *  - session: voice-chat style session (Bluetooth HFP, speaker by default); a phone call
 *    or other interruption releases PTT
 */
import {
  AudioBufferQueueSourceNode,
  AudioContext,
  AudioManager,
  AudioRecorder,
  PlaybackNotificationManager,
} from 'react-native-audio-api';

import { Framer, NET_RATE, Resampler, TONES, toneSequence } from './dsp';
import { floatToUlaw, ulawToFloat } from './g711';

const PREBUFFER_FRAMES = 3; // 60 ms before playback starts
const CHUNK_FRAMES = 3; // enqueue 60 ms per AudioBuffer

export type UplinkHandler = (frame: Uint8Array) => void;

export class AudioEngine {
  private ctx: AudioContext | null = null;
  private recorder: AudioRecorder | null = null;
  private recording = false;
  private framer = new Framer();
  private resampler: Resampler | null = null;
  private onFrame: UplinkHandler | null = null;
  private queue: AudioBufferQueueSourceNode | null = null;
  private pending: Float32Array[] = [];
  private queuedUntil = 0;
  private started = false;
  onInterrupted: (() => void) | null = null;
  level = 0;

  async init(): Promise<boolean> {
    AudioManager.setSystemOptions({
      ios: {
        category: 'playAndRecord',
        mode: 'voiceChat',
        options: ['allowBluetoothHFP', 'defaultToSpeaker', 'duckOthers'],
      },
      android: { mode: 'inCommunication', communicationDevice: 'speaker' },
    });
    const perm = await AudioManager.requestRecordingPermissions();
    await AudioManager.setAudioSessionActivity(true);
    AudioManager.observeAudioInterruptions(true);
    AudioManager.addSystemEventListener('interruption', (e) => {
      if (e.type === 'began') this.onInterrupted?.();
    });
    this.ctx = new AudioContext({ latencyHint: 'interactive', androidOutputProfile: 'voiceCommunication' });
    this.recorder = new AudioRecorder();
    return perm === 'Granted';
  }

  // ------------------------------------------------------------------ capture
  async startCapture(onFrame: UplinkHandler): Promise<void> {
    if (!this.recorder || this.recording) return;
    this.onFrame = onFrame;
    this.framer.reset();
    this.resampler = null;
    this.recorder.onAudioReady({ sampleRate: NET_RATE, bufferLength: 480, channelCount: 1 }, (event) => {
      const buf = event.buffer;
      if (!this.resampler) this.resampler = new Resampler(buf.sampleRate, NET_RATE);
      const pcm = this.resampler.process(buf.getChannelData(0));
      let peak = 0;
      for (let i = 0; i < pcm.length; i++) peak = Math.max(peak, Math.abs(pcm[i]));
      this.level = peak;
      this.framer.push(pcm, (frame) => this.onFrame?.(floatToUlaw(frame)));
    });
    this.recording = true;
    const res = await this.recorder.start();
    if (res.status === 'error') {
      this.recording = false;
      throw new Error(res.message);
    }
  }

  async stopCapture(): Promise<void> {
    if (!this.recorder || !this.recording) return;
    this.recording = false;
    this.onFrame = null;
    this.recorder.clearOnAudioReady();
    await this.recorder.stop();
    this.level = 0;
  }

  get capturing(): boolean {
    return this.recording;
  }

  // ------------------------------------------------------------------ playback
  private ensureQueue(): AudioBufferQueueSourceNode | null {
    if (!this.ctx) return null;
    if (!this.queue) {
      this.queue = this.ctx.createBufferQueueSource();
      this.queue.connect(this.ctx.destination);
      this.started = false;
      this.queuedUntil = this.ctx.currentTime;
    }
    return this.queue;
  }

  private enqueue(samples: Float32Array): void {
    const q = this.ensureQueue();
    if (!q || !this.ctx) return;
    const buf = this.ctx.createBuffer(1, samples.length, NET_RATE);
    buf.copyToChannel(samples as Float32Array<ArrayBuffer>, 0);
    q.enqueueBuffer(buf);
    if (!this.started) {
      q.start();
      this.started = true;
    }
    this.queuedUntil = Math.max(this.queuedUntil, this.ctx.currentTime) + samples.length / NET_RATE;
  }

  /** Feed one or more 160-byte mu-law frames from the network. */
  playUlaw(ulaw: Uint8Array): void {
    for (let i = 0; i + 160 <= ulaw.length; i += 160) this.pending.push(ulawToFloat(ulaw.subarray(i, i + 160)));
    const need = this.started ? CHUNK_FRAMES : PREBUFFER_FRAMES;
    if (this.pending.length >= need) this.flush();
  }

  private flush(): void {
    if (!this.pending.length) return;
    const total = this.pending.reduce((n, f) => n + f.length, 0);
    const out = new Float32Array(total);
    let o = 0;
    for (const f of this.pending) {
      out.set(f, o);
      o += f.length;
    }
    this.pending = [];
    this.enqueue(out);
  }

  /** Call ended: play what is buffered, then release the queue source. */
  endPlayback(): void {
    this.flush();
    const q = this.queue;
    if (q && this.ctx) {
      const remaining = Math.max(0, this.queuedUntil - this.ctx.currentTime);
      q.stop(this.ctx.currentTime + remaining + 0.05);
    }
    this.queue = null;
    this.started = false;
  }

  /** Drop received audio immediately (e.g. we key up). */
  stopPlayback(): void {
    this.pending = [];
    try {
      this.queue?.stop();
    } catch {
      /* already stopped */
    }
    this.queue = null;
    this.started = false;
  }

  playTone(name: keyof typeof TONES): void {
    if (!this.ctx) return;
    const samples = toneSequence(TONES[name]);
    const buf = this.ctx.createBuffer(1, samples.length, NET_RATE);
    buf.copyToChannel(samples as Float32Array<ArrayBuffer>, 0);
    const src = this.ctx.createBufferSource();
    src.buffer = buf;
    src.connect(this.ctx.destination);
    src.start();
  }

  // ------------------------------------------------------------------ background presence
  /**
   * Shows the media notification. On Android this runs the audio foreground service so the
   * app keeps receiving calls in the background; on iOS it fills the lock-screen controls,
   * whose play/pause (= headset button) can be mapped to PTT.
   */
  async showPresence(title: string, subtitle: string): Promise<void> {
    try {
      await PlaybackNotificationManager.show({ title, artist: subtitle, state: 'playing', isLiveStream: true });
      await PlaybackNotificationManager.enableControl('play', true);
      await PlaybackNotificationManager.enableControl('pause', true);
    } catch {
      /* notification permission denied: foreground-only operation */
    }
  }

  onHeadsetButton(cb: () => void): () => void {
    const a = PlaybackNotificationManager.addEventListener('playbackNotificationPlay', cb);
    const b = PlaybackNotificationManager.addEventListener('playbackNotificationPause', cb);
    return () => {
      a.remove();
      b.remove();
    };
  }

  async hidePresence(): Promise<void> {
    try {
      await PlaybackNotificationManager.hide();
    } catch {
      /* ignore */
    }
  }
}
