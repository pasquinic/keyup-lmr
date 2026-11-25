/** The radio: channel display, talkgroup knob, PTT, scan, emergency. */
import * as Haptics from 'expo-haptics';
import { useKeepAwake } from 'expo-keep-awake';
import { router } from 'expo-router';
import React, { useEffect, useRef, useState } from 'react';
import { Modal, Pressable, SectionList, StyleSheet, Text, View } from 'react-native';
import { SafeAreaView } from 'react-native-safe-area-context';

import { DENY_TEXT } from '../protocol';
import { useSession } from '../session';
import { C, mono } from '../theme';

const EMERGENCY_HOLD_MS = 2000;

function KeepAwake() {
  useKeepAwake('keyup-radio');
  return null;
}

function useTicker(active: boolean): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!active) return;
    const id = setInterval(() => setNow(Date.now()), 250);
    return () => clearInterval(id);
  }, [active]);
  return now;
}

export default function Radio() {
  const { state, client, creds, settings, micAllowed, updateSettings } = useSession();
  const [picker, setPicker] = useState(false);
  const emergencyTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const prevTx = useRef(state?.tx);
  const now = useTicker(!!state && (state.tx === 'transmitting' || !!state.rx));

  useEffect(() => {
    if (!creds) router.replace('/activate');
  }, [creds]);

  useEffect(() => {
    if (state?.tx === 'transmitting' && prevTx.current !== 'transmitting') {
      void Haptics.impactAsync(Haptics.ImpactFeedbackStyle.Heavy);
    }
    prevTx.current = state?.tx;
  }, [state?.tx]);

  if (!state || !creds) return <View style={{ flex: 1, backgroundColor: C.bg }} />;

  if (state.link === 'refused') {
    return (
      <SafeAreaView style={[st.screen, { justifyContent: 'center', padding: 24 }]}>
        <Text style={[st.big, { color: C.bad }]}>{DENY_TEXT[state.refusedReason ?? ''] ?? 'Access refused'}</Text>
        <Text style={st.muted}>
          Radio {creds.unit} cannot join the system ({state.refusedReason}). Contact your administrator.
        </Text>
        <Pressable style={[st.btn, { marginTop: 24 }]} onPress={() => router.push('/settings')}>
          <Text style={st.btnText}>Settings</Text>
        </Pressable>
      </SafeAreaView>
    );
  }

  const tg = state.talkgroups.find((t) => t.id === state.selected) ?? null;
  const zone = state.zones.find((z) => z.id === state.zone) ?? state.zones[0] ?? null;
  // the channel knob steps through the talkgroups of the current zone
  const knob = zone ? zone.talkgroups : state.talkgroups.map((t) => t.id);
  const idx = tg ? knob.indexOf(tg.id) : -1;
  const step = (d: number) => {
    if (!knob.length || state.tx !== 'idle') return;
    client?.select(knob[(idx + d + knob.length) % knob.length], zone?.id);
    void Haptics.selectionAsync();
  };
  const byId = new Map(state.talkgroups.map((t) => [t.id, t]));
  const sections = state.zones.map((z) => ({
    zone: z,
    data: z.talkgroups.map((id) => byId.get(id)).filter((t): t is NonNullable<typeof t> => !!t),
  }));
  const tx = state.tx;
  const rx = state.rx;
  const emergencyHere = tg ? state.emergencies[tg.id] : undefined;
  const linkColor = state.link === 'online' ? (state.network ? C.ok : C.warn) : C.bad;
  const linkText =
    state.link === 'online' ? (state.network ? 'CONNECTED' : 'SYSTEM DOWN') : state.link === 'connecting' ? 'CONNECTING' : 'NO SERVICE';
  const secs = (t0: number) => Math.max(0, Math.floor((now - t0) / 1000));

  const pttIn = () => {
    void Haptics.impactAsync(Haptics.ImpactFeedbackStyle.Medium);
    void client?.pttDown();
  };
  const pttOut = () => client?.pttUp();

  const emergencyIn = () => {
    emergencyTimer.current = setTimeout(() => {
      void Haptics.notificationAsync(Haptics.NotificationFeedbackType.Warning);
      client?.emergency(!emergencyHere);
    }, EMERGENCY_HOLD_MS);
  };
  const emergencyOut = () => {
    if (emergencyTimer.current) clearTimeout(emergencyTimer.current);
  };

  return (
    <SafeAreaView style={st.screen}>
      {settings.keepAwake && <KeepAwake />}
      {/* status bar */}
      <View style={st.status}>
        <View style={[st.dot, { backgroundColor: linkColor }]} />
        <Text style={[st.statusText, { color: linkColor }]}>{linkText}</Text>
        <Text style={[st.statusText, { flex: 1, textAlign: 'right' }]} numberOfLines={1}>
          {state.systemName} · ID {creds.unit}
        </Text>
      </View>

      {/* display */}
      <View style={[st.display, tx === 'transmitting' && { borderColor: C.tx }, !!rx && { borderColor: C.rx },
        !!emergencyHere && { borderColor: C.emerg }]}>
        {emergencyHere && (
          <Text style={st.emergencyBanner}>EMERGENCY · {emergencyHere.alias} ({emergencyHere.src})</Text>
        )}
        <Text style={st.zone}>
          {(zone && state.zones.length > 1 ? zone.name.toUpperCase() : 'TALKGROUP') + (state.scanOn ? ' · SCAN' : '')}
        </Text>
        <Text style={st.tgName} numberOfLines={1} adjustsFontSizeToFit>
          {tg ? tg.name : state.talkgroups.length ? 'Select a talkgroup' : 'No talkgroups'}
        </Text>
        <Text style={st.tgId}>{tg ? `TG ${tg.id}${tg.mode ? ' · ' + tg.mode.replace('P25P1', 'P25') : ''}` : ' '}</Text>
        <View style={st.activity}>
          {tx === 'transmitting' ? (
            <Text style={[st.activityText, { color: C.tx }]}>● TX  {secs(state.txStarted)}s</Text>
          ) : tx === 'requesting' ? (
            <Text style={[st.activityText, { color: C.warn }]}>REQUESTING…</Text>
          ) : rx ? (
            <View>
              <Text style={[st.activityText, { color: C.rx }]} numberOfLines={1}>
                ◀ {rx.alias} ({rx.src})
              </Text>
              {rx.tg !== state.selected && <Text style={st.muted}>on {rx.tgName}</Text>}
            </View>
          ) : state.lastHeard ? (
            <Text style={st.muted} numberOfLines={1}>
              Last: {state.lastHeard.alias} ({state.lastHeard.src}) · {secs(state.lastHeard.at)}s ago
            </Text>
          ) : (
            <Text style={st.muted}> </Text>
          )}
        </View>
        {state.notice && <Text style={st.notice}>{state.notice}</Text>}
        {!micAllowed && <Text style={st.notice}>Microphone permission denied — enable it in system settings</Text>}
      </View>

      {/* channel knob */}
      <View style={st.knob}>
        <Pressable style={st.knobBtn} onPress={() => step(-1)}>
          <Text style={st.knobText}>◀</Text>
        </Pressable>
        <Pressable style={[st.knobBtn, { flex: 1 }]} onPress={() => setPicker(true)}>
          <Text style={st.btnText}>Channels ({state.talkgroups.length})</Text>
        </Pressable>
        <Pressable style={st.knobBtn} onPress={() => step(1)}>
          <Text style={st.knobText}>▶</Text>
        </Pressable>
      </View>

      {/* PTT */}
      <Pressable
        onPressIn={pttIn}
        onPressOut={pttOut}
        disabled={!tg}
        style={[st.ptt, tx === 'transmitting' && st.pttTx, tx === 'requesting' && { borderColor: C.warn }, !tg && { opacity: 0.4 }]}
      >
        <Text style={[st.pttText, tx === 'transmitting' && { color: '#fff' }]}>
          {tx === 'transmitting' ? 'TALK' : 'PUSH TO TALK'}
        </Text>
        <Text style={[st.muted, tx === 'transmitting' && { color: '#ffe' }]}>
          {settings.headsetPtt ? 'or headset button' : 'hold'}
        </Text>
      </Pressable>

      {/* bottom row */}
      <View style={st.row}>
        <Pressable
          style={[st.btn, { flex: 1 }, state.scanOn && { borderColor: C.accent }]}
          onPress={() => updateSettings({ scanOn: !settings.scanOn })}
          onLongPress={() => router.push('/settings')}
        >
          <Text style={[st.btnText, state.scanOn && { color: C.accent }]}>SCAN {state.scanOn ? 'ON' : 'OFF'}</Text>
        </Pressable>
        <Pressable
          style={[st.btn, { flex: 1, borderColor: C.emerg }, !tg?.emergency && { opacity: 0.4 }]}
          disabled={!tg?.emergency}
          onPressIn={emergencyIn}
          onPressOut={emergencyOut}
        >
          <Text style={[st.btnText, { color: C.emerg }]}>{emergencyHere ? 'CLEAR EMRG' : 'EMRG (hold)'}</Text>
        </Pressable>
        <Pressable style={[st.btn, { width: 64 }]} onPress={() => router.push('/settings')}>
          <Text style={st.btnText}>⚙</Text>
        </Pressable>
      </View>

      <Modal visible={picker} animationType="slide" transparent onRequestClose={() => setPicker(false)}>
        <View style={st.sheetWrap}>
          <View style={st.sheet}>
            <Text style={st.sheetTitle}>Zones & talkgroups</Text>
            <SectionList
              sections={sections}
              keyExtractor={(t, i) => `${t.id}:${i}`}
              stickySectionHeadersEnabled={false}
              renderSectionHeader={({ section }) => (
                <Pressable onPress={() => client?.setZone(section.zone.id)}>
                  <Text style={[st.zoneHeader, section.zone.id === zone?.id && { color: C.accent },
                    !!section.zone.color && { borderLeftColor: section.zone.color }]}>
                    {section.zone.name}
                  </Text>
                </Pressable>
              )}
              renderItem={({ item, section }) => (
                <Pressable
                  style={[st.tgRow, item.id === state.selected && section.zone.id === zone?.id && { backgroundColor: C.panel2 }]}
                  onPress={() => {
                    client?.select(item.id, section.zone.id);
                    setPicker(false);
                  }}
                >
                  <Text style={[st.btnText, item.id === state.selected && { color: C.accent }]}>{item.name}</Text>
                  <Text style={st.muted}>
                    {item.id}
                    {state.scan.includes(item.id) ? ' · scan' : ''}
                  </Text>
                </Pressable>
              )}
            />
            <Pressable style={[st.btn, { marginTop: 12 }]} onPress={() => setPicker(false)}>
              <Text style={st.btnText}>Close</Text>
            </Pressable>
          </View>
        </View>
      </Modal>
    </SafeAreaView>
  );
}

const st = StyleSheet.create({
  screen: { flex: 1, backgroundColor: C.bg, paddingHorizontal: 16 },
  status: { flexDirection: 'row', alignItems: 'center', gap: 8, paddingVertical: 8 },
  dot: { width: 10, height: 10, borderRadius: 5 },
  zoneHeader: { color: C.muted, fontSize: 12, fontWeight: '800', letterSpacing: 1, paddingTop: 14, paddingBottom: 6,
    paddingLeft: 8, borderLeftWidth: 3, borderLeftColor: 'transparent', textTransform: 'uppercase' },
  statusText: { color: C.muted, fontSize: 12, fontWeight: '700', letterSpacing: 1 },
  display: {
    backgroundColor: C.panel, borderRadius: 14, borderWidth: 2, borderColor: C.line, padding: 18, minHeight: 190,
  },
  emergencyBanner: {
    backgroundColor: C.emerg, color: '#fff', fontWeight: '800', textAlign: 'center', paddingVertical: 6,
    borderRadius: 6, marginBottom: 8, overflow: 'hidden',
  },
  zone: { color: C.muted, fontSize: 12, letterSpacing: 2 },
  tgName: { color: C.text, fontSize: 34, fontWeight: '700', marginTop: 4, fontFamily: mono },
  tgId: { color: C.accent, fontSize: 15, marginTop: 2, fontFamily: mono },
  activity: { marginTop: 18, minHeight: 44 },
  activityText: { fontSize: 22, fontWeight: '700', fontFamily: mono },
  notice: { color: C.warn, marginTop: 8, fontWeight: '600' },
  muted: { color: C.muted, fontSize: 13 },
  big: { color: C.text, fontSize: 26, fontWeight: '700', marginBottom: 12 },
  knob: { flexDirection: 'row', gap: 10, marginTop: 14 },
  knobBtn: {
    backgroundColor: C.panel2, borderColor: C.line, borderWidth: 1, borderRadius: 10, paddingVertical: 14,
    paddingHorizontal: 22, alignItems: 'center',
  },
  knobText: { color: C.text, fontSize: 20 },
  ptt: {
    flex: 1, marginVertical: 16, borderRadius: 28, borderWidth: 3, borderColor: C.line, backgroundColor: C.panel,
    alignItems: 'center', justifyContent: 'center', minHeight: 180,
  },
  pttTx: { backgroundColor: '#8a1c2b', borderColor: C.tx },
  pttText: { color: C.text, fontSize: 30, fontWeight: '800', letterSpacing: 2 },
  row: { flexDirection: 'row', gap: 10, marginBottom: 12 },
  btn: {
    borderWidth: 1, borderColor: C.line, backgroundColor: C.panel2, borderRadius: 10, paddingVertical: 14,
    alignItems: 'center', justifyContent: 'center',
  },
  btnText: { color: C.text, fontSize: 15, fontWeight: '700' },
  sheetWrap: { flex: 1, backgroundColor: '#000a', justifyContent: 'flex-end' },
  sheet: { backgroundColor: C.panel, borderTopLeftRadius: 18, borderTopRightRadius: 18, padding: 18, maxHeight: '75%' },
  sheetTitle: { color: C.text, fontSize: 18, fontWeight: '700', marginBottom: 10 },
  tgRow: { paddingVertical: 14, paddingHorizontal: 10, borderBottomWidth: 1, borderBottomColor: C.line, borderRadius: 6 },
});
