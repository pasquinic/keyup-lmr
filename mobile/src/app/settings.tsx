/** Settings: scan list, headset PTT, keep-awake, radio identity, deactivation. */
import { router } from 'expo-router';
import React from 'react';
import { Alert, Pressable, ScrollView, StyleSheet, Switch, Text, View } from 'react-native';

import { APP_VERSION } from '../client';
import { useSession } from '../session';
import { C } from '../theme';

export default function Settings() {
  const { state, creds, settings, updateSettings, deactivate } = useSession();
  const tgs = state?.talkgroups ?? [];

  const toggleScan = (id: number) => {
    const scan = settings.scan.includes(id) ? settings.scan.filter((x) => x !== id) : [...settings.scan, id].slice(0, 16);
    void updateSettings({ scan });
  };

  const confirmDeactivate = () =>
    Alert.alert('Deactivate this phone?', 'You will need a new activation code from your administrator.', [
      { text: 'Cancel', style: 'cancel' },
      {
        text: 'Deactivate', style: 'destructive',
        onPress: async () => {
          await deactivate();
          router.replace('/activate');
        },
      },
    ]);

  return (
    <ScrollView contentContainerStyle={s.wrap}>
      <Text style={s.h}>Radio</Text>
      <View style={s.card}>
        <Row k="Radio ID" v={String(creds?.unit ?? '—')} />
        <Row k="Alias" v={state?.unit?.alias ?? creds?.alias ?? '—'} />
        <Row k="Server" v={creds?.server ?? '—'} />
        <Row k="System" v={state?.systemName ?? '—'} />
        <Row k="App" v={APP_VERSION} />
      </View>

      <Text style={s.h}>Audio & controls</Text>
      <View style={s.card}>
        <Toggle label="Headset / Bluetooth button toggles PTT" value={settings.headsetPtt}
          onChange={(v) => updateSettings({ headsetPtt: v })} />
        <Toggle label="Keep screen on" value={settings.keepAwake} onChange={(v) => updateSettings({ keepAwake: v })} />
        <Toggle label="Scan" value={settings.scanOn} onChange={(v) => updateSettings({ scanOn: v })} />
      </View>

      <Text style={s.h}>Scan list</Text>
      <Text style={s.muted}>
        While scan is on you also hear these talkgroups. Your selected talkgroup has priority. Transmit always goes to the
        selected talkgroup.
      </Text>
      <View style={s.card}>
        {tgs.map((t) => (
          <Toggle key={t.id} label={`${t.name}  (${t.id})`} value={settings.scan.includes(t.id)}
            onChange={() => toggleScan(t.id)} />
        ))}
        {!tgs.length && <Text style={s.muted}>Connect to load your talkgroups.</Text>}
      </View>

      <Pressable style={s.danger} onPress={confirmDeactivate}>
        <Text style={{ color: C.bad, fontWeight: '700' }}>Deactivate this phone</Text>
      </Pressable>
    </ScrollView>
  );
}

function Row({ k, v }: { k: string; v: string }) {
  return (
    <View style={s.row}>
      <Text style={s.muted}>{k}</Text>
      <Text style={s.v} numberOfLines={1}>{v}</Text>
    </View>
  );
}

function Toggle({ label, value, onChange }: { label: string; value: boolean; onChange: (v: boolean) => void }) {
  return (
    <View style={s.row}>
      <Text style={[s.v, { flex: 1, textAlign: 'left' }]}>{label}</Text>
      <Switch value={value} onValueChange={onChange} trackColor={{ true: C.accent, false: C.line }} />
    </View>
  );
}

const s = StyleSheet.create({
  wrap: { padding: 16, paddingBottom: 48 },
  h: { color: C.muted, fontSize: 12, letterSpacing: 1.5, textTransform: 'uppercase', marginTop: 18, marginBottom: 8 },
  card: { backgroundColor: C.panel, borderRadius: 12, borderWidth: 1, borderColor: C.line, paddingHorizontal: 14 },
  row: {
    flexDirection: 'row', alignItems: 'center', justifyContent: 'space-between', paddingVertical: 12, gap: 12,
    borderBottomWidth: StyleSheet.hairlineWidth, borderBottomColor: C.line,
  },
  v: { color: C.text, fontSize: 15, flexShrink: 1, textAlign: 'right' },
  muted: { color: C.muted, fontSize: 13, marginBottom: 8 },
  danger: { marginTop: 28, borderWidth: 1, borderColor: C.bad, borderRadius: 10, paddingVertical: 14, alignItems: 'center' },
});
