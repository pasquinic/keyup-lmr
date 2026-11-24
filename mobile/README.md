# KeyUp PoC — Android & iOS push-to-talk app

KeyUp PoC turns a phone into a **subscriber radio** on your KeyUp / DVMProject system, working over cellular or Wi-Fi. The phone uses a radio ID from **Subscriber Radios** and follows the same rules as an RF radio with that ID:
- permitted talkgroups;
- inhibit;
- the FNE's radio ID lists;
- transmit timeout.

It is a single React Native (Expo SDK 58) app for both platforms.

## Features

- Activation by QR code, deep link, or by typing the server, radio ID and code.
- A radio-style screen:
  - talkgroup display and channel knob;
  - a large **push-to-talk** button with talk-permit, busy and deny tones and haptics;
  - the talker's alias and ID shown on receive, plus the last unit heard;
  - **scan** of up to 16 talkgroups, with the selected talkgroup as priority;
  - **emergency**: hold for 2 s to declare or clear it, which raises the dispatcher alarm and alerts the talkgroup.
- **Audio:** 8 kHz G.711 μ-law over a TLS WebSocket, about 70 kbit/s while talking or receiving and close to zero when idle. Received audio has a 60 ms jitter buffer, and the voice-chat audio session supports Bluetooth HFP.
- **Background operation:**
  - Android: an audio foreground service with a media notification;
  - iOS: the `audio` background mode;
  - on both, the headset or Bluetooth media button can toggle PTT.
- Reconnects automatically after cell handovers and restores the selected talkgroup and scan list. A phone call interrupts and releases PTT.

## Build

The app uses native modules (`react-native-audio-api`, `expo-camera`), so build it with a **development or production build**; Expo Go can't run it.

```bash
cd mobile
npm install
npx expo run:android            # local build onto a connected device (Android SDK)
npx expo run:ios                # local build (Xcode on macOS)
# or in the cloud, no local Xcode/Android Studio:
npx eas-cli@latest build --platform all --profile production
```

The bundle and package IDs are `com.pasquinic.keyup`; change them in `app.json` before you publish. Native projects are generated from `app.json` and its config plugins, so don't edit `ios/` or `android/` by hand.

## Activate a phone

1. In the KeyUp web UI, go to **Subscriber Radios** and press **PoC QR** on the radio ID.
2. In the app, tap **Scan activation QR code**. You can also open the `keyup://` link on the phone, or type the server, radio ID and code.
3. To retire a phone, open **PoC Phones** in the web UI and press **Revoke**.

The server must use the DVM backend, with the DVM license acknowledged. Each talkgroup a phone may use needs **Selectable by PoC phones** enabled (the default).

## Develop and test

```bash
npm run typecheck && npx eslint src && npm test     # unit tests: codec, protocol, client state machine
npx tsx scripts/live-check.ts https://lmr.example.com <device-token> 2   # real client vs. a live server
```

`../tests/lab_dvm_e2e.py` runs `live-check.ts` against a full lab: KeyUp, the DVM FNE, bridges and simulated sites.

## Code map

| File | What it does |
|---|---|
| `src/client.ts` | Protocol state machine: auth, PTT with audio held until the grant, receive, scan, emergency, reconnect. Has no React dependency |
| `src/audio.ts` | Microphone capture, resampling to 8 kHz, μ-law encoding, playback queue, tones, audio session, background notification |
| `src/protocol.ts`, `src/g711.ts`, `src/dsp.ts` | Wire format, the G.711 codec (byte-identical to the server's) and framing |
| `src/session.tsx` | Credentials in SecureStore, settings, and the client and audio lifecycle |
| `src/app/*` | Expo Router screens: `activate`, `radio`, `settings` |

## Limitations and next steps

- **iOS background:** iOS can still suspend a background app's network socket once audio is idle. For reliable background reception, the next step is Apple's **PushToTalk framework** (iOS 16+). It needs the PTT entitlement and APNs PTT pushes from the server.
- **Hardware PTT buttons** on rugged Android phones (Sonim, Kyocera and others) need a small native module that listens for the vendor's PTT intents. For now only the headset media button is supported.
- **Codec:** G.711 is used for simplicity. Opus would reduce cellular data use about 5× and would need an Opus encoder on the phone and decoding on the server.
- **Not yet tested on hardware:** typecheck, lint, unit tests and the protocol client against a live server all pass. The app hasn't run on a physical phone yet, because the build environment had no Android SDK or Xcode.
