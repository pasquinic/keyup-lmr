"""Packet-voice channel interfaces (DFSI, IP Site Connect, Capacity Plus)."""
from lmrcommon.modes import PACKET_INTERFACES

from .base import PacketChannel


def make_packet_channel(cfg: dict, loop, on_cor, on_audio, on_frame, pool) -> PacketChannel:
    iface = cfg.get("interface")
    if iface == "dfsi":
        from .dfsi import DfsiChannel
        return DfsiChannel(cfg, loop, on_cor, on_audio, on_frame, pool)
    if iface == "ipsc":
        from .ipsc import IpscChannel
        return IpscChannel(cfg, loop, on_cor, on_audio, on_frame, pool)
    if iface == "capplus":
        from .ipsc import CapPlusChannel
        return CapPlusChannel(cfg, loop, on_cor, on_audio, on_frame, pool)
    raise ValueError(f"unknown packet interface {iface}")


__all__ = ["PACKET_INTERFACES", "PacketChannel", "make_packet_channel"]
