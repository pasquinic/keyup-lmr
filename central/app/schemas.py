from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field

Mode = Literal["P25P1", "P25P2", "DMR", "NXDN", "ANALOG"]  # P25P2 only in native backend


class Login(BaseModel):
    username: str
    password: str


class SiteIn(BaseModel):
    id: int = Field(ge=1, le=65535)
    name: str
    location: str = ""
    rfss_id: int = 1
    network_codec: Literal["AUTO", "ULAW", "PCM16", "IMBE", "AMBE2"] = "AUTO"
    enabled: bool = True
    notes: str = ""
    callsign: str = ""
    latitude: float = 0.0
    longitude: float = 0.0


class TalkGroupIn(BaseModel):
    id: int = Field(ge=1, le=16777215)
    name: str
    description: str = ""
    priority: int = Field(5, ge=1, le=10)
    hang_time_ms: int = Field(3000, ge=0, le=30000)
    emergency_enabled: bool = True
    allow_sip: bool = False
    sip_extension: str = ""
    enabled: bool = True
    dmr_slot: int = Field(1, ge=1, le=2)
    affiliated_only: bool = False
    restricted: bool = False
    parrot: bool = False
    site_ids: str = ""
    bridge_mode: Literal["P25P1", "DMR", "ANALOG"] = "P25P1"


class ChannelIn(BaseModel):
    site_id: int
    slot: int = Field(1, ge=1, le=16)
    name: str
    mode: Mode = "P25P1"
    rx_mhz: float = 0.0
    tx_mhz: float = 0.0
    bandwidth_khz: float = 12.5
    nac: str = "293"
    color_code: int = Field(1, ge=0, le=15)
    timeslot: int = Field(1, ge=1, le=2)
    ran: int = Field(1, ge=0, le=63)
    ctcss_hz: float = 0.0
    talkgroup_id: Optional[int] = None
    role: Literal["voice", "control", "data"] = "voice"
    audio_device: str = "default"
    ptt_gpio: int = 17
    cor_gpio: int = 27
    cor_active_low: bool = True
    tx_delay_ms: int = 150
    tx_tail_ms: int = 200
    rx_gain_db: float = 0.0
    tx_gain_db: float = 0.0
    default_unit_id: int = 0
    enabled: bool = True
    peer_id: Optional[int] = None
    modem_type: Literal["uart", "null"] = "uart"
    modem_mode: Literal["air", "dfsi"] = "air"
    modem_port: str = "/dev/ttyUSB0"
    modem_speed: int = 115200
    rx_level: int = Field(50, ge=0, le=100)
    tx_level: int = Field(50, ge=0, le=100)
    power_w: int = 10


class IdenIn(BaseModel):
    id: int = Field(ge=0, le=15)
    name: str = ""
    base_mhz: float
    spacing_khz: float = 6.25
    offset_mhz: float = 5.0
    bandwidth_khz: float = 12.5


class PatchIn(BaseModel):
    name: str
    tg_a: int
    mode_a: Literal["P25P1", "DMR", "ANALOG"] = "DMR"
    tg_b: int
    mode_b: Literal["P25P1", "DMR", "ANALOG"] = "P25P1"
    enabled: bool = True


class SubscriberIn(BaseModel):
    id: int = Field(ge=1, le=16777215)
    alias: str
    mode: Mode = "P25P1"
    model: str = ""
    serial: str = ""
    home_site_id: Optional[int] = None
    enabled: bool = True
    notify_site_trunking: bool = True
    notes: str = ""
    talkgroup_ids: list[int] = []


class SipRouteIn(BaseModel):
    name: str
    endpoint: str
    talkgroup_id: int
    ptt_mode: Literal["dtmf", "vox"] = "dtmf"
    vox_threshold: int = 900
    auto_connect: bool = False
    enabled: bool = True


class TokenIn(BaseModel):
    site_id: int
    auto_approve: bool = False
    valid_hours: int = Field(24, ge=1, le=720)


class EnrollIn(BaseModel):
    token: str
    wg_public_key: str
    hostname: str = ""
    serial: str = ""
    model: str = ""
    agent_version: str = ""


class OriginateIn(BaseModel):
    endpoint: str
    talkgroup_id: int
    ptt_mode: Literal["dtmf", "vox"] = "dtmf"
    caller_id: str = "LMR"
