<p align="center"><img src="assets/brand/keyup-logo.png" alt="KeyUp" width="520"></p>

# KeyUp

**Distributed LMR trunking. Key up anywhere.**

KeyUp is an independent, self-hosted LMR network. Raspberry Pi edges connect base station radios, or packet-voice repeaters, to a central call router over WireGuard. Dispatchers use a Windows console, field users can talk from a phone over cellular, and every call can be logged, recorded and shipped to a second server.

```
                ┌──────────────────────── Central (Ubuntu Server, Docker) ────────────────────────┐
 Browser ─HTTPS─► Caddy ─► central (FastAPI)                                                       │
 Phone (PoC) ───WSS──────►   ├─ Web UI + REST API + live feed                                      │
 Console ─WG or WSS──────►   ├─ Call router: floor, priority/emergency preemption, hang time,      │
                │            │   patches, zones, site limits, affiliation                          │
                │            ├─ Transcoder (G.711 ⇄ IMBE ⇄ AMBE+2, DVSI AMBE-3000 pool)            │
                │            ├─ Recorder + shipper (HTTPS / SIP / SIPREC)                          │
                │            ├─ WireGuard hub (wg-lmr) for edges and consoles                      │
                │            └─ SIP gateway ◄─ARI─► Asterisk                                       │
                │          PostgreSQL                                                              │
                └─────────────────▲─────────────────────────────────▲──────────────────────────────┘
                                  │ WireGuard (control WS + voice UDP)
                ┌─────────────────┴──────────────┐         ┌────────┴───────────────────────────┐
                │ Edge (Raspberry Pi)            │         │ Edge (Raspberry Pi)                │
                │  audio + COR/PTT radios        │   …     │  packet channels:                  │
                │  optional DVSI USB vocoder     │         │  DFSI · IP Site Connect · Cap+     │
                │  site trunking if central down │         │                                    │
                └──┬──────────┬──────────────────┘         └──┬───────────────┬─────────────────┘
               Base radio  Base radio (P25/DMR/NXDN/analog)  P25 station    MOTOTRBO repeaters
```

All code in this repository is KeyUp's own. DVMProject's published documentation was used as a feature reference only; no DVM code is included or required.

## What is implemented

**Call router** (`central/app/services/call_controller.py`)
- Every participant is a leg of one router: edge channels, packet channels, SIP calls, PoC phones and dispatch consoles.
- Floor control per talkgroup, with a busy reply. Priority preemption; emergency calls take priority.
- Hang time with talkback, maximum call time, radio ID permission checks, heartbeat supervision.
- Codec-tagged voice frames. P25 Phase II, DMR and NXDN share AMBE+2 and pass straight through. IMBE, G.711 and SIP go through PCM using the DVSI vocoder pool. Each frame is decoded once and encoded once per destination codec.

**Zones**
- A zone is an ordered list of talkgroups. Assign zones to subscriber radios and to dispatch consoles.
- Phones show a zone knob and a talkgroup list grouped by zone. Consoles show one tab per zone.
- Zones can be exported as CSV for programming radios.

**Dispatch with the Windows console** (`console/`, see `console/README.md`)
- A native Windows app (C#, .NET 8, WPF), one tab per zone and a tile per talkgroup: monitor with per-tile volume and a receive VU meter, select, per-tile PTT plus a PTT key, who-is-talking, emergency banner with acknowledge / clear, radio inhibit / uninhibit, multi-talkgroup patches, and a call and event log.
- **Priority markers:** star a talkgroup; while it is active, other talkgroups duck (6–24 dB, or mute).
- **Audio:** RX (mixed) and TX (mic) VU meters, master / selected / unselected / mic levels on the main screen. Selected and unselected audio each go to a device and an ear (left, right or both), and each talkgroup can override its ear. TX tones (talk permit, busy) and RX tones (emergency alert) can each be turned off.
- **WireGuard is built in.** Activation gives the console its own tunnel, run by a small Windows service (the **KeyUp Tunnel** service) using the official WireGuardNT driver, so no separate WireGuard install is needed and the console itself runs without administrator rights. If the tunnel isn't up, it falls back to HTTPS.
- Activated with a one-time code from **Dispatch Consoles** in the web UI. Secrets are stored with Windows DPAPI.
- Installer and portable zip are built by GitHub Actions (**Console · Windows build** workflow).

**PoC terminals for cellular push to talk** (`mobile/`, protocol in `docs/poc-protocol.md`)
- iOS / Android app (Expo / React Native). Activated by QR code from **Subscriber Radios → PoC QR**.
- A phone is a subscriber radio: same radio ID, permissions, zones, inhibit and emergency as RF.
- Scan list, emergency, and per-device revoke under **PoC Phones**.

**Talkgroup logging and audio recording**
- Every call is logged: talkgroup, source, all talkers with times, sites, duration, emergency flag, end reason.
- Talkgroups with **Record** on are recorded on the central server as μ-law with real-time gaps preserved. Without a vocoder, native IMBE / AMBE+2 frames are kept raw instead.
- **Call History & Recordings** page: filters, an audio player, CSV export, and per-call ship status with retry.
- Retention by days and by total size (System Settings).
- **Shipping to a second server** (System Settings → Recording):
  - `https`: POST the WAV plus JSON metadata to a URL, with a bearer token;
  - `sip`: place a SIP call to the recorder and play the call audio over RTP;
  - `siprec`: SIP with RFC 7865 recording metadata (talkgroup, talkers, times), for SIPREC recorders.
  - Shipping has a queue and retries. SIP uses KeyUp's own UA over UDP or TCP, with digest authentication.

**Packetized voice** (channel **Interface** on the Channels page)
- **DFSI** (P25 fixed station interface over UDP/RTP): IMBE voice with link control and encryption sync, heartbeat and connect / disconnect.
- **IP Site Connect**: the edge joins MOTOTRBO repeaters as a master or a peer, with authentication, on one or both timeslots. AMBE+2 voice with LC.
- **Capacity Plus**: the edge joins the system as a peer and carries the talkgroups listed in **Extra talkgroups** (or `*` for all).
- A packet channel is configured with `packet_config` (local port, remote host / port, peer ID, auth key, timeslots). The router sends each packet site its native codec, so DMR ⇄ NXDN ⇄ P25 Phase II need no vocoder.

**Edge** (`edge/`, Raspberry Pi)
- Zero-touch install with a one-time token; WireGuard set up automatically.
- Audio radios: COR in / PTT out by GPIO or serial RTS/CTS, USB sound cards, TX delay / tail, CW ID.
- Optional DVSI USB-3000 / ThumbDV for native IMBE / AMBE+2 on the network.
- **Site trunking** when central is unreachable: local repeat, data messages and alert tones to radios, and upload of the local call log on recovery.

**SIP interconnect** through Asterisk (ARI + external media): inbound by extension, outbound and permanent patches.

## System rules and network tools

- **Band plan:** define channel identities (base frequency, spacing, input offset). The frequency plan then shows each channel's identity and channel number, and flags transmit frequencies that are off-plan or have the wrong receive offset.
- **Talkgroup rules:**
  - **Restricted:** only radios that list the talkgroup may talk on it.
  - **Allowed sites:** the talkgroup is refused and not routed anywhere else.
  - **Affiliated only:** the talkgroup is repeated only at sites where a radio was recently heard on it. Edit the window under System Settings, `affiliation_timeout_s`.
  - **Parrot:** each transmission is echoed back to the talker's channel, for coverage and audio checks.
- **Talkgroup patches:** link two or more talkgroups so a call on any of them is heard on all. Patches chain (A⇄B, B⇄C), hold the floor on every linked talkgroup, keep each talkgroup's site limits, and cross modes, because the router transcodes per site. Dispatch consoles can create patches, add or remove talkgroups, and unpatch.
- **Network page:**
  - every connected edge, with live carrier and transmit state per channel;
  - the affiliation table;
  - configuration checks: unbound channels, GPIO clashes, off-plan frequencies, unknown sites in limits, bad patches, sites without an edge;
  - a read-only view of the config pushed to each edge.
- **CW ID:** set a callsign on a site and its edge identifies each transmitter in Morse after use, at most once per `cw_id_interval_s` (default 10 minutes).
- **Serial RTS/CTS radio interfaces:** per channel, choose **PTT / COR via serial port**. The edge then keys PTT with RTS and reads COR on CTS, with an optional invert, as with common USB radio interface cables. Audio still uses the channel's sound card.

## Deploy the central server (Ubuntu Server 22.04 / 24.04)

```bash
sudo apt install -y docker.io docker-compose-v2 wireguard
git clone https://github.com/pasquinic/keyup-lmr.git && cd keyup-lmr
cp .env.example .env && nano .env        # hostname, passwords, WG_ENDPOINT
docker compose up -d --build             # db + central + caddy
docker compose --profile sip up -d       # optional: Asterisk SIP (set ARI_ENABLED=true)

# firewall
sudo ufw allow 80,443/tcp
sudo ufw allow 51820/udp
sudo ufw allow in on wg-lmr to any port 8080,50000 proto tcp
sudo ufw allow in on wg-lmr to any port 50000 proto udp
sudo ufw allow 5060/udp && sudo ufw allow 10000:10999/udp   # only if SIP is exposed
sudo ufw enable
```

Open `https://<PUBLIC_HOSTNAME>`, sign in with `ADMIN_USER` / `ADMIN_PASSWORD` and change the password under System Settings. Then set up the system in this order: **Sites → Talkgroups → Zones → Channels → Subscriber radios → Edge Devices (generate a token) → PoC Phones / Dispatch Consoles**.

**Updating an existing install** (including one that ran the DVMProject-based build):

```bash
git pull && docker compose up -d --build --remove-orphans
```

`--remove-orphans` removes the old `dvm` container. The database is upgraded in place on start: new columns are added, and the DVM-era band plan and patches are copied into the native tables.

**Firewall additions for the new features:** PoC phones and the console's HTTPS mode use port 443 only. Consoles in WireGuard mode use UDP 51820 like the edges. If recordings are shipped over SIP, allow the recorder's SIP and RTP traffic outbound.

Recordings are stored under `./data/recordings/` on the host.

## Deploy an edge (Raspberry Pi 4/5, Raspberry Pi OS Bookworm 64-bit)

Copy the install command shown when you generate a token, and run it on the Pi:

```bash
curl -fsSL https://lmr.example.com/edge/install.sh | sudo bash -s -- --server https://lmr.example.com --token <TOKEN>
```

If the token is not auto-approve, approve the device on the Edge Devices page. To run the edge as a container instead, use `edge/docker-compose.yml`. Bare metal gives the lowest audio latency.

**Wiring for each base radio** (one channel = one radio = one USB sound card):

| Signal | Pi side | Radio side |
|---|---|---|
| RX audio | USB sound card mic/line in | Discriminator or line out (accessory port) |
| TX audio | USB sound card out | Mic / line in |
| COR/COS | GPIO input (BCM, default 27), through an opto-isolator | Carrier/squelch detect |
| PTT | GPIO output (BCM, default 17) → transistor/opto | PTT line |

With a USB radio interface cable you can use **serial signalling** instead: RTS drives PTT and CTS reads COR.

Set `audio_device` to an ALSA `plughw:` device so ALSA resamples to 8 kHz. Pins and sound cards are configured per channel in the web UI.

**Packet channels** need no audio wiring. Put the edge on the same LAN as the station or repeaters, set the channel's **Interface** to DFSI, IPSC or Cap+, and fill in its packet settings. The edge then speaks that protocol on UDP.

## Dispatch console

1. Download the console from GitHub → **Actions → Console · Windows build** → latest run → **Artifacts** (installer or portable zip), and run the setup. It installs the console and the KeyUp Tunnel service (administrator rights are asked for once).
2. In the web UI, open **Dispatch Consoles**, add a console (its radio ID and zones) and press **Activate**.
3. Start the console, choose **Connect to KeyUp…**, and enter the server address and the code. The header shows whether it is on WireGuard or HTTPS.

More in `console/README.md`.

## Test without hardware

```bash
pip install -r central/requirements.txt -r edge/requirements.txt pytest
PYTHONPATH=.:central pytest -q tests            # protocol, G.711, DVSI framing, zones, recording, DFSI/IPSC codecs
python3 tests/lab_e2e.py                        # central + 2 simulated sites, full scenario incl. site trunking
python3 tests/lab_rules_e2e.py                  # patches, site limits, parrot, affiliated-only, restricted, CW ID
python3 tests/lab_features_e2e.py               # zones, PoC, recording + SIP/SIPREC/HTTPS shipping, dispatch console
python3 tests/lab_packet_e2e.py                 # DFSI, IP Site Connect and Capacity Plus against simulators
```

The labs use simulators in `tools/`: a pty AMBE-3000 stand-in, DFSI and IPSC stations, a SIP recorder, and PoC and console clients. `lab_features_e2e.py` also runs the console's C# core against the server when the .NET SDK is installed. All of these run in CI.

## Repository layout

```
lmrcommon/        shared: voice frames, modes/codecs, G.711, DVSI driver + pool, DFSI, IPSC, P25 Reed-Solomon
central/app/      FastAPI server: models, API, call router, transcoder, zones, PoC, dispatch, recorder,
                  shipper, SIP UA, WireGuard, SIP gateway, web UI
edge/lmr_edge/    Pi agent: enrollment, links, GPIO/ALSA and serial radios, packet channels, site trunking
console/          Windows dispatch console (C#/.NET 8), tunnel service, Inno Setup installer
assets/brand/     KeyUp logo (SVG masters + PNG renders)
mobile/           PoC app (Expo / React Native)
asterisk/         Asterisk container + PJSIP/ARI config
deploy/Caddyfile  TLS reverse proxy
tools/            simulators and test clients
tests/            unit tests + end-to-end labs
docs/             PoC protocol
```

## Important limitations

- **Packet voice interoperability is untested on real equipment.** The DFSI, IP Site Connect and Capacity Plus byte layouts are written from public and community descriptions and tested only against KeyUp's own simulators. Expect to adjust details against your stations and repeaters. Each protocol is isolated in `lmrcommon/dfsi.py` and `lmrcommon/ipsc.py`. Capacity Plus rest-channel and data features are not implemented; voice only.
- **IMBE and AMBE+2 are proprietary DVSI vocoders.** KeyUp drives licensed DVSI AMBE-3000 hardware and has no software codec.
  - Without DVSI hardware, calls that need IMBE ⇄ AMBE+2 or ⇄ analog conversion are dropped for that destination (the dashboard shows this), and recordings keep raw frames.
  - The rate settings in `lmrcommon/dvsi.py` (`DEFAULT_RATES`) must be checked against your chip's manual. The test vocoder in `tools/fake_dvsi.py` is only a stand-in.
- **Audio radios are conventional repeaters.** The Pi works through audio plus COR/PTT and does not generate a trunking control channel. Affiliations are inferred from radios being heard.
- **The Windows console** was built and its core tested in CI, but the WireGuard tunnel and audio devices have not been tried on a real PC here.
- **SIP and SIPREC** were tested against a simulated recorder and simulated ARI, not a commercial recorder or live Asterisk.
- **Security:** change every default in `.env`, keep port 8080 reachable only inside the tunnel, and put the UI behind your SSO/VPN if possible. AES/ADP encryption is not implemented.
- **Not for public safety or life safety use.**

## License

GPL-2.0 (see `LICENSE` and `NOTICE`). The console installer bundles the unmodified WireGuardNT driver library under its own license.
