from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field, model_validator

Mode = Literal["P25P1", "P25P2", "DMR", "NXDN", "ANALOG"]


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
    restricted: bool = False
    affiliated_only: bool = False
    parrot: bool = False
    site_ids: str = Field("", pattern=r"^[0-9, ]*$")
    sip_extension: str = ""
    allow_poc: bool = True
    record: bool = True
    enabled: bool = True


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
    signaling: Literal["gpio", "serial"] = "gpio"
    serial_port: str = ""
    cor_invert: bool = False
    interface: Literal["radio", "dfsi", "ipsc", "capplus"] = "radio"
    packet_config: dict = {}
    extra_talkgroups: str = Field("", pattern=r"^(\*|[0-9, ]*)$")
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
    zone_ids: list[int] = []
    poc_enabled: bool = False


class ZoneIn(BaseModel):
    name: str = Field(min_length=1, max_length=32)
    position: int = 0
    color: str = Field("", pattern=r"^(#[0-9A-Fa-f]{6})?$")
    notes: str = ""
    talkgroup_ids: list[int] = []


class ConsoleIn(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    subscriber_id: int
    zone_ids: list[int] = []
    can_inhibit: bool = True
    call_priority: bool = True
    can_patch: bool = True
    enabled: bool = True
    notes: str = ""


class ActivateIn(BaseModel):
    code: str
    platform: str = ""
    app: str = ""
    name: str = ""


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


class BandPlanIn(BaseModel):
    id: int = Field(ge=0, le=15)
    name: str = ""
    base_mhz: float = Field(gt=0)
    spacing_khz: float = Field(6.25, gt=0)
    offset_mhz: float = 5.0
    bandwidth_khz: float = 12.5


class PatchIn(BaseModel):
    """A patch of two or more talkgroups. Send `talkgroups`; `tg_a` / `tg_b` alone still work."""
    name: str
    talkgroups: list[int] = []
    tg_a: int = 0
    tg_b: int = 0
    enabled: bool = True

    @model_validator(mode="after")
    def _members(self):
        tgs: list[int] = []
        for tg in (self.talkgroups or [self.tg_a, self.tg_b]):
            if tg and int(tg) not in tgs:
                tgs.append(int(tg))
        if len(tgs) < 2:
            raise ValueError("a patch needs at least two different talkgroups")
        self.talkgroups, self.tg_a, self.tg_b = tgs, tgs[0], tgs[1]
        return self


class PocActivateIn(BaseModel):
    unit: int
    code: str
    device_name: str = ""
    platform: str = ""
    app_version: str = ""
