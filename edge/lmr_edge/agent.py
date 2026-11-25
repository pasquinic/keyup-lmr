"""Edge base-processing agent: control link, voice transport, PTT/COR call handling and site trunking."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass, field
from urllib.parse import urlparse

import websockets

from lmrcommon import audio, morse
from lmrcommon.modes import PACKET_INTERFACES, Codec
from lmrcommon.protocol import FrameType, Msg, VoiceFrame, make_keepalive, Flags
from lmrcommon.vocoder import VocoderPool, VocoderUnavailable

from . import __version__
from .notifier import Notifier
from .packet import make_packet_channel
from .radio import make_channel
from .state import State

log = logging.getLogger("agent")
HW_KEYS = ("audio_device", "ptt_gpio", "cor_gpio", "cor_active_low", "signaling", "serial_port", "cor_invert",
           "interface", "packet_config", "mode", "timeslot")


@dataclass
class ChanState:
    rx: str = "idle"        # idle|requesting|granted|ended_pending|denied|blocked|local
    rx_call: int = 0
    unit: int = 0
    buffer: list = field(default_factory=list)   # ("frame", pcm) or ("pframe", codec frame) before the grant
    tg: int = 0                                    # talkgroup of the current call (packet links: per call)
    uplink: asyncio.Queue | None = None
    local: dict | None = None
    deny_reason: str = ""


class EdgeAgent:
    def __init__(self, cfg: dict, state: State):
        self.cfg = cfg
        self.state = state
        self.site_id = state.get("site_id", 0)
        self.device_id = state.get("device_id", 0)
        self.cached: dict = state.get("cached_config") or {}
        self.radios: dict = {}
        self.chan: dict[int, ChanState] = {}
        self.mode = "wide_area"
        self.mode_since = time.time()
        self.connected = False
        self.ws = None
        self.voice_key = ""
        self.last_link_ok = time.time()
        self.last_announce = 0.0
        self.transport: asyncio.DatagramTransport | None = None
        self.voice_addr: tuple[str, int] | None = None
        self.network_codec = Codec[self.cached.get("network_codec", "ULAW")]
        self.inbound: dict[int, dict] = {}   # call_id -> {"channels": set, "q": Queue, "task": Task}
        self.pool = VocoderPool(cfg["vocoder"].get("dvsi_ports") or [], cfg["vocoder"].get("baud", 460800))
        self.notifier = Notifier(cfg, self)
        self.started = time.time()
        self.stats = {"frames_tx": 0, "frames_rx": 0, "calls_tx": 0, "calls_rx": 0, "local_calls": 0}
        self.loop: asyncio.AbstractEventLoop | None = None
        self.cw_last: dict[int, float] = {}       # channel -> last CW ID time
        self.cw_pending: dict[int, bool] = {}     # channel transmitted since its last ID
        self.cw_sent = 0

    # ================================================================ helpers
    def channels_cfg(self) -> list[dict]:
        return [c for c in self.cached.get("channels", []) if c.get("enabled", True)]

    def setting(self, key: str, default):
        return type(default)(self.cached.get("settings", {}).get(key, default))

    @property
    def st_timeout(self) -> float:
        return float(self.cfg["site_trunking"].get("timeout_s") or self.setting("site_trunking_timeout_s", 10))

    def _task(self, coro):
        t = self.loop.create_task(coro)
        t.add_done_callback(lambda t: t.cancelled() or not t.exception() or
                            log.error("task error: %r", t.exception()))
        return t

    async def send(self, msg: dict) -> None:
        if self.ws and self.connected:
            try:
                await self.ws.send(json.dumps(msg))
            except Exception as e:
                log.debug("send failed: %s", e)

    def vocoders(self) -> list[str]:
        v = ["PCM16", "ULAW"]
        if self.pool.hardware_count:
            v += ["IMBE", "AMBE2"]
        return v

    # ================================================================ config
    async def apply_config(self, cfg: dict) -> None:
        self.cached = cfg
        self.site_id = cfg["site"]["id"]
        self.network_codec = Codec[cfg.get("network_codec", "ULAW")]
        self.state.set(cached_config=cfg, site_id=self.site_id)
        await self._sync_radios()
        log.info("config applied: site %s, %d channels, net codec %s", cfg["site"]["name"],
                 len(self.channels_cfg()), self.network_codec.name)

    async def _sync_radios(self) -> None:
        want = {c["id"]: c for c in self.channels_cfg()}
        for cid in list(self.radios):
            r = self.radios[cid]
            if cid not in want or any(r.cfg.get(k) != want[cid].get(k) for k in HW_KEYS):
                await r.stop()
                del self.radios[cid]
        for cid, c in want.items():
            if cid in self.radios:
                self.radios[cid].update_config(c)
                continue
            if c.get("interface", "radio") in PACKET_INTERFACES:
                r = make_packet_channel(c, self.loop, self.on_cor, self.on_audio, self.on_packet_frame, self.pool)
            else:
                r = make_channel(self.cfg["radio_backend"], c, self.loop, self.on_cor, self.on_audio)
            try:
                await r.start()
            except Exception as e:
                log.error("channel %s (%s) failed to start: %s", cid, c.get("name"), e)
                continue
            self.radios[cid] = r
            cs = self.chan.setdefault(cid, ChanState())
            if cs.uplink is None:
                cs.uplink = asyncio.Queue()
                self._task(self._uplink_worker(cid))

    # ================================================================ radio events
    def on_cor(self, cid: int, active: bool, unit: int) -> None:
        radio, cs = self.radios.get(cid), self.chan.get(cid)
        if not radio or not cs:
            return
        if active:
            if radio.ptt and radio.announcing:
                radio.abort_announcement()
            if radio.ptt:
                cs.rx = "blocked"  # our own transmission / half-duplex
                return
            packet = getattr(radio, "packet", False)
            tg = radio.rx_tg if packet else radio.cfg.get("talkgroup_id")
            if not tg:
                return
            cs.unit = unit or radio.cfg.get("default_unit_id", 0)
            cs.tg = tg
            if self.mode == "site_trunking" or not self.connected:
                self._local_start(cid, tg, cs.unit)
                return
            cs.rx, cs.buffer = "requesting", []
            req = {"type": Msg.CALL_REQUEST, "channel": cid, "tg": tg, "src": cs.unit,
                   "emergency": bool(packet and radio.rx_emergency)}
            if packet:
                req["codec"] = radio.codec.name
            self._task(self.send(req))
        else:
            if cs.rx == "granted":
                cs.uplink.put_nowait(("end", cs.rx_call, None))
                self._task(self.send({"type": Msg.CALL_END, "call_id": cs.rx_call}))
                cs.rx = "idle"
            elif cs.rx == "requesting":
                cs.rx = "ended_pending"
            elif cs.rx == "local":
                self._local_end(cid)
            elif cs.rx == "denied":
                cs.rx = "idle"
                self._task(radio.play(audio.tone_sequence(audio.TONES["busy" if cs.deny_reason == "busy" else "deny"])))
            else:
                cs.rx = "idle"

    def on_audio(self, cid: int, pcm: bytes) -> None:
        cs = self.chan.get(cid)
        if not cs:
            return
        if cs.rx == "requesting":
            cs.buffer.append(("frame", pcm))
            del cs.buffer[:-25]
        elif cs.rx == "granted":
            cs.uplink.put_nowait(("frame", cs.rx_call, pcm))
        elif cs.rx == "local" and cs.local:
            for d in cs.local["dests"]:
                r = self.radios.get(d)
                if r:
                    r.write_audio(pcm)

    def on_packet_frame(self, cid: int, payload: bytes) -> None:
        """A voice frame in the link's native codec from a packet-voice channel."""
        cs, src = self.chan.get(cid), self.radios.get(cid)
        if not cs or not src:
            return
        if cs.rx == "requesting":
            cs.buffer.append(("pframe", payload))
            del cs.buffer[:-25]
        elif cs.rx == "granted":
            cs.uplink.put_nowait(("pframe", cs.rx_call, payload))
        elif cs.rx == "local" and cs.local:
            pcm_dests = []
            for d in cs.local["dests"]:
                r = self.radios.get(d)
                if r is None:
                    continue
                if getattr(r, "packet", False) and r.codec == src.codec:
                    r.write_frame(payload)
                else:
                    pcm_dests.append(r)
            if pcm_dests and self.pool.available(src.codec):
                self._task(self._local_decode(cs, src.codec, payload, pcm_dests))

    async def _local_decode(self, cs: ChanState, codec: Codec, payload: bytes, dests: list) -> None:
        """Site trunking: a packet-link call heard on radios needs the edge's vocoder."""
        try:
            dec = cs.local.setdefault("decoder", None) or self.pool.acquire(codec)
            cs.local["decoder"] = dec
            pcm = await asyncio.to_thread(dec.decode, payload)
        except Exception:
            return
        for r in dests:
            r.write_audio(pcm)

    async def _uplink_worker(self, cid: int) -> None:
        """Encode + send this channel's RX audio in order (DVSI calls run off-loop)."""
        cs = self.chan[cid]
        enc, enc_call, seq = None, 0, 0
        codec = self.network_codec
        while True:
            kind, call_id, pcm = await cs.uplink.get()
            radio = self.radios.get(cid)
            tg = cs.tg or (radio.cfg.get("talkgroup_id", 0) if radio else 0)
            if kind == "start":
                enc_call, seq = call_id, 0
                if enc:
                    enc.release()
                    enc = None
                if getattr(radio, "packet", False):
                    codec = radio.codec      # frames are already in the link's codec
                else:
                    codec = self.network_codec
                    try:
                        enc = self.pool.acquire(codec)
                    except VocoderUnavailable as e:
                        log.error("no vocoder for uplink: %s", e)
                        enc = None
                self._send_voice(VoiceFrame(FrameType.VOICE_START, codec, self.site_id, call_id, tg, cs.unit, seq))
            elif kind == "pframe" and call_id == enc_call:
                seq += 1
                self._send_voice(VoiceFrame(FrameType.VOICE, codec, self.site_id, call_id, tg, cs.unit, seq, 0, pcm))
                self.stats["frames_tx"] += 1
            elif kind == "frame" and enc and call_id == enc_call:
                seq += 1
                try:
                    data = enc.encode(pcm) if enc.name.startswith("software") else await asyncio.to_thread(enc.encode, pcm)
                except Exception as e:
                    log.error("encode error: %s", e)
                    continue
                self._send_voice(VoiceFrame(FrameType.VOICE, codec, self.site_id, call_id, tg, cs.unit, seq, 0, data))
                self.stats["frames_tx"] += 1
            elif kind == "end" and call_id == enc_call:
                self._send_voice(VoiceFrame(FrameType.VOICE_END, codec, self.site_id, call_id, tg, cs.unit, seq + 1))
                if enc:
                    enc.release()
                enc, enc_call = None, 0

    # ================================================================ control messages
    async def handle(self, msg: dict) -> None:
        t = msg.get("type")
        self.last_link_ok = time.time()
        if t == Msg.CONFIG:
            await self.apply_config(msg)
        elif t == Msg.CALL_GRANT:
            cs = self.chan.get(msg.get("channel"))
            if not cs:
                return
            if cs.rx == "requesting":
                cs.rx, cs.rx_call = "granted", msg["call_id"]
                self.stats["calls_tx"] += 1
                cs.uplink.put_nowait(("start", cs.rx_call, None))
                for kind, f in cs.buffer:
                    cs.uplink.put_nowait((kind, cs.rx_call, f))
                cs.buffer = []
            elif cs.rx == "ended_pending":
                cs.rx = "idle"
                await self.send({"type": Msg.CALL_END, "call_id": msg["call_id"]})
        elif t == Msg.CALL_DENY:
            cs = self.chan.get(msg.get("channel"))
            if not cs:
                return
            reason = msg.get("reason", "")
            log.info("call on channel %s denied: %s", msg.get("channel"), reason)
            if cs.rx == "granted" and reason == "preempted":
                cs.uplink.put_nowait(("end", cs.rx_call, None))
                cs.rx = "blocked"
            elif cs.rx == "requesting":
                cs.rx, cs.deny_reason = "denied", reason
            elif cs.rx == "ended_pending":
                cs.rx = "idle"
                self._task(self.radios[msg["channel"]].play(audio.tone_sequence(audio.TONES["deny"])))
        elif t == Msg.CALL_START:
            self._call_start(msg)
        elif t == Msg.CALL_END:
            self._call_end(msg.get("call_id", 0))
        elif t == Msg.COMMAND:
            await self._command(msg)

    def _call_start(self, msg: dict) -> None:
        if self.mode == "site_trunking":
            return
        cid = msg["call_id"]
        want = {c for c in msg.get("channels", []) if c in self.radios}
        call = self.inbound.get(cid)
        if call is None:
            q: asyncio.Queue = asyncio.Queue(maxsize=200)
            call = self.inbound[cid] = {"channels": set(), "q": q, "codec": Codec[msg.get("codec", "ULAW")],
                                        "task": None, "tg": msg.get("tg"), "src": msg.get("src"), "chan_codecs": {}}
            call["task"] = self._task(self._downlink_worker(cid))
            self.stats["calls_rx"] += 1
        call["tg"], call["src"] = msg.get("tg", call["tg"]), msg.get("src", call["src"])
        for c, name in (msg.get("channel_codecs") or {}).items():
            try:
                call["chan_codecs"][int(c)] = Codec[name]
            except (KeyError, ValueError):
                pass
        for c in want:
            r = self.radios[c]
            if getattr(r, "packet", False):
                tg = call["tg"] if r.carries(call["tg"] or 0) else r.cfg.get("talkgroup_id")
                r.tx_info = {"tg": tg, "src": call["src"], "emergency": bool(msg.get("emergency"))}
        for c in call["channels"] - want:
            self.radios[c].unkey()
        for c in want - call["channels"]:
            r = self.radios[c]
            if r.cor and self.chan[c].rx in ("granted", "requesting"):
                continue  # don't stomp a local talker (busy channel)
            r.key()
        call["channels"] = want
        log.info("call %s TG %s from site %s -> keying channels %s", cid, msg.get("tg"), msg.get("src_site"), sorted(want))

    def _call_end(self, call_id: int) -> None:
        call = self.inbound.pop(call_id, None)
        if call:
            for c in call["channels"]:
                if c in self.radios:
                    self.radios[c].unkey()
            call["q"].put_nowait(None)
        for cid, cs in self.chan.items():
            if cs.rx == "granted" and cs.rx_call == call_id:
                cs.uplink.put_nowait(("end", call_id, None))
                cs.rx = "blocked"  # central ended our call (max time etc.) -- wait for COR drop

    async def _downlink_worker(self, call_id: int) -> None:
        """Play a network call: packet links take frames in their own codec untouched; radios (and
        packet links fed another codec) get PCM, decoded once per frame."""
        call = self.inbound[call_id]
        decs: dict[Codec, object] = {}
        failed: set[Codec] = set()
        try:
            while True:
                frame = await call["q"].get()
                if frame is None:
                    break
                if not frame.payload:
                    continue
                pcm_targets = []
                for c in call["channels"]:
                    r = self.radios.get(c)
                    if r is None:
                        continue
                    want = call["chan_codecs"].get(c, call["codec"])
                    if frame.codec != want and len(set(call["chan_codecs"].values()) | {call["codec"]}) > 1:
                        continue   # this stream is for the site's other channels
                    if getattr(r, "packet", False) and frame.codec == r.codec:
                        r.write_frame(frame.payload)
                    else:
                        pcm_targets.append(r)
                if pcm_targets and frame.codec not in failed:
                    dec = decs.get(frame.codec)
                    try:
                        if dec is None:
                            dec = decs[frame.codec] = self.pool.acquire(frame.codec)
                        pcm = dec.decode(frame.payload) if dec.name.startswith("software") else \
                            await asyncio.to_thread(dec.decode, frame.payload)
                    except VocoderUnavailable as e:
                        log.error("call %s: cannot decode %s: %s", call_id, frame.codec.name, e)
                        failed.add(frame.codec)
                        continue
                    for r in pcm_targets:
                        r.write_audio(pcm)
                self.stats["frames_rx"] += 1
        finally:
            for dec in decs.values():
                dec.release()

    def on_voice(self, frame: VoiceFrame) -> None:
        call = self.inbound.get(frame.call_id)
        if not call:
            return
        if frame.type == FrameType.VOICE_END:
            # talker released: drop our transmitters during hang time so a reply can be heard
            for c in call["channels"]:
                if c in self.radios:
                    self.radios[c].unkey()
            return
        for c in call["channels"]:  # (re-)key on voice, e.g. next talker inside hang time
            r = self.radios.get(c)
            if r and not r.ptt and not r.cor:
                if getattr(r, "packet", False):
                    r.tx_info = {**r.tx_info, "src": frame.src}
                r.key()
        try:
            call["q"].put_nowait(frame)
        except asyncio.QueueFull:
            pass

    async def _command(self, msg: dict) -> None:
        cmd = msg.get("command")
        log.info("command from central: %s", cmd)
        if cmd == "test_tone":
            pcm = audio.tone(1000, 1000, 0.3)
            await asyncio.gather(*(r.play(pcm) for r in self.radios.values() if not r.cor))
        elif cmd == "announce_site_trunking_test":
            await self.notifier.tone_all("site_trunking")
        elif cmd == "restart_agent":
            os._exit(3)  # systemd restarts us

    # ================================================================ site trunking (local call processing)
    def _local_authorized(self, tg: int, unit: int) -> bool:
        subs = {s["id"]: s for s in self.cached.get("subscribers", [])}
        if unit and unit in subs:
            tgs = subs[unit].get("talkgroups") or []
            return not tgs or tg in tgs
        return self.setting("require_known_subscriber", "false") != "true"

    def _local_start(self, cid: int, tg: int, unit: int) -> None:
        cs = self.chan[cid]
        if not self._local_authorized(tg, unit):
            cs.rx, cs.deny_reason = "denied", "not_permitted"
            return
        dests = []
        if self.cfg["site_trunking"].get("local_repeat", True):
            for oid, r in self.radios.items():
                carries = r.carries(tg) if getattr(r, "packet", False) else r.cfg.get("talkgroup_id") == tg
                if oid != cid and carries and not r.cor:
                    if getattr(r, "packet", False):
                        r.tx_info = {"tg": tg, "src": unit, "emergency": False}
                    r.key()
                    dests.append(oid)
        cs.rx = "local"
        cs.local = {"tg": tg, "src": unit, "channel": cid, "started": time.time(), "dests": dests,
                    "site_trunking": self.mode == "site_trunking"}
        self.stats["local_calls"] += 1
        log.info("LOCAL call TG %s unit %s on channel %s -> %s", tg, unit, cid, dests)

    def _local_end(self, cid: int) -> None:
        cs = self.chan[cid]
        call, cs.local, cs.rx = cs.local, None, "idle"
        if not call:
            return
        if call.get("decoder"):
            call.pop("decoder").release()
        for d in call["dests"]:
            if d in self.radios:
                self.radios[d].unkey()
        rec = {k: call[k] for k in ("tg", "src", "channel", "started")}
        rec["duration"] = round(time.time() - call["started"], 2)
        logs = self.state.get("pending_logs", [])[-5000:] + [rec]
        self.state.set(pending_logs=logs)
        if call["site_trunking"]:
            self._task(self.notifier.post_call(self.radios[cid]))

    async def enter_site_trunking(self) -> None:
        if self.mode == "site_trunking":
            return
        log.warning("central unreachable for %.0fs -> SITE TRUNKING", time.time() - self.last_link_ok)
        self.mode, self.mode_since = "site_trunking", time.time()
        for call_id in list(self.inbound):
            self._call_end(call_id)
        for cs in self.chan.values():
            if cs.rx in ("granted", "requesting", "ended_pending"):
                cs.rx = "blocked"
        self.last_announce = time.time()
        await self.notifier.enter()

    async def exit_site_trunking(self) -> None:
        if self.mode != "site_trunking":
            return
        log.warning("central link restored after %.0fs -> WIDE AREA", time.time() - self.mode_since)
        self.mode, self.mode_since = "wide_area", time.time()
        logs = self.state.get("pending_logs", [])
        if logs:
            await self.send({"type": Msg.CALL_LOG_UPLOAD, "calls": logs})
            self.state.set(pending_logs=[])
        await self.send({"type": Msg.SITE_STATUS, "mode": "wide_area"})
        await self.notifier.exit()

    async def _cw_id(self) -> None:
        """Identify each transmitter in CW (site callsign) after use, at most once per interval."""
        callsign = (self.cached.get("site") or {}).get("callsign", "").strip()
        if not callsign:
            return
        interval = self.setting("cw_id_interval_s", 600)
        t = time.time()
        for cid, r in list(self.radios.items()):
            if getattr(r, "packet", False):
                continue   # no CW on packet links (the equipment identifies itself)
            if r.ptt and not r.cw_keyed:
                self.cw_pending[cid] = True
            if not self.cw_pending.get(cid) or r.ptt or r.cor:
                continue
            if t - self.cw_last.get(cid, 0) < interval:
                continue
            self.cw_pending[cid], self.cw_last[cid] = False, t
            r.cw_keyed = True
            self.cw_sent += 1
            log.info("CW ID %s on channel %s", callsign, cid)

            async def send(r=r):
                try:
                    await r.play(morse.to_pcm(callsign))
                    for _ in range(100):  # wait out the TX tail so the ID doesn't count as new use
                        if not r.ptt:
                            break
                        await asyncio.sleep(0.05)
                finally:
                    r.cw_keyed = False
            self._task(send())

    async def monitor_loop(self) -> None:
        while True:
            await asyncio.sleep(0.5)
            try:
                await self._cw_id()
            except Exception:
                log.exception("CW ID failed")
            silent = time.time() - self.last_link_ok
            if self.connected and silent > max(self.st_timeout, 6):
                log.warning("control link silent for %.0fs, dropping connection", silent)
                self.connected = False
                if self.ws:
                    self._task(self.ws.close())
            if self.mode != "site_trunking" and not self.connected and silent > self.st_timeout and self.cached:
                await self.enter_site_trunking()
            if self.mode == "site_trunking":
                interval = self.setting("site_trunking_announce_interval_s", 300)
                if interval and time.time() - self.last_announce > interval:
                    self.last_announce = time.time()
                    self._task(self.notifier.periodic())

    # ================================================================ transport
    def _control_url(self) -> str:
        if self.cfg.get("control_url"):
            return self.cfg["control_url"].rstrip("/")
        return (self.state.get("wireguard") or {}).get("control_url", self.cfg["server_url"]).rstrip("/")

    def _send_voice(self, frame: VoiceFrame) -> None:
        if self.transport and self.voice_addr and self.connected:
            self.transport.sendto(frame.pack(), self.voice_addr)

    def _telemetry(self) -> dict:
        t = {"version": __version__, "mode": self.mode, "uptime_s": round(time.time() - self.started),
             "vocoders": self.vocoders(), "channels": [r.status() for r in self.radios.values()],
             "cw_ids_sent": self.cw_sent, **self.stats}
        try:
            t["cpu_temp_c"] = round(int(open("/sys/class/thermal/thermal_zone0/temp").read()) / 1000, 1)
        except Exception:
            pass
        try:
            t["load"] = round(os.getloadavg()[0], 2)
        except Exception:
            pass
        return t

    async def control_loop(self) -> None:
        backoff = 1
        url = self._control_url().replace("http", "ws", 1) + "/edge/ws"
        while True:
            try:
                async with websockets.connect(url, open_timeout=5, ping_interval=5, ping_timeout=5,
                                              close_timeout=2) as ws:
                    self.ws = ws
                    await ws.send(json.dumps({"type": Msg.HELLO, "device_id": self.device_id,
                                              "device_secret": self.state.get("device_secret"),
                                              "version": __version__, "vocoders": self.vocoders(),
                                              "mode": self.mode}))
                    ack = json.loads(await asyncio.wait_for(ws.recv(), 10))
                    if ack.get("type") != Msg.HELLO_ACK:
                        raise ConnectionError(f"rejected: {ack}")
                    self.voice_key = ack["voice_key"]
                    host = urlparse(self._control_url()).hostname
                    self.voice_addr = ((self.state.get("wireguard") or {}).get("voice_host") if not self.cfg.get("control_url") else host,
                                       ack.get("voice_port", 50000))
                    self.connected, self.last_link_ok, backoff = True, time.time(), 1
                    self._keepalive()
                    log.info("connected to central %s (voice %s:%s)", url, *self.voice_addr)
                    hb = self._task(self._heartbeat_loop())
                    restored = self._task(self._restore_after_config())
                    try:
                        async for raw in ws:
                            await self.handle(json.loads(raw))
                    finally:
                        hb.cancel()
                        restored.cancel()
            except Exception as e:
                if self.connected:
                    log.warning("control link lost: %s", e)
                else:
                    log.debug("connect failed: %s", e)
            self.connected, self.ws = False, None
            for call_id in list(self.inbound):
                self._call_end(call_id)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 8)

    async def _restore_after_config(self) -> None:
        await asyncio.sleep(0.5)  # config arrives right after hello_ack
        if self.mode == "site_trunking":
            await self.exit_site_trunking()
        elif self.state.get("pending_logs"):
            await self.send({"type": Msg.CALL_LOG_UPLOAD, "calls": self.state.get("pending_logs")})
            self.state.set(pending_logs=[])

    async def _heartbeat_loop(self) -> None:
        while True:
            await self.send({"type": Msg.HEARTBEAT, "mode": self.mode, "telemetry": self._telemetry()})
            self._keepalive()
            await asyncio.sleep(3)

    def _keepalive(self) -> None:
        if self.transport and self.voice_addr and self.voice_key:
            self.transport.sendto(make_keepalive(self.site_id, self.device_id, self.voice_key), self.voice_addr)

    def status(self) -> dict:
        return {"device_id": self.device_id, "site_id": self.site_id, "mode": self.mode,
                "mode_since": self.mode_since, "connected": self.connected,
                "network_codec": self.network_codec.name, "pending_logs": len(self.state.get("pending_logs", [])),
                "notifications": self.notifier.sent[-20:], "inbound_calls": list(self.inbound), **self._telemetry()}

    # ================================================================ main
    async def run(self) -> None:
        self.loop = asyncio.get_running_loop()

        agent = self

        class VoiceProto(asyncio.DatagramProtocol):
            def datagram_received(self, data, addr):
                try:
                    agent.on_voice(VoiceFrame.unpack(data))
                except ValueError:
                    pass

        self.transport, _ = await self.loop.create_datagram_endpoint(VoiceProto, local_addr=("0.0.0.0", 0))
        if self.cached:
            await self._sync_radios()
            log.info("started from cached config (%d channels)", len(self.radios))
        from .localapi import serve
        await serve(self, self.cfg["local_api"]["host"], int(self.cfg["local_api"]["port"]))
        await asyncio.gather(self.control_loop(), self.monitor_loop())
