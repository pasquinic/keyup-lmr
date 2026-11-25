#!/usr/bin/env python3
"""A stand-in for DVSI AMBE-3000R USB vocoders, for testing KeyUp's transcoding without hardware.

  python3 tools/fake_dvsi.py --count 4 --dir /tmp/dvsi     # creates /tmp/dvsi/dvsi0 .. dvsi3

Each device is a pseudo-terminal speaking the AMBE-3000R packet protocol that lmrcommon/dvsi.py
uses (reset/READY, PRODID, RATET / RATEP, speech <-> channel packets). It is NOT a vocoder: it
"encodes" 20 ms of PCM as its pitch (zero crossings) and loudness, and "decodes" those bytes back
into a tone of that pitch and loudness, so audio survives a transcode recognisably and tests can
check it. Point DVSI_PORTS (central) or vocoder.dvsi_ports (edge) at the device paths.
"""
from __future__ import annotations

import argparse
import math
import os
import select
import struct
import time
import tty

START = 0x61


def encode_bits(samples: list[int], nbits: int) -> bytes:
    zc = sum(1 for a, b in zip(samples, samples[1:]) if (a < 0) != (b < 0))
    rms = int(math.sqrt(sum(s * s for s in samples) / len(samples)))
    out = bytes([min(255, zc), min(255, rms >> 7)])
    return out.ljust((nbits + 7) // 8, b"\x00")


def decode_bits(data: bytes, n: int, phase: float) -> tuple[list[int], float]:
    zc, level = (data[0], data[1]) if len(data) >= 2 else (0, 0)
    freq = zc * 8000 / 2 / 160            # zero crossings per 160 samples -> Hz
    amp = min(32000, level << 7)
    out = []
    for _ in range(n):
        out.append(int(amp * math.sin(phase)))
        phase += 2 * math.pi * freq / 8000
    return out, phase % (2 * math.pi)


class Device:
    def __init__(self, path: str):
        self.master, slave = os.openpty()
        tty.setraw(slave)
        self.slave_name = os.ttyname(slave)
        if os.path.lexists(path):
            os.remove(path)
        os.symlink(self.slave_name, path)
        self.path = path
        self.buf = b""
        self.nbits = 49
        self.phase = 0.0
        self.counts = {"encode": 0, "decode": 0}
        self._slave = slave   # keep the slave open so the pty stays alive between opens

    def reply(self, ptype: int, fields: bytes) -> None:
        os.write(self.master, struct.pack("!BHB", START, len(fields), ptype) + fields)

    def feed(self, data: bytes) -> None:
        self.buf += data
        while True:
            i = self.buf.find(bytes([START]))
            if i < 0:
                self.buf = b""
                return
            self.buf = self.buf[i:]
            if len(self.buf) < 4:
                return
            length, ptype = struct.unpack("!HB", self.buf[1:4])
            if len(self.buf) < 4 + length:
                return
            body, self.buf = self.buf[4:4 + length], self.buf[4 + length:]
            self.handle(ptype, body)

    def handle(self, ptype: int, body: bytes) -> None:
        if ptype == 0x00:                       # control
            f = body[0]
            if f == 0x33:                       # reset -> READY
                self.reply(0x00, bytes([0x39, 0x2F, 0x00]))
            elif f == 0x30:                     # product id
                self.reply(0x00, bytes([0x30]) + b"AMBE3000-FAKE\x00")
            elif f == 0x09:                     # RATET: index 34 = AMBE+2 2450
                self.nbits = 49
                self.reply(0x00, bytes([0x09, 0x00]))
            elif f == 0x0A:                     # RATEP: rate control words (IMBE full rate)
                self.nbits = 88
                self.reply(0x00, bytes([0x0A, 0x00]))
            else:
                self.reply(0x00, bytes([f, 0x00]))
        elif ptype == 0x02 and body[0] == 0x00:  # speech -> channel
            n = body[1]
            samples = list(struct.unpack(f">{n}h", body[2:2 + 2 * n]))
            bits = encode_bits(samples, self.nbits)
            self.counts["encode"] += 1
            self.reply(0x01, bytes([0x01, self.nbits]) + bits)
        elif ptype == 0x01 and body[0] == 0x01:  # channel -> speech
            nbits = body[1]
            data = body[2:2 + (nbits + 7) // 8]
            samples, self.phase = decode_bits(data, 160, self.phase)
            self.counts["decode"] += 1
            self.reply(0x02, bytes([0x00, 160]) + struct.pack(">160h", *samples))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--count", type=int, default=2)
    ap.add_argument("--dir", required=True)
    a = ap.parse_args()
    os.makedirs(a.dir, exist_ok=True)
    devs = [Device(os.path.join(a.dir, f"dvsi{i}")) for i in range(a.count)]
    print(",".join(d.path for d in devs), flush=True)
    stats = os.path.join(a.dir, "stats")
    last = 0.0
    while True:
        r, _, _ = select.select([d.master for d in devs], [], [], 0.5)
        for d in devs:
            if d.master in r:
                try:
                    d.feed(os.read(d.master, 4096))
                except OSError:
                    pass
        if time.time() - last > 0.5:
            last = time.time()
            with open(stats + ".tmp", "w") as f:
                f.write(repr({d.path: d.counts for d in devs}))
            os.replace(stats + ".tmp", stats)


if __name__ == "__main__":
    main()
