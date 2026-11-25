# KeyUp PoC protocol (v1)

Push-to-talk over cellular (PoC) clients connect to the KeyUp central server through a WebSocket at `wss://<server>/poc/ws`. The connection goes through Caddy over TLS. A PoC client is a **subscriber radio**: it uses one Subscriber's radio ID and the same rules as an RF radio.

## Activation

1. In **Subscriber Radios**, the admin presses **PoC QR**. KeyUp then:
   - enables PoC for the radio ID;
   - issues a one-time code (8 characters, valid for 24 hours);
   - shows a QR code containing `keyup://activate?s=<server>&u=<radioId>&c=<code>`.
2. The app sends `POST /api/poc/activate {unit, code, device_name, platform, app_version}` and gets back `{token, unit, alias, ws_path}`.
3. The device token is stored in the phone's secure storage (Keychain or Keystore). The server keeps only its SHA-256 hash. Revoking it in **PoC Phones** disconnects the phone immediately.

## Messages

JSON text frames:

| Direction | Message | Meaning |
|---|---|---|
| → | `{"t":"auth","token","app","platform"}` | First message |
| ← | `auth_ok` / `profile` `{unit, system, talkgroups[], zones[], selected, scan, scan_on, network}` | Permitted talkgroups only. `zones` is `[{id, name, talkgroups:[tg,…]}]` in the radio's zone order; talkgroups not in any of its zones appear in an "Other" zone. `profile` is re-sent when the admin changes something |
| ← | `auth_fail {reason}` / `inhibited {reason}` | The connection is then closed (`invalid_token`, `device_revoked`, `inhibited`, `poc_disabled`, …) |
| → | `{"t":"select","tg","scan":[..],"scan_on"}` | Selected talkgroup, plus the scan list (up to 16) |
| → | `{"t":"ptt","on":true,"tg","emergency"}` / `{"t":"ptt","on":false}` | Push-to-talk |
| ← | `grant {tg}` / `deny {tg, reason}` / `tx_end {tg, reason}` | Deny reasons: `busy`, `not_permitted`, `network_down`, `no_interconnect`. `tx_end` reasons: `timeout` and others |
| ← | `rx_start {tg, tg_name, src, alias, emergency}` / `rx_end {tg}` | Who is talking. The selected talkgroup has priority over scanned ones |
| ↔ | `emergency {tg, on}` | Declare or clear an emergency. It is broadcast to everyone on the talkgroup and raises the console alarm |
| ← | `status {network}` | Radio network up or down (at least one edge or packet site connected) |
| → / ← | `ping {ts}` / `pong {ts, network}` | Every 5 s. After 20 s with no pong, the client reconnects |

Binary frames carry audio as 20 ms frames of G.711 μ-law at 8 kHz:
- **Uplink:** `0x01` followed by N × 160 bytes. The app batches 3 frames (60 ms) per message.
- **Downlink:** `0x02`, tg (u32, big-endian), src (u32, big-endian), then 160 bytes μ-law.

The link is half duplex, like a radio. Received audio is muted while transmitting.

## How the server handles it

`central/app/services/poc.py`. Each connected phone is a leg of KeyUp's own call router (`call_controller.py`), exactly like an edge site, a SIP call or a dispatch console:

- **Same rules as a radio:**
  - only permitted talkgroups (and `allow_poc` on the talkgroup);
  - restricted talkgroups only if the radio ID lists them;
  - talkgroup patches, allowed sites and affiliated-only routing apply as for RF;
  - the system maximum call time;
  - inhibit (or disabling PoC on the Subscriber) logs the phone out within one save;
  - last-seen talkgroup and time are updated.
- **Floor control:** the router's normal floor per talkgroup. A key-up while the talkgroup is busy gets `deny busy`; an emergency key-up preempts a lower-priority call.
- **Audio:** μ-law from the phone becomes codec-tagged voice frames in the router. The transcoder converts to each destination's codec (G.711 pass-through, or IMBE/AMBE+2 through the DVSI pool). Downlink audio to a phone is always μ-law.
- **Jitter buffer:** 60 ms, paced at 20 ms. If the uplink stalls while PTT is held, silence is sent so the call doesn't drop.
- **Phone to phone:** routed by the router like any other leg; the talker never hears itself.
- **Recording:** PoC calls are recorded and logged like any other call when the talkgroup has recording on.
