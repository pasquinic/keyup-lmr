"""Driver for DVSI AMBE-3000R based USB vocoders (USB-3000, ThumbDV, AMBE-3000 boards).

IMBE (P25 Phase I) and AMBE+2 (P25 Phase II / DMR / NXDN) are proprietary DVSI
vocoders. This project does not ship a software implementation of them; real
encode/decode is done by a licensed DVSI hardware vocoder attached over USB-serial.

Packet format (AMBE-3000R Vocoder Chip Users Manual, "Packet Format"):

    0x61 | length(2, big-endian, bytes after the 4-byte header) | type(1) | fields...

    type 0x00 control, 0x01 channel (encoded bits), 0x02 speech (PCM)

The rate settings below are the commonly used values; confirm them against the
manual revision for your chip before going live (they are overridable in config).
"""
from __future__ import annotations

import logging
import struct
import threading

from .modes import Codec, FRAME_BITS, FRAME_BYTES

log = logging.getLogger("dvsi")

START = 0x61
PKT_CONTROL, PKT_CHANNEL, PKT_SPEECH = 0x00, 0x01, 0x02
F_CHAND = 0x01
F_SPEECHD = 0x00
F_RATET = 0x09
F_RATEP = 0x0A
F_PRODID = 0x30
F_RESET = 0x33
F_PARITY = 0x2F
F_PARITYMODE = 0x3F

# Rate configuration per network codec (no air-interface FEC on the network side).
DEFAULT_RATES = {
    # AMBE+2 2450 bps voice only (rate index 34). DMR/NXDN/P25P2 FEC is added at the air interface.
    Codec.AMBE2: {"index": 34},
    # P25 full-rate IMBE-compatible 4400 bps, no FEC, via rate control words.
    Codec.IMBE: {"rcw": [0x0558, 0x086B, 0x0000, 0x0000, 0x0000, 0x0158]},
}


def _parity(body: bytes) -> int:
    p = 0
    for b in body:
        p ^= b
    return p


def build_packet(ptype: int, fields: bytes, parity: bool = True) -> bytes:
    if parity:
        fields = fields + bytes([F_PARITY])
    hdr = struct.pack("!BHB", START, len(fields) + (1 if parity else 0), ptype)
    pkt = hdr + fields
    if parity:
        pkt += bytes([_parity(pkt[1:])])
    return pkt


def bits_to_bytes(bits_payload: bytes, nbits: int) -> bytes:
    return bits_payload[: (nbits + 7) // 8]


class DVSIError(RuntimeError):
    pass


class DVSIVocoder:
    """One AMBE-3000R channel. Thread-safe, blocking request/response over serial."""

    def __init__(self, port: str, baud: int = 460800, rates: dict | None = None, timeout: float = 0.25):
        import serial  # pyserial

        self.port = port
        self.rates = {**DEFAULT_RATES, **(rates or {})}
        self.ser = serial.Serial(port, baud, timeout=timeout)
        self.lock = threading.Lock()
        self.codec: Codec | None = None
        self.product = "unknown"
        self.reset()

    # -- low level -----------------------------------------------------------
    def _read_packet(self) -> tuple[int, bytes]:
        hdr = self.ser.read(4)
        if len(hdr) < 4 or hdr[0] != START:
            raise DVSIError(f"{self.port}: no/invalid response header {hdr!r}")
        length, ptype = struct.unpack("!HB", hdr[1:])
        body = self.ser.read(length)
        if len(body) != length:
            raise DVSIError(f"{self.port}: short read")
        return ptype, body

    def _xfer(self, ptype: int, fields: bytes) -> tuple[int, bytes]:
        with self.lock:
            self.ser.write(build_packet(ptype, fields))
            return self._read_packet()

    # -- control -------------------------------------------------------------
    def reset(self) -> None:
        with self.lock:
            self.ser.reset_input_buffer()
            self.ser.write(build_packet(PKT_CONTROL, bytes([F_RESET]), parity=False))
            self.ser.read(7)  # READY packet
        _, body = self._xfer(PKT_CONTROL, bytes([F_PRODID]))
        self.product = body[1:].split(b"\x00")[0].decode(errors="ignore") or "AMBE3000"
        log.info("DVSI %s on %s ready", self.product, self.port)

    def set_codec(self, codec: Codec) -> None:
        if self.codec == codec:
            return
        cfg = self.rates[Codec(codec)]
        if "index" in cfg:
            fields = bytes([F_RATET, cfg["index"]])
        else:
            fields = bytes([F_RATEP]) + struct.pack("!6H", *cfg["rcw"])
        _, body = self._xfer(PKT_CONTROL, fields)
        if len(body) < 2 or body[1] != 0x00:
            raise DVSIError(f"rate set failed: {body!r}")
        self.codec = Codec(codec)

    # -- voice ---------------------------------------------------------------
    def encode(self, pcm: bytes) -> bytes:
        """160 samples PCM16 LE -> voice parameter bits for current codec."""
        samples = struct.unpack("<160h", pcm)
        fields = bytes([F_SPEECHD, 160]) + struct.pack(">160h", *samples)
        ptype, body = self._xfer(PKT_SPEECH, fields)
        if ptype != PKT_CHANNEL or body[0] != F_CHAND:
            raise DVSIError("unexpected encode response")
        nbits = body[1]
        out = body[2: 2 + (nbits + 7) // 8]
        return out.ljust(FRAME_BYTES[self.codec], b"\x00")

    def decode(self, bits: bytes) -> bytes:
        nbits = FRAME_BITS[self.codec]
        fields = bytes([F_CHAND, nbits]) + bits_to_bytes(bits, nbits)
        ptype, body = self._xfer(PKT_CHANNEL, fields)
        if ptype != PKT_SPEECH or body[0] != F_SPEECHD:
            raise DVSIError("unexpected decode response")
        n = body[1]
        samples = struct.unpack(f">{n}h", body[2: 2 + 2 * n])
        return struct.pack(f"<{n}h", *samples)

    def close(self) -> None:
        try:
            self.ser.close()
        except Exception:
            pass
