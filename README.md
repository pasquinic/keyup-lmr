# KeyUp

**Distributed LMR trunking. Key up anywhere.**


A distributed LMR trunking system. Raspberry Pi edge devices connect base station radios to a central management and call processing server over WireGuard. The central server runs on Ubuntu Server in Docker.

```
                         ┌──────────────────────── Central (Ubuntu Server, Docker) ───────────────────────┐
  Browser ──HTTPS──► Caddy ─► central (FastAPI)                                                            │
                         │      ├─ Web UI: dashboard, sites, enrollment, channels/frequencies, TGs, radios │
                         │      ├─ REST API + live WebSocket feed                                          │
                         │      ├─ Call controller: floor control, priority/emergency preemption, hang time│
                         │      ├─ Voice router (UDP :50000) + multi-mode transcoder (DVSI AMBE-3000 pool)  │
                         │      ├─ WireGuard hub (wg-lmr, 10.77.0.0/16), enrollment + approval              │
                         │      └─ SIP gateway ◄─ARI/RTP─► Asterisk (PJSIP) ◄─SIP─► phones / PBX / carrier  │
                         │    PostgreSQL                                                                   │
                         └──────────────▲──────────────────────────────▲──────────────────────────────────┘
                                        │ WireGuard (control WS + voice UDP)
               ┌────────────────────────┴───────┐              ┌───────┴────────────────────────┐
               │ Edge: Raspberry Pi (site 1)    │              │ Edge: Raspberry Pi (site N)    │
               │  lmr-edge agent                │      …       │                                │
               │  COR in / PTT out (GPIO)       │              │  site trunking when central    │
               │  RX/TX audio (USB sound card)  │              │  is unreachable                │
               │  optional DVSI USB vocoder     │              │                                │
               └──┬──────────┬──────────┬───────┘              └────────────────────────────────┘
               Base radio  Base radio  Base radio   (P25 Ph I / Ph II, DMR, NXDN-NEXEDGE, analog)
```

## What is implemented

**Central management server** (`central/`)
- **Web UI** (dark ops console, works on mobile) with these pages:
  - **Dashboard:** live calls with timers and emergency highlighting, event feed, site status tiles (online / site trunking / offline), calls per hour, busiest talkgroups, and the status of transcoding, call processing, WireGuard and SIP.
  - **Subscriber radios:** radio ID, alias, mode and model. Each radio is assigned to a site/edge device and has permitted talkgroups. You can inhibit a radio, see where it was last seen, and import radios from CSV.
  - **Talkgroups:** priority, hang time, whether emergency calls are allowed, and SIP access by extension.
  - **Channels and frequency plan:** each base radio's RX/TX frequency, mode, NAC / color code / timeslot / RAN / CTCSS, bound talkgroup, and the edge's GPIO pins, sound card, TX delay/tail and gains. It flags TX frequencies that are reused.
  - **Edge devices and enrollment:** one-time tokens with an install command, auto-approve or manual approval, revoke, test tone, and per-device WireGuard handshake, traffic and telemetry.
  - **SIP interconnect:** inbound calls by extension, outbound patches, and permanent patches.
  - **Call history**, and **system settings** (WACN / system ID, site trunking timers and messages, and more).
- **Call processing:**
  - Floor control per talkgroup, with a busy reply when the talkgroup is in use.
  - Priority preemption, and emergency calls take priority.
  - **Hang time with talkback:** destinations release PTT when the talker unkeys, so a reply from any site picks up the same call.
  - Checks that a radio is permitted on the talkgroup, a maximum call duration, and heartbeat supervision of edges.
- **Multi-mode transcoding:**
  - The network carries vocoder voice parameters without FEC.
  - **P25 Phase II, DMR and NXDN/NEXEDGE all use AMBE+2**, so calls between them pass straight through with no tandem vocoding.
  - P25 Phase I (IMBE), analog/G.711 and SIP are converted through PCM using a pool of DVSI AMBE-3000 USB vocoders. Each frame is decoded once and encoded once per destination codec.
- **WireGuard hub:**
  - Each edge generates its own key on the Pi; only the public key is sent.
  - Each edge gets a /32 address.
  - Control and voice are only accepted from the edge's tunnel IP. Voice also needs a per-session HMAC keepalive.
- **SIP:** Asterisk with ARI and externalMedia, using G.711 RTP. Phones press `*` to talk and `#` to release, or you can use VOX instead.

**Edge base processing** (`edge/`, Raspberry Pi)
- **Zero-touch install:** `curl …/edge/install.sh | sudo bash -s -- --server … --token …` enrolls the Pi, writes `/etc/wireguard/lmr0.conf`, enables `wg-quick` and installs a systemd service.
- **COR handling:** COR is read from GPIO. On COR, the edge requests the floor, buffers about 0.5 s of audio until the grant, then encodes and streams.
- **PTT handling:** on an incoming call it keys PTT with TX delay and tail. It never keys over a local talker.
- **Vocoding:** G.711 in software, or native IMBE/AMBE+2 with a DVSI USB-3000/ThumbDV on the Pi. Native codecs cut tunnel bandwidth by about 55 kbps per call.
- **Central config cached locally:** channels, talkgroups and subscribers are pushed from central and cached on the edge.
- **Site trunking:**
  - **Trigger:** central has been unreachable for `site_trunking_timeout_s` (default 10 s).
  - **Local repeat:** the site keeps working, cross-patching local channels that share a talkgroup and checking permissions from the cache.
  - **Notifying subscriber radios:**
    - A data message (`SITE TRUNKING`) to each talkgroup as a group message, and/or to each subscriber assigned to that site.
    - The site trunking alert tone on entry and after each local call, re-announced every 5 minutes by default.
    - An optional GPIO indicator and a webhook.
  - **On recovery:** subscribers get `WIDE AREA RESTORED`, and the call records logged locally during site trunking are uploaded to central history.
- **Local troubleshooting API:** `curl localhost:8090/status`.

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

Open `https://<PUBLIC_HOSTNAME>`, sign in with `ADMIN_USER` / `ADMIN_PASSWORD` and change the password under System Settings. Then set up the system in this order: **Sites → Talkgroups → Channels → Subscriber radios → Edge Devices (generate a token)**.

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

Set `audio_device` to an ALSA `plughw:` device so ALSA resamples to 8 kHz. Pins and sound cards are configured per channel in the web UI.

## Test without hardware

```bash
pip install -r central/requirements.txt -r edge/requirements.txt pytest
PYTHONPATH=.:central pytest -q tests        # protocol, G.711, DVSI framing, transcoder paths
python3 tests/lab_e2e.py                    # central + 2 simulated sites, full scenario
```

`lab_e2e.py` runs the full scenario and checks that it passes:
1. Enrollment and config push.
2. A call routed across sites, with talkback during hang time.
3. Talkgroup isolation.
4. A permission denial.
5. The central server is killed: both sites go to site trunking, local repeat works, and data notifications go to talkgroups and assigned subscribers.
6. The central server restarts: wide area is restored and the site trunking call log is uploaded.

## Repository layout

```
lmrcommon/        shared: voice frame protocol, modes/codecs, G.711, DVSI AMBE-3000 driver, vocoder pool
central/app/      FastAPI server: models, API, call controller, transcoder, WireGuard, SIP gateway, web UI
edge/lmr_edge/    Pi agent: enrollment, control/voice links, GPIO+ALSA radio backend, sim backend, notifier
asterisk/         Asterisk container + PJSIP/ARI/dialplan config
deploy/Caddyfile  TLS reverse proxy
tests/            unit tests + end-to-end lab test
```

## Important limitations and next steps

- **IMBE and AMBE+2 are proprietary DVSI vocoders.** This project drives licensed DVSI AMBE-3000 hardware (USB-3000, ThumbDV) and does not include a software codec.
  - Without DVSI hardware, sites use G.711 on the network, and calls that need IMBE↔AMBE+2 conversion are dropped for that destination. The dashboard shows this.
  - The driver follows the AMBE-3000R packet format, but the rate settings (`lmrcommon/dvsi.py`, `DEFAULT_RATES`) **must be checked against your chip's manual**. They have not been validated on hardware here.
- **The air interface is handled by the base radio.** The Pi works with radios through audio plus COR/PTT. It does not generate a P25/DMR/NXDN control channel itself.
  - A radio ID is only known if the radio reports it; otherwise the channel's default unit ID is used.
  - Native trunking control channels would need a modem-based backend, such as one built on MMDVM or OP25. `RadioChannel` is the place to add it.
- **Site trunking data messages are vendor specific.** Two paths are provided:
  - a `serial` template sender, for example to a repeater's data port;
  - a `send_data()` hook on the radio backend.

  Map either one to your radios' text/status messaging, such as the Kenwood, Motorola or Hytera data APIs.
- **SIP** was tested against a simulated ARI. It still needs a run against a live Asterisk.
- **Docker** images and compose were written but not built here, because no Docker daemon was available. The services were tested by running them directly.
- **Security:** change every default in `.env`, keep port 8080 reachable only from inside the tunnel, and put the UI behind your SSO/VPN if possible. AES/ADP encryption pass-through is flagged in the frame header but not implemented.
