"""DVMProject integration: KeyUp is the management plane, DVM is the radio/network plane.

KeyUp's database (sites, channels, band plan, talkgroups, subscribers, patches) is
compiled into DVM configuration:

  central (FNE container, <DVM_DIR>/instances/...)
    fne/            dvmfne config.yml, talkgroup_rules.yml, rid_acl.dat, peer_list.dat,
                    adj_site_map.yml, iden_table.dat
    ic-tg<N>/       "interconnect" dvmbridge per talkgroup used by SIP or PoC (cellular) subscribers:
                    raw PCM over UDP with srcId metadata, so a phone transmits as its own radio ID
    patch-<id>-a/b  two dvmbridges back to back = cross-mode patch (e.g. DMR TG <-> P25 TG)

  edge (pushed over the WireGuard control link to each site's Pi)
    ch<slot>-...    dvmhost per RF channel (DVM-V1 / hotspot modem, or V.24 DFSI)
    ana<slot>-...   dvmbridge per analog base radio (sound card + serial RTS PTT / CTS COR)

DVM itself does the air interface, trunking control channels, affiliation, call
routing and -- when an edge loses the FNE -- native site trunking (the P25/NXDN/DMR
site broadcasts drop the "network active" flag, so subscriber radios show SITE TRUNKING).
"""
from __future__ import annotations

import asyncio
import datetime as dt
import glob
import hashlib
import hmac
import json
import logging
import os
import re
import time

import httpx

from ..config import settings
from ..db import session_scope
from ..models import (CallRecord, Channel, DispatchConsole, IdenEntry, Patch, Site, Subscriber, TalkGroup, now)
from .events import bus

log = logging.getLogger("dvm")

DVM_REF = "435ff45b367887ffa94c89cb1a2779628e0f7bfb+keyup.1"  # upstream base commit of the dvm/dvmhost fork
MODE_PROTO = {"P25P1": "p25", "DMR": "dmr", "NXDN": "nxdn"}
BRIDGE_TXMODE = {"DMR": 1, "P25P1": 2, "ANALOG": 3}
FNE_INSTANCE = "fne"
IC_RECV_BASE, IC_SEND_BASE = 32000, 34000     # dvmbridge listens on RECV, sends to KeyUp on SEND
PATCH_PORT_BASE = 36000


# ====================================================================== YAML emitter
def _scalar(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v) if isinstance(v, float) else str(v)
    s = str(v)
    if re.fullmatch(r"[A-Za-z0-9_./:@+-]+", s) and not re.fullmatch(r"-?[0-9.]+|true|false|null|~", s, re.I):
        return s
    s = s.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{s}"'


def _emit_map(d: dict, ind: int) -> list[str]:
    out = []
    pad = " " * ind
    for k, v in d.items():
        if v is None or v == "":
            continue  # DVM's YAML parser accepts neither "" nor a bare "key:"; omitted keys take DVM defaults
        if isinstance(v, dict):
            out.append(f"{pad}{k}:")
            out += _emit_map(v, ind + 4)
        elif isinstance(v, list):
            if not v:
                out.append(f"{pad}{k}: []")
                continue
            out.append(f"{pad}{k}:")
            for item in v:
                if isinstance(item, dict):
                    lines = _emit_map(item, ind + 4)
                    lines[0] = " " * (ind + 2) + "- " + lines[0].lstrip()
                    out += lines
                else:
                    out.append(f"{pad}  - {_scalar(item)}")
        else:
            out.append(f"{pad}{k}: {_scalar(v)}")
    return out


def to_yaml(d: dict, header: str = "") -> str:
    head = "".join(f"# {l}\n" for l in header.splitlines()) if header else ""
    return head + "\n".join(_emit_map(d, 0)) + "\n"


# ====================================================================== helpers
def secret(kind: str, ident) -> str:
    """Deterministic per-peer secret, derived from SECRET_KEY (nothing extra to store)."""
    return hmac.new(settings.secret_key.encode(), f"{kind}:{ident}".encode(), hashlib.sha256).hexdigest()[:24]


def console_password(peer_id: int) -> str:
    return secret("console", peer_id)


def default_peer_id(site_id: int, slot: int) -> int:
    return 100000 + site_id * 100 + slot


def edge_ports(site_id: int, slot: int) -> tuple[int, int]:
    """(rpc, rest) local ports for a dvmhost; unique per site so several sites can share a lab box."""
    rpc = 20000 + (site_id % 50) * 200 + slot
    return rpc, rpc + 100


def _hex(v, default="1") -> str:
    try:
        return format(int(str(v), 16), "X")
    except (TypeError, ValueError):
        return default


def _int(v, default=0) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def channel_number(tx_mhz: float, rx_mhz: float, idens: list[dict]) -> tuple[dict | None, int, list[str]]:
    """Find the iden entry + channel number for a TX frequency, and sanity-check RX."""
    warns: list[str] = []
    tx_hz = round(tx_mhz * 1e6)
    for iden in idens:
        base = round(iden["base_mhz"] * 1e6)
        step = iden["spacing_khz"] * 1000
        if tx_hz < base or step <= 0:
            continue
        n = (tx_hz - base) / step
        if abs(n - round(n)) > 1e-6 or round(n) > 4095:
            continue
        n = int(round(n))
        exp_rx = tx_hz + round(iden["offset_mhz"] * 1e6)
        if rx_mhz and abs(round(rx_mhz * 1e6) - exp_rx) > 1:
            warns.append(f"RX {rx_mhz:.5f} MHz doesn't match band plan {iden['id']} offset "
                         f"({iden['offset_mhz']:+.4f} MHz -> {exp_rx / 1e6:.5f})")
        return iden, n, warns
    return None, 0, [f"TX {tx_mhz:.5f} MHz is not on any band plan entry (add one under Band Plan)"]


# ====================================================================== the plan
class DvmPlan:
    """Snapshot of the database, compiled into DVM configuration files."""

    def __init__(self, db):
        from .call_controller import get_system_settings
        self.s = get_system_settings(db)
        self.ack = self.s.get("dvm_license_acknowledged") == "true"
        self.sites = {x.id: x.to_dict() for x in db.query(Site).all()}
        self.channels = [c.to_dict() for c in db.query(Channel).order_by(Channel.site_id, Channel.slot).all()]
        self.tgs = [t.to_dict() for t in db.query(TalkGroup).order_by(TalkGroup.id).all()]
        self.subs = [s.to_dict() for s in db.query(Subscriber).order_by(Subscriber.id).all()]
        self.poc_users = [s for s in self.subs if s.get("poc_enabled") and s.get("enabled")]
        self.idens = [i.to_dict() for i in db.query(IdenEntry).order_by(IdenEntry.id).all()]
        self.patches = [p.to_dict() for p in db.query(Patch).order_by(Patch.id).all()]
        self.consoles = [c.to_dict() for c in db.query(DispatchConsole).order_by(DispatchConsole.id).all()
                         if c.enabled]
        self.warnings: dict[str, list[str]] = {}
        if _hex(self.s.get("wacn"), "BB800") == "BEE00":
            self.warn("system", "WACN BEE00 is refused by DVM (dvmhost/dvmbridge exit); choose another WACN")
        for c in self.channels:
            if not c.get("peer_id"):
                c["peer_id"] = default_peer_id(c["site_id"], c["slot"])

    # -------------------------------------------------------------- shared identity
    @property
    def fne_host(self) -> str:
        return self.s.get("fne_address", "10.77.0.1")

    @property
    def fne_port(self) -> int:
        return _int(self.s.get("fne_port"), 62031)

    def warn(self, key: str, msg: str) -> None:
        self.warnings.setdefault(key, []).append(msg)

    def poc_talkgroups(self, sub: dict) -> list[dict]:
        """Talkgroups a PoC subscriber may use -- the same permission rules as a radio:
        its permitted TG list (empty = any), restricted TGs only if listed, TG enabled + PoC-capable."""
        mine = set(sub.get("talkgroup_ids") or [])
        out = []
        for t in self.tgs:
            if not t["enabled"] or not t.get("allow_poc", True) or t.get("bridge_mode") not in BRIDGE_TXMODE:
                continue
            if mine and t["id"] not in mine:
                continue
            if t.get("restricted") and t["id"] not in mine:
                continue
            out.append(t)
        return out

    def console_talkgroups(self, con: dict) -> list[dict]:
        """Talkgroups a dispatch console gets: what its radio ID may use (same rules as a radio),
        narrowed to the console's own list when one is set."""
        sub = next((s for s in self.subs if s["id"] == con["subscriber_id"]), None)
        if sub is None:
            return []
        mine = set(sub.get("talkgroup_ids") or [])
        pick = set(con.get("talkgroup_ids") or [])
        out = []
        for t in self.tgs:
            if not t["enabled"] or (mine and t["id"] not in mine) or (t.get("restricted") and t["id"] not in mine):
                continue
            if pick and t["id"] not in pick:
                continue
            out.append(t)
        return out

    def console_peers_by_tg(self) -> dict[int, list[int]]:
        out: dict[int, list[int]] = {}
        for c in self.consoles:
            for t in self.console_talkgroups(c):
                out.setdefault(t["id"], []).append(c["fne_peer_id"])
        return out

    def ic_tgs(self) -> list[dict]:
        """Talkgroups that need an interconnect bridge: SIP-enabled, or usable by an active PoC subscriber."""
        poc = {t["id"] for s in self.poc_users for t in self.poc_talkgroups(s)}
        return [t for t in self.tgs if t["enabled"] and (t["allow_sip"] or t["id"] in poc)]

    def ic_ports(self, tg_id: int) -> tuple[int, int]:
        idx = [t["id"] for t in self.ic_tgs()].index(tg_id)
        return IC_RECV_BASE + idx, IC_SEND_BASE + idx

    def ic_peer(self, tg_id: int) -> int:
        return 9100000 + [t["id"] for t in self.ic_tgs()].index(tg_id)

    def bridge_peers(self) -> list[tuple[int, str, dict]]:
        """(peer_id, alias, meta) for every central dvmbridge."""
        out = []
        for t in self.ic_tgs():
            out.append((self.ic_peer(t["id"]), f"IC-TG{t['id']}", {"tg": t["id"], "interconnect": True}))
        for p in self.patches:
            if p["enabled"]:
                out.append((9200000 + p["id"] * 2, f"PATCH{p['id']}-A", {"tg": p["tg_a"]}))
                out.append((9200000 + p["id"] * 2 + 1, f"PATCH{p['id']}-B", {"tg": p["tg_b"]}))
        return out

    # -------------------------------------------------------------- FNE lookup files
    def iden_table(self) -> str:
        lines = ["# KeyUp generated band plan: ChId,Base Freq,Spacing (khz),Input Offset (mhz),Bandwidth (khz),"]
        for i in self.idens:
            lines.append(f"{i['id']},{round(i['base_mhz'] * 1e6)},{i['spacing_khz']:g},"
                         f"{i['offset_mhz']:.5f},{i['bandwidth_khz']:g},")
        return "\n".join(lines) + "\n"

    def rid_acl(self) -> str:
        lines = ["# KeyUp generated radio ID list: RID,Enabled,Alias,IP,"]
        for s in self.subs:
            alias = re.sub(r"[,\n]", " ", s["alias"])[:32]
            lines.append(f"{s['id']},{1 if s['enabled'] else 0},{alias},,")
        return "\n".join(lines) + "\n\n"

    def peer_list(self) -> str:
        lines = ["# KeyUp generated peer ACL: PeerId,Password,Replica,Alias,Keys,Inhibit,Priority,Jitter,MaxFrames,MaxWait,"]
        for c in self.channels:
            if c["enabled"]:
                alias = f"{self.sites.get(c['site_id'], {}).get('name', '?')} {c['name']}"
                alias = re.sub(r"[,\n]", " ", alias)[:32]
                lines.append(f"{c['peer_id']},{secret('peer', c['peer_id'])},0,{alias},0,0,0,0,4,40000,")
        for pid, alias, _ in self.bridge_peers():
            lines.append(f"{pid},{secret('peer', pid)},0,{alias},0,0,0,0,4,40000,")
        for c in self.consoles:
            alias = re.sub(r"[,\n]", " ", f"CONSOLE {c['name']}")[:32]
            lines.append(f"{c['fne_peer_id']},{console_password(c['fne_peer_id'])},0,{alias},0,"
                         f"{1 if c['can_inhibit'] else 0},{1 if c['call_priority'] else 0},0,4,40000,")
        return "\n".join(lines) + "\n\n"

    def _site_peers(self, site_ids: list[int]) -> list[int]:
        return [c["peer_id"] for c in self.channels if c["site_id"] in site_ids and c["enabled"]]

    def talkgroup_rules(self) -> str:
        members: dict[int, list[int]] = {}
        for pid, _, meta in self.bridge_peers():
            members.setdefault(meta["tg"], []).append(pid)
        tg_rids: dict[int, list[int]] = {}
        for s in self.subs:
            for tg in s.get("talkgroup_ids", []):
                tg_rids.setdefault(tg, []).append(s["id"])
        consoles = self.console_peers_by_tg()
        groups = []
        for t in self.tgs:
            site_ids = [int(x) for x in str(t.get("site_ids") or "").split(",") if x.strip().isdigit()]
            con = consoles.get(t["id"], [])
            inclusion = (self._site_peers(site_ids) + members.get(t["id"], []) + con) if site_ids else []
            groups.append({
                "name": t["name"][:40],
                "alias": (t.get("description") or t["name"])[:40],
                "config": {
                    "active": bool(t["enabled"]),
                    "affiliated": bool(t.get("affiliated_only")),
                    "parrot": bool(t.get("parrot")),
                    "inclusion": inclusion,
                    "exclusion": [],
                    "rewrite": [],
                    "always": con,   # dispatch consoles never affiliate; always send them their talkgroups
                    "preferred": [],
                    "rid_permitted": tg_rids.get(t["id"], []) if t.get("restricted") else [],
                },
                "source": {"tgid": t["id"], "slot": _int(t.get("dmr_slot"), 1) or 1},
            })
        return to_yaml({"groupVoice": groups}, "KeyUp generated talkgroup rules -- edit in the KeyUp web UI")

    def adj_site_map(self) -> str:
        ccs = {}
        for c in self.channels:
            if c["enabled"] and c["role"] == "control" and c["mode"] in MODE_PROTO:
                ccs.setdefault(c["site_id"], c["peer_id"])
        peers = [{"peerId": pid, "active": True, "neighbors": [p for s, p in ccs.items() if s != sid]}
                 for sid, pid in ccs.items()]
        return to_yaml({"peers": peers}, "KeyUp generated adjacent site map")

    def fne_config(self) -> str:
        cfg = {
            "daemon": False,
            "iAgreeNotToBeStupid": self.ack,
            "log": {"displayLevel": 2, "fileLevel": 2, "useSyslog": False, "filePath": "log",
                    "activityFilePath": "log", "fileRoot": "FNE"},
            "master": {
                "peerId": _int(self.s.get("fne_peer_id"), 9000100), "address": "0.0.0.0", "port": self.fne_port,
                "password": secret("master", "fne"), "verbose": False, "packetDump": False, "debug": False,
                "logDenials": True, "logUpstreamCallStartEnd": True, "workers": 8, "connectionLimit": 200,
                "enableSpanningTree": True, "reportPeerPing": True, "encrypted": False,
                "allowDMRTraffic": True, "allowP25Traffic": True, "allowNXDNTraffic": True,
                "allowAnalogTraffic": True, "parrotDelay": 2000, "parrotGrantDemand": True,
                "callCollisionTimeout": 5, "rejectUnknownRID": self.s.get("require_known_subscriber") == "true",
                "filterTerminators": True, "enableInflux": False,
                "crypto_container": {"enable": False, "file": "key_container.ekc", "password": "", "time": 30},
                "talkgroup_rules": {"file": "talkgroup_rules.yml", "time": 1},
                "adj_site_map": {"file": "adj_site_map.yml", "time": 1},
            },
            "peers": [],
            "system": {
                "identity": re.sub(r"[^A-Za-z0-9]", "", self.s.get("system_name", "KEYUP"))[:8].upper() or "KEYUP",
                "pingTime": 5, "maxMissedPings": 10, "aclRuleUpdateTime": 10, "sendTalkgroups": True,
                "restEnable": True, "restAddress": "127.0.0.1", "restPort": settings.fne_rest_port,
                "restSsl": False, "restPassword": settings.fne_rest_password, "restDebug": False,
                "radio_id": {"file": "rid_acl.dat", "time": 1},
                "peer_acl": {"enable": True, "file": "peer_list.dat", "time": 1},
            },
            "vtun": {"enable": False},
        }
        return to_yaml(cfg, "KeyUp generated dvmfne configuration -- edit in the KeyUp web UI")

    def fne_instance(self) -> dict:
        return {"binary": "dvmfne", "args": ["-f", "-c", "config.yml"],
                "env": {"KEYUP_EVENTS_FILE": "log/keyup-events.jsonl"}, "files": {
            "config.yml": self.fne_config(), "talkgroup_rules.yml": self.talkgroup_rules(),
            "rid_acl.dat": self.rid_acl(), "peer_list.dat": self.peer_list(),
            "adj_site_map.yml": self.adj_site_map(), "iden_table.dat": self.iden_table()}}

    # -------------------------------------------------------------- dvmbridge
    def bridge_config(self, *, peer_id: int, identity: str, tg: int, mode: str, slot: int = 1,
                      fne_host: str = "127.0.0.1", udp: tuple[int, int] | None = None,
                      local_audio: bool = False, serial_port: str = "", src_id: int = 0,
                      grant_demand: bool = False, metadata: bool = False) -> str:
        """metadata=True: raw PCM16 UDP framed as [len][pcm][dstId][srcId] so the sender picks the
        source radio ID per call (PoC/SIP). False: RTP G.711 (bridge-to-bridge patches)."""
        if mode not in BRIDGE_TXMODE:
            self.warn(identity, f"dvmbridge cannot speak {mode}; using P25")
            mode = "P25P1"
        system = {
            "identity": identity[:8], "netId": _hex(self.s.get("wacn"), "BB800"),
            "sysId": _hex(self.s.get("system_id"), "1"), "grantDemand": grant_demand,
            "txMode": BRIDGE_TXMODE[mode], "rxAudioGain": 1.0, "vocoderDecoderAudioGain": 3.0,
            "vocoderDecoderAutoGain": False, "txAudioGain": 1.0, "vocoderEncoderAudioGain": 3.0,
            "localAudio": local_audio, "voxSampleLevel": 80.0, "dropTimeMs": 180,
            "udpAudio": bool(udp), "udpMetadata": metadata,
            "udpSendPort": udp[1] if udp else 34001, "udpSendAddress": "127.0.0.1",
            "udpReceivePort": udp[0] if udp else 32001, "udpReceiveAddress": "127.0.0.1",
            "udpRTPFrames": not metadata, "udpIgnoreRTPTiming": False, "udpRTPContinuousSeq": False,
            "udpUseULaw": not metadata, "udpUsrp": False, "udpFrameTiming": metadata,
            "rtsPttEnable": bool(serial_port), "rtsPttPort": serial_port or "/dev/ttyUSB0", "rtsPttHoldoffMs": 250,
            "ctsCorEnable": bool(serial_port), "ctsCorPort": serial_port or "/dev/ttyUSB0",
            "ctsCorInvert": False, "ctsCorHoldoffMs": 250,
        }
        cfg = {
            "daemon": False,
            "iAgreeNotToBeStupid": self.ack,
            "log": {"displayLevel": 2, "fileLevel": 2, "useSyslog": False, "filePath": "log",
                    "activityFilePath": "log", "fileRoot": identity},
            "network": {"id": peer_id, "address": fne_host, "port": self.fne_port,
                        "password": secret("peer", peer_id), "encrypted": False,
                        "allowDiagnosticTransfer": True, "sourceId": src_id or peer_id,
                        "overrideSourceIdFromMDC": False, "overrideSourceIdFromUDP": metadata,
                        "resetCallForSourceIdChange": metadata,
                        "destinationId": tg, "slot": slot},
            "system": system,
        }
        return to_yaml(cfg, f"KeyUp generated dvmbridge configuration ({identity})")

    def central_instances(self) -> dict[str, dict]:
        inst = {FNE_INSTANCE: self.fne_instance()}
        for t in self.ic_tgs():
            recv, send = self.ic_ports(t["id"])
            inst[f"ic-tg{t['id']}"] = {"binary": "dvmbridge", "args": ["-f", "-c", "config.yml"], "files": {
                "config.yml": self.bridge_config(peer_id=self.ic_peer(t["id"]), identity=f"IC{t['id']}"[:8], tg=t["id"],
                                                 mode=t.get("bridge_mode") or "P25P1", slot=t.get("dmr_slot") or 1,
                                                 udp=(recv, send), grant_demand=True, metadata=True)}}
        for p in self.patches:
            if not p["enabled"]:
                continue
            a_port, b_port = PATCH_PORT_BASE + p["id"] * 2, PATCH_PORT_BASE + p["id"] * 2 + 1
            for side, tg, mode, pid, recv, send in (("a", p["tg_a"], p["mode_a"], 9200000 + p["id"] * 2, a_port, b_port),
                                                    ("b", p["tg_b"], p["mode_b"], 9200000 + p["id"] * 2 + 1, b_port, a_port)):
                inst[f"patch-{p['id']}-{side}"] = {"binary": "dvmbridge", "args": ["-f", "-c", "config.yml"], "files": {
                    "config.yml": self.bridge_config(peer_id=pid, identity=f"PT{p['id']}{side.upper()}", tg=tg,
                                                     mode=mode, udp=(recv, send), grant_demand=True)}}
        return inst

    # -------------------------------------------------------------- edge (per site)
    def site_instances(self, site_id: int) -> dict[str, dict]:
        site = self.sites[site_id]
        chans = [c for c in self.channels if c["site_id"] == site_id and c["enabled"]]
        rf = [c for c in chans if c["mode"] in MODE_PROTO]
        ccs = [c for c in rf if c["role"] == "control"]
        lookups = {"iden_table.dat": self.iden_table(), "rid_acl.dat": self.rid_acl(),
                   "talkgroup_rules.yml": self.talkgroup_rules()}
        tg_mode = {t["id"]: t.get("bridge_mode") or "P25P1" for t in self.tgs}
        out: dict[str, dict] = {}
        chinfo = {}
        for c in rf:
            iden, chno, warns = channel_number(c["tx_mhz"], c["rx_mhz"], self.idens)
            for w in warns:
                self.warn(f"channel:{c['id']}", w)
            chinfo[c["id"]] = (iden["id"] if iden else 0, chno)
        for c in rf:
            cc = next((x for x in ccs if x["mode"] == c["mode"]), None)
            trunked = cc is not None
            is_cc = trunked and cc["id"] == c["id"]
            proto = MODE_PROTO[c["mode"]]
            ch_id, ch_no = chinfo[c["id"]]
            rpc_port, rest_port = edge_ports(site_id, c["slot"])
            if is_cc:
                voice = [x for x in rf if x["mode"] == c["mode"] and x["id"] != c["id"]]
                voice_list = [{"channelId": chinfo[v["id"]][0], "channelNo": format(chinfo[v["id"]][1], "X"),
                               "rpcAddress": "127.0.0.1", "rpcPort": edge_ports(site_id, v["slot"])[0],
                               "rpcPassword": secret("rpc", site_id)} for v in voice]
                if not voice:
                    self.warn(f"channel:{c['id']}", "control channel has no voice channels at this site")
            else:
                voice_list = [{"channelId": ch_id, "channelNo": format(ch_no, "X"), "rpcAddress": "127.0.0.1",
                               "rpcPort": rpc_port, "rpcPassword": secret("rpc", site_id)}]
            protocols = {
                "dmr": {"enable": proto == "dmr",
                        "control": {"dedicated": is_cc and proto == "dmr", "enable": is_cc and proto == "dmr", "slot": 1},
                        "callHang": 5, "txHang": 8, "verbose": True},
                "p25": {"enable": proto == "p25",
                        "control": {"enable": is_cc and proto == "p25", "dedicated": is_cc and proto == "p25",
                                    "broadcast": True, "interval": 300, "duration": 1},
                        "callHang": 5, "verbose": True},
                "nxdn": {"enable": proto == "nxdn",
                         "control": {"enable": is_cc and proto == "nxdn", "dedicated": is_cc and proto == "nxdn",
                                     "broadcast": True, "interval": 300, "duration": 1},
                         "callHang": 5, "verbose": True},
            }
            identity = re.sub(r"[^A-Za-z0-9]", "", f"{site.get('callsign') or site['name']}")[:8] or f"SITE{site_id}"
            cfg = {
                "daemon": False,
                "iAgreeNotToBeStupid": self.ack,
                "log": {"displayLevel": 2, "fileLevel": 2, "useSyslog": False, "filePath": "log",
                        "activityFilePath": "log", "fileRoot": f"ch{c['slot']}"},
                "network": {
                    "enable": True, "id": c["peer_id"], "address": self.fne_host, "port": self.fne_port,
                    "password": secret("peer", c["peer_id"]), "encrypted": False, "jitter": 360,
                    "slot1": True, "slot2": True, "updateLookups": True, "saveLookups": True,
                    "allowActivityTransfer": True, "allowDiagnosticTransfer": True, "allowStatusTransfer": True,
                    "rpcAddress": "127.0.0.1", "rpcPort": rpc_port, "rpcPassword": secret("rpc", site_id),
                    "restEnable": True, "restAddress": "127.0.0.1", "restPort": rest_port,
                    "restSsl": False, "restPassword": secret("rest", c["peer_id"]),
                },
                "protocols": protocols,
                "system": {
                    "identity": identity, "timeout": 180, "duplex": True, "modeHang": 10,
                    "fixedMode": True, "rfTalkgroupHang": 10,
                    "info": {"latitude": float(site.get("latitude") or 0.0), "longitude": float(site.get("longitude") or 0.0),
                             "height": 1, "power": int(c.get("power_w") or 10),
                             "location": (site.get("location") or site["name"])[:40]},
                    "config": {
                        "authoritative": True, "supervisor": is_cc,
                        "channelId": ch_id, "channelNo": format(ch_no, "X"),
                        "controlCh": {"rpcAddress": "127.0.0.1" if trunked and not is_cc else "",
                                      "rpcPort": edge_ports(site_id, cc["slot"])[0] if trunked and not is_cc else 0,
                                      "rpcPassword": secret("rpc", site_id),
                                      "notifyEnable": trunked and not is_cc,  # only voice channels notify their CC
                                      "presence": 30},  # VC re-announces itself to the CC every 30 s
                        "voiceChNo": voice_list,
                        "colorCode": _int(c.get("color_code"), 1), "nac": _hex(c.get("nac"), "293"),
                        "ran": _int(c.get("ran"), 1), "pSuperGroup": "FFFE", "announcementGroup": "FFFE",
                        "defaultNetIdleTalkgroup": 0, "dmrNetId": _hex(self.s.get("dmr_net_id"), "1"),
                        "netId": _hex(self.s.get("wacn"), "BB800"), "sysId": _hex(self.s.get("system_id"), "1"),
                        "rfssId": format(int(site.get("rfss_id") or 1), "X"), "siteId": format(site_id & 0xFF, "X"),
                    },
                    "cwId": {"enable": bool(site.get("callsign")), "time": 10, "callsign": site.get("callsign") or "NOCALL"},
                    "modem": {
                        "protocol": {"type": c.get("modem_type") or "uart", "mode": c.get("modem_mode") or "air",
                                     "uart": {"port": c.get("modem_port") or "/dev/ttyUSB0",
                                              "speed": int(c.get("modem_speed") or 115200)}},
                        "rxInvert": False, "txInvert": False, "pttInvert": False, "dcBlocker": True,
                        "rxLevel": int(c.get("rx_level") or 50), "txLevel": int(c.get("tx_level") or 50),
                    },
                    "iden_table": {"file": "iden_table.dat", "time": 30},
                    "radio_id": {"file": "rid_acl.dat", "time": 2, "acl": False},
                    "talkgroup_id": {"file": "talkgroup_rules.yml", "time": 2, "acl": False},
                },
            }
            if site_id > 255:
                self.warn(f"site:{site_id}", "P25/DVM site IDs are 8-bit; site number is truncated")
            name = f"ch{c['slot']}"
            out[name] = {"binary": "dvmhost", "args": ["-f", "-c", "config.yml"], "channel_id": c["id"],
                         "peer_id": c["peer_id"], "rest_port": rest_port, "rest_password": secret("rest", c["peer_id"]),
                         "files": {"config.yml": to_yaml(cfg, f"KeyUp generated dvmhost config: {site['name']} / {c['name']}"),
                                   **lookups}}
        for c in chans:
            if c["mode"] == "ANALOG":
                if not c.get("talkgroup_id"):
                    self.warn(f"channel:{c['id']}", "analog channel needs a bound talkgroup")
                    continue
                aud = str(c.get("audio_device") or "")
                args = ["-f", "-c", "config.yml"]
                if re.fullmatch(r"\d+,\d+", aud):
                    i, o = aud.split(",")
                    args += ["-i", i, "-o", o]
                name = f"ana{c['slot']}"
                out[name] = {"binary": "dvmbridge", "args": args, "channel_id": c["id"], "peer_id": c["peer_id"],
                             "files": {"config.yml": self.bridge_config(
                                 peer_id=c["peer_id"], identity=f"AN{site_id}{c['slot']}", tg=c["talkgroup_id"],
                                 mode=tg_mode.get(c["talkgroup_id"], "P25P1"), fne_host=self.fne_host,
                                 local_audio=True, serial_port=c.get("modem_port") or "",
                                 src_id=c.get("default_unit_id") or 0)}}
            elif c["mode"] == "P25P2":
                self.warn(f"channel:{c['id']}", "P25 Phase II is not supported by DVM; channel skipped")
        return out

    def channel_meta(self) -> dict[int, dict]:
        """peer_id -> {site_id, channel_id, label}"""
        out = {}
        for c in self.channels:
            out[c["peer_id"]] = {"site_id": c["site_id"], "channel_id": c["id"],
                                 "label": f"{self.sites.get(c['site_id'], {}).get('name', '?')} / {c['name']}"}
        for pid, alias, meta in self.bridge_peers():
            out[pid] = {"site_id": 0, "channel_id": None, "label": alias}
        return out


# ====================================================================== writing central instances
from lmrcommon.supervisor import write_instances  # noqa: E402  (shared with the edge agent)


# ====================================================================== FNE REST + log monitor
TERM_RE = re.compile(r"U: 0*(\d+) .*?(DMR), TERMINATOR_WITH_LC.*?dstId = (\d+)")
CALL_RE = re.compile(r"(P25|DMR|NXDN|Analog), (Private )?Call (Start|End), peer = (\d+).*?srcId = (\d+), "
                     r"dstId = (\d+)(?:, slot = (\d+))?(?:, duration = (\d+))?, streamId = (\d+)")


class FneClient:
    def __init__(self):
        self.base = f"http://127.0.0.1:{settings.fne_rest_port}"
        self.token = ""
        self.http = httpx.AsyncClient(timeout=4)

    async def _auth(self) -> None:
        h = hashlib.sha256(settings.fne_rest_password.encode()).hexdigest()
        r = await self.http.put(self.base + "/auth", json={"auth": h})
        r.raise_for_status()
        self.token = str(r.json().get("token", ""))

    async def get(self, path: str) -> dict:
        for attempt in range(2):
            if not self.token:
                await self._auth()
            r = await self.http.get(self.base + path, headers={"X-DVM-Auth-Token": self.token})
            r.raise_for_status()
            data = r.json()
            # DVM answers HTTP 200 with {"status": 401, ...} for a bad/expired token
            status = int(data.get("status", 200)) if isinstance(data, dict) else 200
            if status == 401 and attempt == 0:
                self.token = ""
                continue
            if status >= 400 and not (path == "/peer/query" and "no peers" in str(data.get("message", ""))):
                raise RuntimeError(f"FNE {path}: {data.get('message', status)}")
            return data
        return {}


class FneMonitor:
    """Polls the FNE REST API and tails the FNE log for call start/end events."""

    def __init__(self, ctl):
        self.ctl = ctl
        self.client = FneClient()
        self.root = os.path.join(settings.dvm_dir, "instances")
        self.reachable = False
        self.peers: dict[int, dict] = {}
        self.affiliations: list[dict] = []
        self.stats: dict = {}
        self.active: dict[int, dict] = {}   # streamId -> call summary
        self.recent_end: dict[int, float] = {}  # streamId -> end time (FNE may log a late Start after End)
        self.meta: dict[int, dict] = {}
        self.error = ""
        self.poc = None  # PocGateway, to label calls that come from phones
        self._log_path = ""
        self._log_pos = 0
        self._ev_pos = 0
        self.structured = False  # True once the fork's JSON call events are seen

    def refresh_meta(self) -> None:
        with session_scope() as db:
            self.meta = DvmPlan(db).channel_meta()

    # ------------------------------------------------------------------ REST
    async def poll(self) -> None:
        try:
            q = await self.client.get("/peer/query")
            peers = {}
            for p in q.get("peers", []):
                pid = int(p.get("peerId", 0))
                cfg = p.get("config", {}) if isinstance(p.get("config"), dict) else {}
                peers[pid] = {"peer_id": pid, "address": p.get("address"), "connected": p.get("connected", True),
                              "last_ping": p.get("lastPing"), "pings": p.get("pingsReceived"),
                              "identity": cfg.get("identity"), "software": cfg.get("software"),
                              "control_channel": p.get("controlChannel"), **self.meta.get(pid, {})}
            self.peers = peers
            aff = await self.client.get("/report-affiliations")
            self.affiliations = aff.get("affiliations", [])
            self.reachable, self.error = True, ""
        except Exception as e:
            if self.reachable:
                bus.publish("fne", reachable=False, error=str(e))
            self.reachable, self.error, self.peers = False, str(e), {}

    async def reload(self, what: set[str]) -> None:
        paths = {"talkgroup_rules.yml": "/reload-tgs", "rid_acl.dat": "/reload-rids", "peer_list.dat": "/reload-peers"}
        for f, path in paths.items():
            if f in what:
                try:
                    await self.client.get(path)
                except Exception as e:
                    log.debug("FNE %s failed: %s", path, e)

    # ------------------------------------------------------------------ log tail
    def _tail(self) -> list[str]:
        files = sorted(glob.glob(os.path.join(self.root, FNE_INSTANCE, "log", "FNE-*.log")))
        if not files:
            return []
        path = files[-1]
        if path != self._log_path:
            self._log_path = path
            self._log_pos = os.path.getsize(path) if not self.active and self._log_pos == 0 else 0
        with open(path, errors="replace") as f:
            f.seek(self._log_pos)
            data = f.read()
            self._log_pos = f.tell()
        return data.splitlines()

    def _on_call(self, m: re.Match) -> None:
        mode, private, kind, peer, src, dst, slot, duration, stream = m.groups()
        self._call(mode, bool(private), kind, int(peer), int(src), int(dst), float(duration or 0), int(stream))

    def _call(self, mode: str, private: bool, kind: str, peer: int, src: int, dst: int, duration: float,
              stream: int) -> None:
        info = self.meta.get(peer, {"site_id": 0, "label": f"peer {peer}"})
        if kind == "Start":
            if stream in self.active or time.time() - self.recent_end.get(stream, 0) < 10:
                return
            if self.poc is not None and self.poc.is_talking(dst, src):
                info = {**info, "label": f"PoC phone ({info['label']})"}
            summary = {"call_id": stream, "tg": dst, "tg_name": self.ctl.tg_names.get(dst, str(dst)),
                       "src_id": src, "src_alias": self.ctl.unit_alias(src), "src_site": info.get("site_id", 0),
                       "src_label": info["label"], "codec": "UNIT" if private else mode.upper(), "state": "active",
                       "emergency": False, "via_sip": info["label"].startswith("IC-"), "started": time.time(),
                       "dests": [], "frames": 0, "peer_id": peer, "private": bool(private), "mode": mode}
            self.active[stream] = summary
            bus.publish("call_start", call=summary)
        else:
            call = self.active.pop(stream, None)
            if call is None and stream in self.recent_end:
                return
            self.recent_end[stream] = time.time()
            if len(self.recent_end) > 500:
                for k in sorted(self.recent_end, key=self.recent_end.get)[:250]:
                    self.recent_end.pop(k, None)
            dur = float(duration)
            bus.publish("call_end", call_id=stream, tg=dst, reason="end", duration=dur)
            started = call["started"] if call else time.time() - dur
            with session_scope() as db:
                db.add(CallRecord(call_id=stream & 0x7FFFFFFF, talkgroup_id=dst, source_id=src,
                                  source_site_id=info.get("site_id", 0), source_channel_id=info.get("channel_id"),
                                  source_codec=mode.upper(), started_at=dt.datetime.utcfromtimestamp(started),
                                  ended_at=now(), duration_s=dur, via_sip=info["label"].startswith("IC-"),
                                  dest_sites=json.dumps([]), result="granted" if not private else "granted:private"))
                s = db.get(Subscriber, src)
                if s:
                    s.last_site_id, s.last_tg_id, s.last_seen = info.get("site_id"), dst, now()

    def _events(self) -> list[dict] | None:
        """Structured call events from the KeyUp DVM fork (None when running an upstream FNE)."""
        path = os.path.join(self.root, FNE_INSTANCE, "log", "keyup-events.jsonl")
        if not os.path.exists(path):
            return None
        size = os.path.getsize(path)
        if size < self._ev_pos:
            self._ev_pos = 0  # rotated / truncated
        with open(path, errors="replace") as f:
            f.seek(self._ev_pos)
            data = f.read()
        complete = data[:data.rfind("\n") + 1]
        self._ev_pos += len(complete.encode())
        out = []
        for line in complete.splitlines():
            try:
                out.append(json.loads(line))
            except ValueError:
                pass
        return out

    def process_log(self) -> None:
        events = self._events()
        if events is not None:
            self.structured = True
            for e in events:
                try:
                    self._call(e["mode"], bool(e.get("private")), "Start" if e["event"] == "start" else "End",
                               int(e["peer"]), int(e["src"]), int(e["dst"]), e.get("duration_ms", 0) / 1000,
                               int(e["stream"]))
                except Exception:
                    log.exception("bad call event %s", e)
        for line in self._tail():
            m = CALL_RE.search(line)
            if m and self.structured:
                continue  # the fork's event feed already reported it
            if m:
                try:
                    self._on_call(m)
                except Exception:
                    log.exception("call log parse failed: %s", line)
                continue
            m = TERM_RE.search(line)  # DMR terminator seen in peer diagnostics (FNE may not log Call End)
            if m:
                peer, dst = int(m.group(1)), int(m.group(3))
                for sid, c in list(self.active.items()):
                    if c["peer_id"] == peer and c["tg"] == dst and c.get("mode") == "DMR":
                        self._finish(sid, c, time.time() - c["started"])
        t = time.time()
        for sid, c in list(self.active.items()):  # lost end: close after max call time
            if t - c["started"] > 180:
                self._finish(sid, c, t - c["started"])

    def _finish(self, stream: int, call: dict, dur: float) -> None:
        line = (f"{call.get('mode', 'P25')}, {'Private ' if call.get('private') else ''}Call End, peer = {call['peer_id']}, srcId = {call['src_id']}, dstId = {call['tg']}, "
                f"duration = {int(dur)}, streamId = {stream}")
        m = CALL_RE.search(line)
        if m:
            self._on_call(m)

    async def run(self) -> None:
        self.refresh_meta()
        while True:
            try:
                self.process_log()
            except Exception:
                log.exception("FNE log tail failed")
            await self.poll()
            await asyncio.sleep(1.0)

    def status(self) -> dict:
        return {"reachable": self.reachable, "event_feed": "keyup-fork" if self.structured else "log-parse", "error": self.error, "peers": list(self.peers.values()),
                "affiliation_count": sum(len(a.get("affiliations", [])) if isinstance(a, dict) else 1
                                         for a in self.affiliations),
                "active_calls": list(self.active.values()), "dvm_ref": DVM_REF}


# ====================================================================== apply
async def apply(monitor: FneMonitor | None) -> dict:
    """Compile DB -> DVM files for central instances; hot-reload the FNE when lookups change."""
    with session_scope() as db:
        plan = DvmPlan(db)
        instances = plan.central_instances() if plan.ack else {}
    root = os.path.join(settings.dvm_dir, "instances")
    changed = write_instances(root, instances, prune_prefixes=("fne", "sip-", "ic-", "patch-"))
    fne_changed = {c.split("/", 1)[1] for c in changed if c.startswith(f"{FNE_INSTANCE}/")}
    if monitor:
        monitor.refresh_meta()
        if fne_changed - {"config.yml", "run.json"}:
            await monitor.reload(fne_changed)
    if changed:
        log.info("DVM config updated: %s", ", ".join(sorted(changed)))
    return {"changed": sorted(changed), "warnings": plan.warnings, "license_acknowledged": plan.ack}
