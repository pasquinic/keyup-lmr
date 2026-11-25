from __future__ import annotations

import datetime as dt
import json

from sqlalchemy import (Boolean, Column, DateTime, Float, ForeignKey, Integer, String, Table, Text,
                        UniqueConstraint)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


def now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)


def _iso(v):
    return v.isoformat() + "Z" if isinstance(v, dt.datetime) else v


class DictMixin:
    _hidden: tuple = ()

    def to_dict(self) -> dict:
        return {c.name: _iso(getattr(self, c.name)) for c in self.__table__.columns if c.name not in self._hidden}


subscriber_talkgroups = Table(
    "subscriber_talkgroups", Base.metadata,
    Column("subscriber_id", ForeignKey("subscribers.id", ondelete="CASCADE"), primary_key=True),
    Column("talkgroup_id", ForeignKey("talkgroups.id", ondelete="CASCADE"), primary_key=True),
)


subscriber_zones = Table(
    "subscriber_zones", Base.metadata,
    Column("subscriber_id", ForeignKey("subscribers.id", ondelete="CASCADE"), primary_key=True),
    Column("zone_id", ForeignKey("zones.id", ondelete="CASCADE"), primary_key=True),
)

console_zones = Table(
    "console_zones", Base.metadata,
    Column("console_id", ForeignKey("dispatch_consoles.id", ondelete="CASCADE"), primary_key=True),
    Column("zone_id", ForeignKey("zones.id", ondelete="CASCADE"), primary_key=True),
)


class User(DictMixin, Base):
    __tablename__ = "users"
    _hidden = ("password_hash",)
    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True)
    password_hash: Mapped[str] = mapped_column(String(256))
    role: Mapped[str] = mapped_column(String(16), default="admin")
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=now)


class AuthToken(Base):
    __tablename__ = "auth_tokens"
    token: Mapped[str] = mapped_column(String(128), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    expires_at: Mapped[dt.datetime] = mapped_column(DateTime)


class Site(DictMixin, Base):
    """A radio site = one edge base-processing device + its base station radios."""
    __tablename__ = "sites"
    id: Mapped[int] = mapped_column(primary_key=True)  # site number used on the wire (1..65535)
    name: Mapped[str] = mapped_column(String(64))
    location: Mapped[str] = mapped_column(String(128), default="")
    rfss_id: Mapped[int] = mapped_column(Integer, default=1)
    network_codec: Mapped[str] = mapped_column(String(8), default="AUTO")  # AUTO|ULAW|IMBE|AMBE2|PCM16
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    notes: Mapped[str] = mapped_column(Text, default="")
    callsign: Mapped[str] = mapped_column(String(16), default="")  # station ID sent in CW by the edge
    latitude: Mapped[float] = mapped_column(Float, default=0.0)
    longitude: Mapped[float] = mapped_column(Float, default=0.0)
    # runtime
    status: Mapped[str] = mapped_column(String(16), default="offline")  # offline|online|site_trunking
    last_seen: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)

    channels = relationship("Channel", back_populates="site", cascade="all, delete-orphan")
    devices = relationship("EdgeDevice", back_populates="site", cascade="all, delete-orphan")


class EdgeDevice(DictMixin, Base):
    __tablename__ = "edge_devices"
    _hidden = ("secret_hash",)
    id: Mapped[int] = mapped_column(primary_key=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id", ondelete="CASCADE"))
    hostname: Mapped[str] = mapped_column(String(64), default="")
    serial: Mapped[str] = mapped_column(String(64), default="")
    model: Mapped[str] = mapped_column(String(64), default="")
    agent_version: Mapped[str] = mapped_column(String(32), default="")
    wg_public_key: Mapped[str] = mapped_column(String(64), unique=True)
    wg_ip: Mapped[str] = mapped_column(String(32), unique=True)
    secret_hash: Mapped[str] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(String(16), default="pending")  # pending|active|revoked
    connected: Mapped[bool] = mapped_column(Boolean, default=False)
    enrolled_at: Mapped[dt.datetime] = mapped_column(DateTime, default=now)
    last_seen: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    telemetry: Mapped[str] = mapped_column(Text, default="{}")  # last heartbeat (cpu, temp, ...)

    site = relationship("Site", back_populates="devices")


class EnrollmentToken(DictMixin, Base):
    __tablename__ = "enrollment_tokens"
    token: Mapped[str] = mapped_column(String(64), primary_key=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id", ondelete="CASCADE"))
    auto_approve: Mapped[bool] = mapped_column(Boolean, default=False)
    expires_at: Mapped[dt.datetime] = mapped_column(DateTime)
    used_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    used_by_device: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=now)


class TalkGroup(DictMixin, Base):
    __tablename__ = "talkgroups"
    id: Mapped[int] = mapped_column(primary_key=True)  # TGID
    name: Mapped[str] = mapped_column(String(64))
    description: Mapped[str] = mapped_column(String(256), default="")
    priority: Mapped[int] = mapped_column(Integer, default=5)  # 1 (low) .. 10 (high)
    hang_time_ms: Mapped[int] = mapped_column(Integer, default=3000)
    emergency_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    allow_sip: Mapped[bool] = mapped_column(Boolean, default=False)
    # routing rules
    restricted: Mapped[bool] = mapped_column(Boolean, default=False)      # only radios that list this TG may talk
    affiliated_only: Mapped[bool] = mapped_column(Boolean, default=False) # repeat only at sites with an affiliated radio
    parrot: Mapped[bool] = mapped_column(Boolean, default=False)          # echo each transmission back (test TG)
    site_ids: Mapped[str] = mapped_column(String(256), default="")        # comma list of allowed sites; blank = all
    sip_extension: Mapped[str] = mapped_column(String(32), default="")
    allow_poc: Mapped[bool] = mapped_column(Boolean, default=True)        # PoC phones may use it (if permitted)
    record: Mapped[bool] = mapped_column(Boolean, default=True)           # record audio (when recording is on)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)


class Channel(DictMixin, Base):
    """A base station radio (RF channel) wired to an edge device via audio + COR/PTT."""
    __tablename__ = "channels"
    __table_args__ = (UniqueConstraint("site_id", "slot"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id", ondelete="CASCADE"))
    slot: Mapped[int] = mapped_column(Integer, default=1)  # interface port on the edge device
    name: Mapped[str] = mapped_column(String(64))
    mode: Mapped[str] = mapped_column(String(8), default="P25P1")  # SystemMode
    rx_mhz: Mapped[float] = mapped_column(Float, default=0.0)
    tx_mhz: Mapped[float] = mapped_column(Float, default=0.0)
    bandwidth_khz: Mapped[float] = mapped_column(Float, default=12.5)
    nac: Mapped[str] = mapped_column(String(4), default="293")  # P25 NAC (hex)
    color_code: Mapped[int] = mapped_column(Integer, default=1)  # DMR
    timeslot: Mapped[int] = mapped_column(Integer, default=1)    # DMR / P25P2
    ran: Mapped[int] = mapped_column(Integer, default=1)         # NXDN
    ctcss_hz: Mapped[float] = mapped_column(Float, default=0.0)  # analog
    talkgroup_id: Mapped[int | None] = mapped_column(ForeignKey("talkgroups.id", ondelete="SET NULL"), nullable=True)
    role: Mapped[str] = mapped_column(String(16), default="voice")  # voice|control|data
    # edge hardware mapping
    audio_device: Mapped[str] = mapped_column(String(64), default="default")
    ptt_gpio: Mapped[int] = mapped_column(Integer, default=17)
    cor_gpio: Mapped[int] = mapped_column(Integer, default=27)
    cor_active_low: Mapped[bool] = mapped_column(Boolean, default=True)
    tx_delay_ms: Mapped[int] = mapped_column(Integer, default=150)
    tx_tail_ms: Mapped[int] = mapped_column(Integer, default=200)
    rx_gain_db: Mapped[float] = mapped_column(Float, default=0.0)
    tx_gain_db: Mapped[float] = mapped_column(Float, default=0.0)
    default_unit_id: Mapped[int] = mapped_column(Integer, default=0)
    # radio interface signalling: GPIO pins, or a serial port's RTS (PTT) / CTS (COR) lines
    signaling: Mapped[str] = mapped_column(String(8), default="gpio")   # gpio | serial
    serial_port: Mapped[str] = mapped_column(String(64), default="")
    cor_invert: Mapped[bool] = mapped_column(Boolean, default=False)    # serial: CTS low = carrier
    # how the edge reaches this channel: an analog/vocoded radio (audio + COR/PTT), or packetized voice
    interface: Mapped[str] = mapped_column(String(12), default="radio")  # radio | dfsi | ipsc | capplus
    packet_config: Mapped[str] = mapped_column(Text, default="{}")      # JSON settings for packet interfaces
    extra_talkgroups: Mapped[str] = mapped_column(String(512), default="")  # packet: more TGs carried ("*" = all)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)

    site = relationship("Site", back_populates="channels")

    def to_dict(self):
        d = super().to_dict()
        try:
            d["packet_config"] = json.loads(self.packet_config or "{}")
        except ValueError:
            d["packet_config"] = {}
        return d


class Subscriber(DictMixin, Base):
    __tablename__ = "subscribers"
    id: Mapped[int] = mapped_column(primary_key=True)  # radio / unit id
    alias: Mapped[str] = mapped_column(String(64))
    mode: Mapped[str] = mapped_column(String(8), default="P25P1")
    model: Mapped[str] = mapped_column(String(64), default="")
    serial: Mapped[str] = mapped_column(String(64), default="")
    home_site_id: Mapped[int | None] = mapped_column(ForeignKey("sites.id", ondelete="SET NULL"), nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    notify_site_trunking: Mapped[bool] = mapped_column(Boolean, default=True)
    notes: Mapped[str] = mapped_column(Text, default="")
    last_site_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_tg_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_seen: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)

    poc_enabled: Mapped[bool] = mapped_column(Boolean, default=False)   # may use the PoC phone app

    talkgroups = relationship("TalkGroup", secondary=subscriber_talkgroups, lazy="selectin")
    zones = relationship("Zone", secondary=subscriber_zones, lazy="selectin")

    def to_dict(self):
        d = super().to_dict()
        d["talkgroup_ids"] = [t.id for t in self.talkgroups]
        d["zone_ids"] = [z.id for z in self.zones]
        return d


class Zone(DictMixin, Base):
    """An ordered set of talkgroups, like a zone in a radio codeplug. Radios, PoC phones and dispatch
    consoles are given zones; each shows only the talkgroups its radio ID may use."""
    __tablename__ = "zones"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(32))
    position: Mapped[int] = mapped_column(Integer, default=0)
    color: Mapped[str] = mapped_column(String(9), default="")
    notes: Mapped[str] = mapped_column(Text, default="")

    members = relationship("ZoneMember", order_by="ZoneMember.position", cascade="all, delete-orphan",
                           lazy="selectin")

    def to_dict(self):
        d = super().to_dict()
        d["talkgroup_ids"] = [m.talkgroup_id for m in self.members]
        return d


class ZoneMember(Base):
    __tablename__ = "zone_talkgroups"
    zone_id: Mapped[int] = mapped_column(ForeignKey("zones.id", ondelete="CASCADE"), primary_key=True)
    talkgroup_id: Mapped[int] = mapped_column(ForeignKey("talkgroups.id", ondelete="CASCADE"), primary_key=True)
    position: Mapped[int] = mapped_column(Integer, default=0)


class PocDevice(DictMixin, Base):
    """A phone running the KeyUp PoC app, acting as the subscriber radio it is bound to."""
    __tablename__ = "poc_devices"
    _hidden = ("token_hash", "activation_hash")
    id: Mapped[int] = mapped_column(primary_key=True)
    subscriber_id: Mapped[int] = mapped_column(ForeignKey("subscribers.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(64), default="")
    platform: Mapped[str] = mapped_column(String(16), default="")
    app_version: Mapped[str] = mapped_column(String(16), default="")
    token_hash: Mapped[str] = mapped_column(String(64), default="", index=True)
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=now)
    last_seen: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)


class DispatchConsole(DictMixin, Base):
    """A PC running the KeyUp Dispatch Console. It transmits as one subscriber radio ID."""
    __tablename__ = "dispatch_consoles"
    _hidden = ("token_hash", "activation_hash")
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(64))
    subscriber_id: Mapped[int] = mapped_column(ForeignKey("subscribers.id", ondelete="CASCADE"), index=True)
    can_inhibit: Mapped[bool] = mapped_column(Boolean, default=True)      # may inhibit / uninhibit radios
    call_priority: Mapped[bool] = mapped_column(Boolean, default=True)    # may talk over radios
    can_patch: Mapped[bool] = mapped_column(Boolean, default=True)        # may create talkgroup patches
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    wg_public_key: Mapped[str] = mapped_column(String(64), default="")
    wg_ip: Mapped[str] = mapped_column(String(45), default="")
    token_hash: Mapped[str] = mapped_column(String(64), default="", index=True)
    activation_hash: Mapped[str] = mapped_column(String(128), default="")
    activation_expires: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    activated_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    last_seen: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    notes: Mapped[str] = mapped_column(Text, default="")

    zones = relationship("Zone", secondary=console_zones, lazy="selectin")

    def to_dict(self):
        d = super().to_dict()
        d["zone_ids"] = [z.id for z in self.zones]
        d["activated"] = bool(self.token_hash)
        return d


class BandPlanEntry(DictMixin, Base):
    """Channel identity: channel number n is base + n * spacing; the receive (input) is offset from transmit."""
    __tablename__ = "band_plan"
    id: Mapped[int] = mapped_column(primary_key=True)  # identity 0..15
    name: Mapped[str] = mapped_column(String(64), default="")
    base_mhz: Mapped[float] = mapped_column(Float)
    spacing_khz: Mapped[float] = mapped_column(Float, default=6.25)
    offset_mhz: Mapped[float] = mapped_column(Float, default=5.0)
    bandwidth_khz: Mapped[float] = mapped_column(Float, default=12.5)


class TalkgroupPatch(DictMixin, Base):
    """Links two talkgroups: a call on either is heard on both (across modes; the router transcodes)."""
    __tablename__ = "talkgroup_patches"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(64))
    tg_a: Mapped[int] = mapped_column(ForeignKey("talkgroups.id", ondelete="CASCADE"))
    tg_b: Mapped[int] = mapped_column(ForeignKey("talkgroups.id", ondelete="CASCADE"))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)


class SipRoute(DictMixin, Base):
    """Outbound/permanent SIP patches. Inbound calls use TalkGroup.sip_extension."""
    __tablename__ = "sip_routes"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(64))
    endpoint: Mapped[str] = mapped_column(String(128))  # e.g. PJSIP/1001 or PJSIP/5551234@carrier
    talkgroup_id: Mapped[int] = mapped_column(ForeignKey("talkgroups.id", ondelete="CASCADE"))
    ptt_mode: Mapped[str] = mapped_column(String(8), default="dtmf")  # dtmf|vox
    vox_threshold: Mapped[int] = mapped_column(Integer, default=900)
    auto_connect: Mapped[bool] = mapped_column(Boolean, default=False)  # permanent patch
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)


class CallRecord(DictMixin, Base):
    __tablename__ = "call_records"
    id: Mapped[int] = mapped_column(primary_key=True)
    call_id: Mapped[int] = mapped_column(Integer, index=True)
    talkgroup_id: Mapped[int] = mapped_column(Integer, index=True)
    source_id: Mapped[int] = mapped_column(Integer, default=0)
    source_site_id: Mapped[int] = mapped_column(Integer, default=0)
    source_channel_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source_codec: Mapped[str] = mapped_column(String(8), default="")
    dest_sites: Mapped[str] = mapped_column(Text, default="[]")
    started_at: Mapped[dt.datetime] = mapped_column(DateTime, default=now, index=True)
    ended_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    duration_s: Mapped[float] = mapped_column(Float, default=0.0)
    frames: Mapped[int] = mapped_column(Integer, default=0)
    transcoded: Mapped[bool] = mapped_column(Boolean, default=False)
    emergency: Mapped[bool] = mapped_column(Boolean, default=False)
    via_sip: Mapped[bool] = mapped_column(Boolean, default=False)
    site_trunking: Mapped[bool] = mapped_column(Boolean, default=False)
    result: Mapped[str] = mapped_column(String(32), default="granted")  # granted|denied:<reason>|preempted
    talkers: Mapped[str] = mapped_column(Text, default="[]")    # [{src, alias, at (s into call), dur, via}]
    recording: Mapped[str] = mapped_column(String(256), default="")   # file under the recordings folder
    recording_bytes: Mapped[int] = mapped_column(Integer, default=0)
    recording_note: Mapped[str] = mapped_column(String(128), default="")  # e.g. why there is no audio
    ship_status: Mapped[str] = mapped_column(String(16), default="")      # ""|queued|sent|failed:<why>
    shipped_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)

    def to_dict(self):
        d = super().to_dict()
        d["dest_sites"] = json.loads(self.dest_sites or "[]")
        d["talkers"] = json.loads(self.talkers or "[]")
        return d


class SystemSetting(Base):
    __tablename__ = "system_settings"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text)


DEFAULT_SYSTEM_SETTINGS = {
    "system_name": "KeyUp",
    "wacn": "BEE00",
    "system_id": "1A1",
    "require_known_subscriber": "false",
    "site_trunking_timeout_s": "10",
    "site_trunking_announce_interval_s": "300",
    "site_trunking_message": "SITE TRUNKING",
    "wide_area_message": "WIDE AREA RESTORED",
    "default_hang_time_ms": "3000",
    "max_call_s": "120",
    "affiliation_timeout_s": "1800",   # a radio counts as affiliated at a site this long after it was heard
    "cw_id_interval_s": "600",         # edges identify each transmitter in CW at most this often
    "parrot_delay_ms": "1000",
    # call recording
    "recording_enabled": "true",
    "recording_retention_days": "30",
    "recording_max_gb": "20",
    # ship recordings to a second server: off | siprec | sip | https
    "recording_ship_mode": "off",
    "recording_ship_target": "",        # siprec/sip: sip:recorder@host[:port]; https: URL
    "recording_ship_transport": "udp",  # sip: udp | tcp
    "recording_ship_username": "",
    "recording_ship_password": "",
    "recording_ship_token": "",         # https: bearer token
}
