/**
 * Activation: the administrator issues a one-time code for a radio ID (Subscriber Radios ->
 * PoC). Scan its QR code (keyup://activate?s=..&u=..&c=..), open it as a link, or type it in.
 */
import { CameraView, useCameraPermissions } from 'expo-camera';
import { router, useLocalSearchParams } from 'expo-router';
import React, { useEffect, useRef, useState } from 'react';
import {
  ActivityIndicator, Image, KeyboardAvoidingView, Platform, Pressable, ScrollView, StyleSheet, Text, TextInput, View,
} from 'react-native';

import { parseActivationUri } from '../protocol';
import { useSession } from '../session';
import { C } from '../theme';

export default function Activate() {
  const params = useLocalSearchParams<{ s?: string; u?: string; c?: string }>();
  const { activate, creds } = useSession();
  const [server, setServer] = useState(params.s ?? 'https://');
  const [unit, setUnit] = useState(params.u ?? '');
  const [code, setCode] = useState(params.c ?? '');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [scanning, setScanning] = useState(false);
  const [perm, requestPerm] = useCameraPermissions();
  const handled = useRef(false);

  useEffect(() => {
    if (creds) router.replace('/radio');
  }, [creds]);

  const submit = async (s = server, u = unit, c = code) => {
    setError(null);
    const n = parseInt(u, 10);
    if (!Number.isFinite(n) || !c.trim()) {
      setError('Enter the radio ID and activation code');
      return;
    }
    setBusy(true);
    try {
      await activate(s, n, c);
      router.replace('/radio');
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  // arriving through a keyup://activate deep link: activate straight away
  useEffect(() => {
    if (params.s && params.u && params.c && !handled.current) {
      handled.current = true;
      void submit(params.s, params.u, params.c);
    }
  }, [params.s, params.u, params.c]); // eslint-disable-line react-hooks/exhaustive-deps

  const startScan = async () => {
    if (!perm?.granted) {
      const r = await requestPerm();
      if (!r.granted) {
        setError('Camera permission is needed to scan the QR code');
        return;
      }
    }
    handled.current = false;
    setScanning(true);
  };

  if (scanning) {
    return (
      <View style={{ flex: 1, backgroundColor: '#000' }}>
        <CameraView
          style={{ flex: 1 }}
          facing="back"
          barcodeScannerSettings={{ barcodeTypes: ['qr'] }}
          onBarcodeScanned={({ data }) => {
            if (handled.current) return;
            const a = parseActivationUri(data);
            if (!a) return;
            handled.current = true;
            setScanning(false);
            setServer(a.server);
            setUnit(String(a.unit));
            setCode(a.code);
            void submit(a.server, String(a.unit), a.code);
          }}
        />
        <Pressable style={[s.btn, s.ghost, { margin: 24 }]} onPress={() => setScanning(false)}>
          <Text style={s.btnText}>Cancel</Text>
        </Pressable>
      </View>
    );
  }

  return (
    <KeyboardAvoidingView style={{ flex: 1 }} behavior={Platform.OS === 'ios' ? 'padding' : undefined}>
      <ScrollView contentContainerStyle={s.wrap} keyboardShouldPersistTaps="handled">
        <Image source={require('../../assets/logo.png')} style={s.logo} accessibilityLabel="KeyUp" />
        <Text style={s.brand}>
          KEY<Text style={{ color: C.accent }}>UP</Text>
        </Text>
        <Text style={s.muted}>
          This phone becomes a subscriber radio on your system. Ask your administrator for an activation QR code.
        </Text>
        <Pressable style={[s.btn, s.primary]} onPress={startScan} disabled={busy}>
          <Text style={[s.btnText, { color: '#04121c' }]}>Scan activation QR code</Text>
        </Pressable>
        <Text style={[s.muted, { textAlign: 'center', marginVertical: 8 }]}>or enter it manually</Text>
        <Text style={s.label}>Server</Text>
        <TextInput style={s.input} value={server} onChangeText={setServer} autoCapitalize="none" autoCorrect={false}
          keyboardType="url" placeholder="https://lmr.example.com" placeholderTextColor={C.muted} />
        <Text style={s.label}>Radio ID</Text>
        <TextInput style={s.input} value={unit} onChangeText={setUnit} keyboardType="number-pad" placeholder="1001"
          placeholderTextColor={C.muted} />
        <Text style={s.label}>Activation code</Text>
        <TextInput style={s.input} value={code} onChangeText={setCode} autoCapitalize="characters" autoCorrect={false}
          placeholder="ABCD2345" placeholderTextColor={C.muted} />
        {error && <Text style={s.error}>{error}</Text>}
        <Pressable style={[s.btn, s.ghost]} onPress={() => submit()} disabled={busy}>
          {busy ? <ActivityIndicator color={C.accent} /> : <Text style={s.btnText}>Activate</Text>}
        </Pressable>
      </ScrollView>
    </KeyboardAvoidingView>
  );
}

const s = StyleSheet.create({
  wrap: { padding: 24, gap: 6 },
  logo: { width: 88, height: 88, marginBottom: 12 },
  brand: { color: C.text, fontSize: 32, fontWeight: '700', letterSpacing: 6, marginBottom: 8 },
  muted: { color: C.muted, fontSize: 14, lineHeight: 20 },
  label: { color: C.muted, fontSize: 12, marginTop: 10, textTransform: 'uppercase', letterSpacing: 1 },
  input: {
    backgroundColor: C.panel, color: C.text, borderColor: C.line, borderWidth: 1, borderRadius: 8,
    paddingHorizontal: 12, paddingVertical: 12, fontSize: 16,
  },
  btn: { borderRadius: 10, paddingVertical: 15, alignItems: 'center', marginTop: 14 },
  primary: { backgroundColor: C.accent },
  ghost: { borderWidth: 1, borderColor: C.line, backgroundColor: C.panel2 },
  btnText: { color: C.text, fontSize: 16, fontWeight: '600' },
  error: { color: C.bad, marginTop: 10 },
});
