# KeyUp

**Distributed LMR trunking. Key up anywhere.**

KeyUp is a trunked land-mobile-radio system for a personal network. It has three parts in one repository:

| Part | Where | What it is |
|---|---|---|
| **Central + edges** | `central/`, `edge/`, `lmrcommon/` | Web management, WireGuard hub, enrollment, config compiler, SIP interconnect and dashboard; Raspberry Pi edge agent with site trunking |
| **KeyUp DVM fork** | `dvm/dvmhost/` | A maintained fork of [DVMProject/dvmhost](https://github.com/DVMProject/dvmhost): `dvmfne`, `dvmhost`, `dvmbridge`, `dvmcmd` (P25 Phase I, DMR, NXDN, analog) |
| **KeyUp PoC app** | `mobile/` | Android and iOS push-to-talk app; each phone is a subscriber radio with the same rules as an RF radio |

Two call backends are available, chosen in **System Settings → `call_backend`**:

- **`dvm`** (default): the KeyUp DVM fork carries the traffic. KeyUp compiles your configuration into DVM's config files and supervises the daemons.
- **`native`**: KeyUp's own Python router carries the traffic between edges, for analog or lab setups without DVM modems. Talkgroup rules, patches, band plan, CW ID and serial interfaces work in both.

> **Use restrictions (from DVMProject):** DVM is for personal, non-commercial, hobbyist use only. It must never be used in public safety or life safety critical applications. KeyUp does not start any DVM process until you acknowledge this on the **DVM Network** page, which sets DVM's `iAgreeNotToBeStupid` flag. The fork keeps this flag and DVM's other safeguards unchanged.

```
                    ┌───────────────────────── Central (Ubuntu Server, Docker) ─────────────────────────┐
 Browser ─HTTPS─► Caddy ─► KeyUp central (FastAPI + Postgres)                                           │
                    │        ├─ web UI: sites, channels, band plan, talkgroups, radios, patches, SIP      │
                    │        ├─ compiles the DB into DVM configs ──► ./data/dvm/instances/*             │
                    │        ├─ FNE monitor: REST (peers, affiliations) + log tail (live calls, history)  │
                    │        ├─ WireGuard hub + device enrollment, edge control link                     │
                    │        └─ SIP gateway (DTMF/VOX floor) ◄─RTP─► per-TG dvmbridge                    │
                    │      dvm container: supervisor ─► dvmfne · dvmbridge (SIP TGs) · dvmbridge pairs   │
                    │                                    (cross-mode patches, e.g. DMR TG1 ⇄ P25 TG100)  │
                    │      Asterisk (PJSIP + ARI) ◄─SIP─► phones / PBX / carrier                          │
                    └───────────────────────────────▲────────────────────────────────────────────────────┘
                                 WireGuard tunnel   │  DVM peer link (UDP 62031) + KeyUp control link
              ┌─────────────────────────────────────┴──────────┐
              │ Edge: Raspberry Pi  (lmr-edge agent)           │   one per site
              │   pulls its site's DVM configs from central    │
              │   supervises dvmhost per RF channel            │──► DVM-V1 / hotspot modem ─► repeater
              │            dvmbridge per analog radio          │──► sound card + serial RTS/CTS ─► radio
              │   reports health; caches config for outages    │
              └────────────────────────────────────────────────┘
```

## The KeyUp DVM fork

`dvm/dvmhost/` is DVMProject's dvmhost source at upstream commit `435ff45`, vendored into this repository and built by KeyUp's Dockerfiles, the Pi installer and CI. KeyUp's changes are small and marked `// KeyUp fork`:

- **Structured call events:** when the FNE runs with `KEYUP_EVENTS_FILE` set, every P25, DMR, NXDN and analog call start and end is appended as a JSON line (source, destination, peer, duration, stream). KeyUp's FNE monitor reads this feed instead of parsing log text; the **DVM Network** page shows `event feed: keyup-fork`. Upstream DVM binaries still work through the log-parse fallback.
- **Version tag:** binaries report `… keyup.1`. The network protocol version is unchanged, so the fork interoperates with upstream DVM peers.

`dvm/FORK.md` lists every change and how to sync with upstream. The central server serves the fork source to edges at `/edge/dvm-src.tar.gz`, so every site builds the same code.

## How the pieces map

| You configure in KeyUp | KeyUp generates for DVM |
|---|---|
| Sites (number, RFSS, callsign, location) | `system.config.siteId` / `rfssId`, CW ID and info in each `dvmhost` config |
| Band plan entries | `iden_table.dat`. Each channel's TX frequency becomes a `channelId` and `channelNo`, and the RX offset is checked |
| Channels: P25 Phase I / DMR / NXDN, *control* or *voice* role, modem port, levels | One `dvmhost` per channel, with a peer ID and password. A site with a control-role channel becomes a **trunked site**: the CC lists its voice channels, and the voice channels notify the CC over RPC |
| Analog channels | One `dvmbridge` per radio: local audio, plus serial RTS for PTT and CTS for COR. It speaks the talkgroup's mode |
| Talkgroups (slot, affiliated-only, restricted, parrot, site limits) | `talkgroup_rules.yml`: `rid_permitted`, `inclusion`, `affiliated`, `parrot` |
| Subscriber radios (enabled, permitted TGs) | `rid_acl.dat`; optionally reject unknown radio IDs |
| Edge devices and channels | `peer_list.dat`: the FNE only accepts peers KeyUp knows about |
| Control channels across sites | `adj_site_map.yml`, so radios can roam between neighbor sites |
| SIP-enabled talkgroups, and talkgroups PoC phones may use | One interconnect `dvmbridge` per talkgroup (raw PCM over UDP, with the source radio ID in metadata), shared by the SIP gateway and the PoC gateway |
| Cross-mode patches | Two `dvmbridge`s connected back to back, which re-vocode, for example DMR ⇄ P25 |

Saving anything in the UI regenerates these files. The FNE hot-reloads lookup changes through its REST API, and the supervisors restart a daemon only if its main config changed. Each site's files are pushed to its edge over the tunnel.

**Site trunking** is handled by DVM itself. When a site's `dvmhost` loses the FNE, it keeps repeating locally and clears the *network active* flag in its P25 (and NXDN/DMR) site broadcasts, so trunked subscriber radios show **SITE TRUNKING**. `updateLookups` and `saveLookups` are enabled, so the site keeps the last talkgroup and radio ID lists. The KeyUp edge agent detects the lost FNE link, reports the site as site trunking on the dashboard, and can drive a GPIO indicator or webhook.

## PoC phones: cellular subscriber radios

The **KeyUp PoC** app (`mobile/`, one React Native / Expo codebase for Android and iOS) makes a phone a subscriber radio over cellular or Wi-Fi. The phone uses a radio ID from **Subscriber Radios** and is held to the same rules as an RF radio:
- **Permitted talkgroups:** only those the radio ID is allowed on; restricted talkgroups only if the ID lists them.
- **Inhibit:** disabling the Subscriber logs the phone out at once.
- **Radio ID rules on the FNE:** the phone's transmissions reach the FNE carrying its own radio ID, through a per-talkgroup interconnect `dvmbridge`, so the FNE's radio ID lists and permitted-ID checks apply.
- **Busy and timeout:** busy rules and the system's maximum call time apply.
- **Emergencies:** raise the dispatcher alarm.

To provision a phone, press **PoC QR** on a subscriber, then scan the QR code in the app. Phones are managed on the **PoC Phones** page. Details are in `mobile/README.md` and `docs/poc-protocol.md`.

### Building the apps

| Build | How | Output |
|---|---|---|
| Android APK | GitHub Actions **Mobile · Android build** (on every push to `mobile/`, or run manually) | `KeyUp-PoC.apk` artifact; signed with your key if the `ANDROID_KEYSTORE_*` secrets are set, otherwise debug-signed for side-loading |
| iOS Simulator | GitHub Actions **Mobile · iOS build** (macOS runner) | `KeyUp-PoC-simulator.zip` artifact |
| Signed iOS + Android | GitHub Actions **Mobile · EAS build** (manual; needs an `EXPO_TOKEN` secret) or `npx eas-cli build` | Installable IPA/APK, or store bundles with the `production` profile |
| Local | `cd mobile && npx expo run:android` / `npx expo run:ios` | Debug build on a connected device |

Installing on iPhones requires an Apple Developer account; EAS handles the certificates. One-time EAS setup is in `mobile/README.md`.

## System rules and network tools

With the **native** backend these are enforced by KeyUp's own router. With the **DVM** backend the same settings compile into the FNE's `talkgroup_rules.yml`, `iden_table.dat` and bridge pairs, so you configure them once either way.

- **Band plan:** define channel identities (base frequency, spacing, input offset). The frequency plan then shows each channel's identity and channel number, and flags transmit frequencies that are off-plan or have the wrong receive offset.
- **Talkgroup rules:**
  - **Restricted:** only radios that list the talkgroup may talk on it.
  - **Allowed sites:** the talkgroup is refused and not routed anywhere else.
  - **Affiliated only:** the talkgroup is repeated only at sites where a radio was recently heard on it. Edit the window under System Settings, `affiliation_timeout_s`.
  - **Parrot:** each transmission is echoed back to the talker's channel, for coverage and audio checks.
- **Talkgroup patches:** link two talkgroups so a call on either is heard on both. Patches chain (A⇄B, B⇄C), hold the floor on every linked talkgroup, and cross modes, because the router transcodes per site.
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
cp .env.example .env && nano .env             # hostname, passwords, WG_ENDPOINT, FNE_REST_PASSWORD
docker compose up -d --build                  # db + central + dvm (FNE) + caddy
docker compose --profile sip up -d            # optional Asterisk (set ARI_ENABLED=true)

sudo ufw allow 80,443/tcp && sudo ufw allow 51820/udp
sudo ufw allow in on wg-lmr to any port 8080,50000 proto tcp
sudo ufw allow in on wg-lmr to any port 62031 proto udp       # DVM peers only inside the tunnel
sudo ufw enable
```

Then, in the UI:
1. Go to **DVM Network** and acknowledge the license.
2. Set up the system in this order: **Band Plan → Sites → Talkgroups → Channels → Subscriber Radios**.
   To run without DVM modems, set **System Settings → `call_backend` = `native`** instead.
3. Under **Edge Devices**, generate an enrollment token.

## Deploy an edge (Raspberry Pi 4/5, Raspberry Pi OS Bookworm 64-bit)

```bash
curl -fsSL https://lmr.example.com/edge/install.sh | sudo bash -s -- --server https://lmr.example.com --token <TOKEN>
```

The installer:
- downloads the KeyUp DVM fork source from your central server and builds `dvmhost`, `dvmbridge` and `dvmcmd` (about 20–40 minutes on a Pi 4);
- installs the agent;
- enrolls the Pi;
- brings up WireGuard.

Add `--gpio-modem` if a DVM modem HAT uses the Pi's UART. A USB modem shows up as `/dev/ttyACM0`. A container build is in `edge/Dockerfile`.

You set each channel's modem in the UI:
- **UART + air** for a DVM-V1 or hotspot;
- **UART + DFSI** for a Motorola V.24 station through a DVM-V24;
- **Null** for lab testing without RF.

## Tests

```bash
pip install -r central/requirements.txt -r edge/requirements.txt pytest pyyaml
PYTHONPATH=.:central pytest -q tests        # unit tests (config compiler, rules, supervisor)
python3 tests/lab_e2e.py                     # native backend: edges, routing, site trunking
python3 tests/lab_rules_e2e.py               # native backend: talkgroup rules, patches, CW ID, band plan

# DVM backend against the fork's binaries
(mkdir -p dvm/dvmhost/build && cd dvm/dvmhost/build && cmake .. -DENABLE_TUI_SUPPORT=off && make -j4 dvmfne dvmhost dvmbridge dvmcmd)
(cd mobile && npm ci --legacy-peer-deps)
DVM_BIN_DIR=$PWD/dvm/dvmhost/build python3 tests/lab_dvm_e2e.py

cd mobile && npm run typecheck && npx eslint src && npm test   # app
```

`lab_dvm_e2e.py` runs KeyUp, `dvmfne`, three `dvmbridge`s and two edges supervising three `dvmhost`s, all on one machine. It checks that:
- every generated config is accepted and the P25 voice channel attaches to its control channel;
- SIP audio becomes a P25 call that KeyUp records, and the patch re-vocodes it onto DMR;
- PoC phones transmit with their own radio ID, hear radio traffic, get busy and not-permitted denials, and are cut off when inhibited; the app's TypeScript client keys up against the live server;
- the fork's structured call-event feed is active;
- an FNE outage puts both sites into site trunking, and they return to wide area afterwards.

GitHub Actions (`.github/workflows/ci.yml`) runs all of these on every push.

## Notes and limitations

- **Modes:** DVM supports P25 Phase I, DMR and NXDN. **P25 Phase II is not supported** by DVM, and KeyUp flags any Phase II channel in DVM mode (the native backend treats it as a mode label only). `dvmbridge` speaks DMR, P25 and analog, so NXDN can't be a SIP, PoC or patch endpoint.
- **WACN:** DVM refuses WACN `BEE00`, so KeyUp's default is `BB800` and it warns if `BEE00` is set.
- **Trunked sites:** one Pi per trunked site. The control channel and its voice channels coordinate over localhost RPC.
- **PoC** needs the DVM backend.
- **Not yet tested here:** the Docker images, the Pi installer's build and the app store builds were written but not run in this environment (no Docker daemon, Pi, Android SDK or macOS). The fork binaries were built from source and exercised by the lab tests above, and SIP was tested with a simulated Asterisk.

## License

KeyUp is licensed under the **GNU General Public License v2.0** (`LICENSE`), because it includes and modifies DVMProject's GPL-2.0 code. DVMProject's copyright notices are kept in every file of the fork; see `NOTICE` and `dvm/FORK.md`.
