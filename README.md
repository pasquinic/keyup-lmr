# KeyUp

**Distributed LMR trunking. Key up anywhere.**

KeyUp is a management and orchestration layer for a personal LMR network built on [DVMProject](https://github.com/DVMProject). DVM's programs carry the radio traffic: `dvmhost` at each site, `dvmfne` as the network core, and `dvmbridge` for analog, SIP and cross-mode audio. KeyUp provides:
- a web UI where you define the whole system once;
- zero-touch Raspberry Pi edges over WireGuard;
- config generation for every DVM process;
- live monitoring;
- SIP interconnect;
- site trunking status.

> **Use restrictions (from DVMProject):** DVM is for personal, non-commercial, hobbyist use only. It must never be used in public safety or life safety critical applications. KeyUp does not start any DVM process until you acknowledge this on the **DVM Network** page, which sets DVM's `iAgreeNotToBeStupid` flag.

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
3. Under **Edge Devices**, generate an enrollment token.

## Deploy an edge (Raspberry Pi 4/5, Raspberry Pi OS Bookworm 64-bit)

```bash
curl -fsSL https://lmr.example.com/edge/install.sh | sudo bash -s -- --server https://lmr.example.com --token <TOKEN>
```

The installer:
- builds `dvmhost` and `dvmbridge` from DVMProject at the pinned commit (about 20–40 minutes on a Pi 4);
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
PYTHONPATH=.:central pytest -q tests                                   # unit tests (config compiler etc.)
DVM_BIN_DIR=/path/to/dvmhost/build python3 tests/lab_dvm_e2e.py        # real DVM binaries, null modems
python3 tests/lab_e2e.py                                               # legacy native backend
```

`lab_dvm_e2e.py` runs KeyUp, `dvmfne`, three `dvmbridge`s and two edges supervising three `dvmhost`s, all on one machine. It checks that:
- every generated config is accepted;
- the P25 voice channel attaches to its control channel;
- SIP audio becomes a P25 call that KeyUp records;
- the patch re-vocodes that call onto DMR;
- an FNE outage puts both sites into site trunking, and they return to wide area afterwards.

## Notes and limitations

- **DVM version:** pinned to DVMProject/dvmhost `435ff45`. Bump `DVM_REF` in `dvm/Dockerfile`, `edge/Dockerfile` and `edge/install.sh` together. KeyUp runs DVM's programs unmodified and talks to them through config files, REST and UDP; DVM remains GPL-2.0.
- **Modes:** DVM supports P25 Phase I, DMR and NXDN. **P25 Phase II is not supported**, and KeyUp flags any Phase II channel. `dvmbridge` speaks DMR, P25 and analog, so NXDN can't be a SIP or patch endpoint.
- **WACN:** DVM refuses WACN `BEE00`, so KeyUp's default is `BB800` and it warns if `BEE00` is set.
- **Trunked sites:** one Pi per trunked site. The control channel and its voice channels coordinate over localhost RPC.
- **Native backend:** the original KeyUp call router is still available (`call_backend = native` in System Settings) for setups without DVM modems.
- **Not yet tested:** the Docker images and the Pi installer's DVM build were written but not run here, because no Docker daemon or Pi was available. The DVM binaries were built from source and exercised in the lab test above. SIP was tested with a simulated Asterisk.
