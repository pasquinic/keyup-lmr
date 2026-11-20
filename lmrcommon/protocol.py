"""Voice transport protocol carried over UDP inside the WireGuard tunnel.

Header (24 bytes, network byte order):

    magic   2s   b"LV"
    version B
    type    B    FrameType
    flags   B    Flags
    codec   B    Codec
    site    H    originating site id (0 = SIP gateway / central)
    call_id I    assigned by central call controller (0 for keepalive)
    tg      I    talkgroup id
    src     I    source unit (radio) id, 0 if unknown
    seq     H    frame sequence number (wraps)
    plen    H    payload length

KEEPALIVE frames bind a UDP source address to a site. Their payload is
device_id(4) | unix_ts(8) | HMAC-SHA256(secret, device_id|ts)[:16].
"""
from __future__ import annotations

import enum
import hashlib
import hmac
import struct
import time
from dataclasses import dataclass

from .modes import Codec

MAGIC = b"LV"
VERSION = 1
HEADER = struct.Struct("!2sBBBBHIIIHH")
assert HEADER.size == 24


class FrameType(enum.IntEnum):
    KEEPALIVE = 0
    VOICE_START = 1
    VOICE = 2
    VOICE_END = 3


class Flags(enum.IntFlag):
    NONE = 0
    EMERGENCY = 1
    ENCRYPTED = 2
    SITE_TRUNKING = 4


@dataclass
class VoiceFrame:
    type: FrameType
    codec: Codec
    site: int
    call_id: int
    tg: int
    src: int = 0
    seq: int = 0
    flags: int = 0
    payload: bytes = b""

    def pack(self) -> bytes:
        return HEADER.pack(
            MAGIC, VERSION, int(self.type), int(self.flags), int(self.codec),
            self.site & 0xFFFF, self.call_id & 0xFFFFFFFF, self.tg & 0xFFFFFFFF,
            self.src & 0xFFFFFFFF, self.seq & 0xFFFF, len(self.payload),
        ) + self.payload

    @classmethod
    def unpack(cls, data: bytes) -> "VoiceFrame":
        if len(data) < HEADER.size:
            raise ValueError("short frame")
        magic, ver, ftype, flags, codec, site, call_id, tg, src, seq, plen = HEADER.unpack_from(data)
        if magic != MAGIC or ver != VERSION:
            raise ValueError("bad magic/version")
        payload = data[HEADER.size:HEADER.size + plen]
        if len(payload) != plen:
            raise ValueError("truncated payload")
        return cls(FrameType(ftype), Codec(codec), site, call_id, tg, src, seq, flags, payload)

    def with_payload(self, codec: Codec, payload: bytes, site: int | None = None) -> "VoiceFrame":
        return VoiceFrame(self.type, codec, self.site if site is None else site, self.call_id,
                          self.tg, self.src, self.seq, self.flags, payload)


# ---------------------------------------------------------------- keepalive auth
KA = struct.Struct("!IQ")


def make_keepalive(site: int, device_id: int, secret: str) -> bytes:
    body = KA.pack(device_id, int(time.time()))
    mac = hmac.new(secret.encode(), body, hashlib.sha256).digest()[:16]
    return VoiceFrame(FrameType.KEEPALIVE, Codec.PCM16, site, 0, 0, payload=body + mac).pack()


def parse_keepalive(frame: VoiceFrame) -> tuple[int, int, bytes, bytes]:
    """Return (device_id, ts, body, mac)."""
    if len(frame.payload) != KA.size + 16:
        raise ValueError("bad keepalive")
    device_id, ts = KA.unpack_from(frame.payload)
    return device_id, ts, frame.payload[:KA.size], frame.payload[KA.size:]


def verify_keepalive(body: bytes, mac: bytes, secret: str, ts: int, max_skew: int = 120) -> bool:
    if abs(time.time() - ts) > max_skew:
        return False
    exp = hmac.new(secret.encode(), body, hashlib.sha256).digest()[:16]
    return hmac.compare_digest(exp, mac)


# ---------------------------------------------------------------- control msgs
class Msg:
    """JSON control-channel message types (WebSocket, edge <-> central)."""
    HELLO = "hello"                    # edge -> central: auth + inventory
    HELLO_ACK = "hello_ack"            # central -> edge
    CONFIG = "config"                  # central -> edge: full site config snapshot
    HEARTBEAT = "heartbeat"            # edge -> central
    HEARTBEAT_ACK = "heartbeat_ack"
    CALL_REQUEST = "call_request"      # edge -> central (COR active)
    CALL_GRANT = "call_grant"          # central -> edge
    CALL_DENY = "call_deny"            # central -> edge
    CALL_END = "call_end"              # either direction
    CALL_START = "call_start"          # central -> destination edges (key up)
    AFFILIATE = "affiliate"            # edge -> central (unit seen on TG)
    CALL_LOG_UPLOAD = "call_log_upload"  # edge -> central after site trunking
    SITE_STATUS = "site_status"        # edge -> central (wide_area / site_trunking)
    COMMAND = "command"                # central -> edge (reboot, test tone, ...)
