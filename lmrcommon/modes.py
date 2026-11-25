"""System (air-interface) modes and the network voice codec each one uses.

Key design point: P25 Phase II, DMR and NXDN all use the AMBE+2 half-rate
vocoder (2450 bps voice parameters). The network carries *voice parameters
without air-interface FEC*, so calls between those three modes are bridged by
passing the 49-bit voice frames straight through -- no tandem vocoding and no
audio-quality loss. Only P25 Phase I (IMBE full-rate), analog and SIP need a
real decode/encode through PCM.
"""
from __future__ import annotations

import enum


class Codec(enum.IntEnum):
    PCM16 = 0  # 8 kHz, 16-bit signed little-endian, 160 samples / 20 ms
    ULAW = 1   # G.711 mu-law, 160 bytes / 20 ms (analog + SIP)
    IMBE = 2   # P25 Phase I full-rate 4400 bps voice params, 88 bits / 20 ms
    AMBE2 = 3  # AMBE+2 half-rate 2450 bps voice params, 49 bits / 20 ms


FRAME_MS = 20
SAMPLES_PER_FRAME = 160

# Bytes of payload carried per 20 ms frame for each network codec.
FRAME_BYTES = {
    Codec.PCM16: 320,
    Codec.ULAW: 160,
    Codec.IMBE: 11,   # 88 bits
    Codec.AMBE2: 7,   # 49 bits, padded to 56
}

# Bits of voice parameters (used by the DVSI channel packet).
FRAME_BITS = {Codec.IMBE: 88, Codec.AMBE2: 49}


class SystemMode(str, enum.Enum):
    P25P1 = "P25P1"   # P25 Phase I FDMA (IMBE)
    P25P2 = "P25P2"   # P25 Phase II TDMA (AMBE+2)
    DMR = "DMR"       # ETSI DMR Tier II/III (AMBE+2)
    NXDN = "NXDN"     # NXDN / Kenwood NEXEDGE (AMBE+2 EHR)
    ANALOG = "ANALOG" # FM analog


MODE_CODEC = {
    SystemMode.P25P1: Codec.IMBE,
    SystemMode.P25P2: Codec.AMBE2,
    SystemMode.DMR: Codec.AMBE2,
    SystemMode.NXDN: Codec.AMBE2,
    SystemMode.ANALOG: Codec.ULAW,
}

MODE_LABELS = {
    SystemMode.P25P1: "P25 Phase I (FDMA / IMBE)",
    SystemMode.P25P2: "P25 Phase II (TDMA / AMBE+2)",
    SystemMode.DMR: "DMR (AMBE+2)",
    SystemMode.NXDN: "NXDN / NEXEDGE (AMBE+2)",
    SystemMode.ANALOG: "Analog FM",
}


def codec_for_mode(mode: str) -> Codec:
    return MODE_CODEC[SystemMode(mode)]


def needs_transcode(src: Codec, dst: Codec) -> bool:
    return Codec(src) != Codec(dst)


# Channel interfaces that carry packetized digital voice (codec frames, no audio): the channel's mode
# decides its native codec (DFSI: P25 Phase I -> IMBE; IP Site Connect / Capacity Plus: DMR -> AMBE+2).
PACKET_INTERFACES = ("dfsi", "ipsc", "capplus")
INTERFACE_MODES = {"dfsi": ("P25P1",), "ipsc": ("DMR",), "capplus": ("DMR",)}
