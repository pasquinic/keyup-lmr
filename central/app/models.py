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
    callsign: Mapped[str] = mapped_column(String(16), default="")  # CW ID (blank = off)
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
    sip_extension: Mapped[str] = mapped_column(String(32), default="")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    # DVM talkgroup rules
    dmr_slot: Mapped[int] = mapped_column(Integer, default=1)
    affiliated_only: Mapped[bool] = mapped_column(Boolean, default=False)  # only repeat where affiliated
    restricted: Mapped[bool] = mapped_column(Boolean, default=False)       # only subscribers that list this TG may transmit
    parrot: Mapped[bool] = mapped_column(Boolean, default=False)
    site_ids: Mapped[str] = mapped_column(String(256), default="")         # comma list; blank = all sites
    bridge_mode: Mapped[str] = mapped_column(String(8), default="P25P1")   # mode the SIP/PoC bridge speaks on this TG
    allow_poc: Mapped[bool] = mapped_column(Boolean, default=True)         # selectable by PoC (cellular) subscribers


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
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    # DVM (dvmhost / dvmbridge) settings
    peer_id: Mapped[int | None] = mapped_column(Integer, unique=True, nullable=True)  # FNE peer id (auto)
    modem_type: Mapped[str] = mapped_column(String(8), default="uart")   # uart | null (lab)
    modem_mode: Mapped[str] = mapped_column(String(8), default="air")    # air (DVM-V1/hotspot) | dfsi (V.24)
    modem_port: Mapped[str] = mapped_column(String(64), default="/dev/ttyUSB0")
    modem_speed: Mapped[int] = mapped_column(Integer, default=115200)
    rx_level: Mapped[int] = mapped_column(Integer, default=50)
    tx_level: Mapped[int] = mapped_column(Integer, default=50)
    power_w: Mapped[int] = mapped_column(Integer, default=10)

    site = relationship("Site", back_populates="channels")


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
    # Push-to-talk over cellular: the phone app is this radio ID on the system
    poc_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    poc_activation_hash: Mapped[str] = mapped_column(String(128), default="")
    poc_activation_expires: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    last_site_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_tg_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_seen: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)

    talkgroups = relationship("TalkGroup", secondary=subscriber_talkgroups, lazy="selectin")
    _hidden = ("poc_activation_hash",)

    def to_dict(self):
        d = super().to_dict()
        d["talkgroup_ids"] = [t.id for t in self.talkgroups]
        return d


class PocDevice(DictMixin, Base):
    """A phone activated as a subscriber radio (one radio ID may have a few devices)."""
    __tablename__ = "poc_devices"
    _hidden = ("token_hash",)
    id: Mapped[int] = mapped_column(primary_key=True)
    subscriber_id: Mapped[int] = mapped_column(ForeignKey("subscribers.id", ondelete="CASCADE"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    name: Mapped[str] = mapped_column(String(64), default="")
    platform: Mapped[str] = mapped_column(String(16), default="")
    app_version: Mapped[str] = mapped_column(String(16), default="")
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=now)
    last_seen: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)


class IdenEntry(DictMixin, Base):
    """Band plan identity (DVM iden_table.dat): maps channel numbers to frequencies."""
    __tablename__ = "iden_table"
    id: Mapped[int] = mapped_column(primary_key=True)  # channel identity 0..15
    name: Mapped[str] = mapped_column(String(64), default="")
    base_mhz: Mapped[float] = mapped_column(Float)
    spacing_khz: Mapped[float] = mapped_column(Float, default=6.25)
    offset_mhz: Mapped[float] = mapped_column(Float, default=5.0)  # RX (input) = TX + offset
    bandwidth_khz: Mapped[float] = mapped_column(Float, default=12.5)


class Patch(DictMixin, Base):
    """Cross-mode patch: two dvmbridge instances back to back (e.g. DMR TG 1 <-> P25 TG 100)."""
    __tablename__ = "patches"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(64))
    tg_a: Mapped[int] = mapped_column(Integer)
    mode_a: Mapped[str] = mapped_column(String(8), default="DMR")
    tg_b: Mapped[int] = mapped_column(Integer)
    mode_b: Mapped[str] = mapped_column(String(8), default="P25P1")
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

    def to_dict(self):
        d = super().to_dict()
        d["dest_sites"] = json.loads(self.dest_sites or "[]")
        return d


class SystemSetting(Base):
    __tablename__ = "system_settings"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text)


DEFAULT_SYSTEM_SETTINGS = {
    "system_name": "KeyUp",
    "wacn": "BB800",                     # P25 WACN / DVM netId (hex)
    "system_id": "1A1",                 # P25 system id / DVM sysId (hex)
    "require_known_subscriber": "false",
    "site_trunking_timeout_s": "10",
    "site_trunking_announce_interval_s": "300",
    "site_trunking_message": "SITE TRUNKING",
    "wide_area_message": "WIDE AREA RESTORED",
    "default_hang_time_ms": "3000",
    "max_call_s": "120",
    # DVM foundation
    "call_backend": "dvm",               # dvm (DVMProject FNE + dvmhost) | native (KeyUp router)
    "dvm_license_acknowledged": "false", # must be set by the operator (DVM iAgreeNotToBeStupid)
    "fne_peer_id": "9000100",
    "fne_address": "10.77.0.1",          # FNE address as seen by edges (tunnel IP)
    "fne_port": "62031",
    "dmr_net_id": "1",
}
