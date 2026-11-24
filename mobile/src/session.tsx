/** App-wide session: stored credentials + settings, the audio engine and the PoC client. */
import * as SecureStore from 'expo-secure-store';
import React, { createContext, useCallback, useContext, useEffect, useRef, useState } from 'react';
import { Platform } from 'react-native';

import { AudioEngine } from './audio';
import { APP_VERSION, Credentials, PocClient, RadioState } from './client';

const CREDS_KEY = 'keyup.credentials';
const SETTINGS_KEY = 'keyup.settings';

export interface Settings {
  headsetPtt: boolean; // headset / Bluetooth media button toggles PTT
  keepAwake: boolean;
  scan: number[];
  scanOn: boolean;
}

const DEFAULT_SETTINGS: Settings = { headsetPtt: true, keepAwake: false, scan: [], scanOn: false };

interface SessionValue {
  ready: boolean;
  creds: Credentials | null;
  settings: Settings;
  state: RadioState | null;
  client: PocClient | null;
  micAllowed: boolean;
  activate(server: string, unit: number, code: string): Promise<void>;
  deactivate(): Promise<void>;
  updateSettings(patch: Partial<Settings>): Promise<void>;
}

const Ctx = createContext<SessionValue | null>(null);

export function useSession(): SessionValue {
  const v = useContext(Ctx);
  if (!v) throw new Error('useSession outside SessionProvider');
  return v;
}

export async function activateDevice(server: string, unit: number, code: string): Promise<Credentials> {
  const base = server.trim().replace(/\/+$/, '');
  if (!/^https?:\/\//i.test(base)) throw new Error('Server must start with https://');
  const res = await fetch(`${base}/api/poc/activate`, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({
      unit, code: code.trim(), device_name: `${Platform.OS} ${String(Platform.Version)}`,
      platform: Platform.OS, app_version: APP_VERSION,
    }),
  });
  const body = (await res.json().catch(() => ({}))) as { token?: string; alias?: string; detail?: string };
  if (!res.ok || !body.token) throw new Error(body.detail ?? `Activation failed (${res.status})`);
  return { server: base, token: body.token, unit, alias: body.alias ?? String(unit) };
}

export function SessionProvider({ children }: { children: React.ReactNode }) {
  const [ready, setReady] = useState(false);
  const [creds, setCreds] = useState<Credentials | null>(null);
  const [settings, setSettings] = useState<Settings>(DEFAULT_SETTINGS);
  const [state, setState] = useState<RadioState | null>(null);
  const [micAllowed, setMicAllowed] = useState(true);
  const [client, setClient] = useState<PocClient | null>(null);
  const engine = useRef<AudioEngine | null>(null);
  const clientRef = useRef<PocClient | null>(null);
  const settingsRef = useRef(settings);
  useEffect(() => {
    settingsRef.current = settings;
  }, [settings]);

  useEffect(() => {
    (async () => {
      const [c, s] = await Promise.all([SecureStore.getItemAsync(CREDS_KEY), SecureStore.getItemAsync(SETTINGS_KEY)]);
      if (s) setSettings({ ...DEFAULT_SETTINGS, ...(JSON.parse(s) as Partial<Settings>) });
      if (c) setCreds(JSON.parse(c) as Credentials);
      setReady(true);
    })();
  }, []);

  // one client per activation
  useEffect(() => {
    if (!creds) return;
    let cancelled = false;
    let unsubscribe = () => {};
    let unHeadset = () => {};
    (async () => {
      if (!engine.current) {
        engine.current = new AudioEngine();
        setMicAllowed(await engine.current.init());
      }
      if (cancelled) return;
      const eng = engine.current;
      const c = new PocClient(creds, eng, Platform.OS);
      eng.onInterrupted = () => c.pttUp(); // phone call etc. releases PTT
      clientRef.current = c;
      setClient(c);
      unsubscribe = c.subscribe(setState);
      c.start();
      await eng.showPresence('KeyUp', `Unit ${creds.unit} · ${creds.alias}`);
      unHeadset = eng.onHeadsetButton(() => {
        if (settingsRef.current.headsetPtt) c.pttToggle();
      });
    })();
    return () => {
      cancelled = true;
      unsubscribe();
      unHeadset();
      clientRef.current?.stop();
      clientRef.current = null;
      setClient(null);
      void engine.current?.hidePresence();
    };
  }, [creds]);

  // re-apply the saved scan list once connected
  const online = state?.link === 'online';
  useEffect(() => {
    if (online && settings.scan.length) clientRef.current?.setScan(settings.scan, settings.scanOn);
  }, [online]); // eslint-disable-line react-hooks/exhaustive-deps

  const activate = useCallback(async (server: string, unit: number, code: string) => {
    const c = await activateDevice(server, unit, code);
    await SecureStore.setItemAsync(CREDS_KEY, JSON.stringify(c));
    setCreds(c);
  }, []);

  const deactivate = useCallback(async () => {
    await SecureStore.deleteItemAsync(CREDS_KEY);
    setState(null);
    setCreds(null);
  }, []);

  const updateSettings = useCallback(async (patch: Partial<Settings>) => {
    const next = { ...settingsRef.current, ...patch };
    setSettings(next);
    await SecureStore.setItemAsync(SETTINGS_KEY, JSON.stringify(next));
    if (patch.scan !== undefined || patch.scanOn !== undefined) clientRef.current?.setScan(next.scan, next.scanOn);
  }, []);

  return (
    <Ctx.Provider value={{ ready, creds, settings, state, client, micAllowed, activate, deactivate, updateSettings }}>
      {children}
    </Ctx.Provider>
  );
}
