"""System rules for the native router: band plan, talkgroup links, affiliations, config checks.

These are pure helpers (no I/O besides the passed-in DB session) so they are easy to test.
"""
from __future__ import annotations

import time
from dataclasses import dataclass


# ====================================================================== band plan
@dataclass
class ChannelSlot:
    identity: int | None
    number: int
    problems: list[str]


def locate_channel(tx_mhz: float, rx_mhz: float, plan: list[dict]) -> ChannelSlot:
    """Which band-plan identity and channel number a transmit frequency sits on."""
    if not tx_mhz:
        return ChannelSlot(None, 0, [])
    tx_hz = int(round(tx_mhz * 1_000_000))
    for entry in sorted(plan, key=lambda e: e["id"]):
        base_hz = int(round(entry["base_mhz"] * 1_000_000))
        step_hz = entry["spacing_khz"] * 1000.0
        offset = tx_hz - base_hz
        if offset < 0 or step_hz <= 0:
            continue
        steps = offset / step_hz
        nearest = round(steps)
        if abs(steps - nearest) > 1e-6 or nearest > 0xFFF:
            continue
        problems = []
        want_rx = tx_hz + int(round(entry["offset_mhz"] * 1_000_000))
        if rx_mhz and abs(int(round(rx_mhz * 1_000_000)) - want_rx) > 1:
            problems.append(f"receive {rx_mhz:.5f} MHz differs from band plan {entry['id']} "
                            f"input offset (expected {want_rx / 1e6:.5f} MHz)")
        return ChannelSlot(entry["id"], int(nearest), problems)
    return ChannelSlot(None, 0, [f"transmit {tx_mhz:.5f} MHz is not on any band plan entry"])


# ====================================================================== talkgroup links (patches)
def link_groups(patches: list[dict]) -> dict[int, frozenset[int]]:
    """Talkgroup -> every talkgroup it is (transitively) patched with, itself included."""
    parent: dict[int, int] = {}

    def root(x: int) -> int:
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for p in patches:
        if p.get("enabled", True) and p["tg_a"] != p["tg_b"]:
            parent[root(p["tg_a"])] = root(p["tg_b"])
    members: dict[int, set[int]] = {}
    for tg in list(parent):
        members.setdefault(root(tg), set()).add(tg)
    return {tg: frozenset(members[root(tg)]) for tg in parent}


def site_list(value: str | None) -> set[int]:
    return {int(x) for x in (value or "").replace(" ", "").split(",") if x.isdigit()}


# ====================================================================== affiliations
class Affiliations:
    """Where each radio was last heard: unit -> (site, talkgroup, time). Used for affiliated-only talkgroups."""

    def __init__(self):
        self.units: dict[int, tuple[int, int, float]] = {}

    def heard(self, unit: int, site: int, tg: int) -> None:
        if unit and site:
            self.units[unit] = (site, tg, time.time())

    def sites_for(self, tg: int, timeout_s: float) -> set[int]:
        cutoff = time.time() - timeout_s
        return {site for site, t, ts in self.units.values() if t == tg and ts >= cutoff}

    def expire(self, timeout_s: float) -> None:
        cutoff = time.time() - timeout_s
        for u in [u for u, (_, _, ts) in self.units.items() if ts < cutoff]:
            del self.units[u]

    def table(self, timeout_s: float) -> list[dict]:
        self.expire(timeout_s)
        return [{"unit": u, "site": s, "tg": t, "age_s": round(time.time() - ts)}
                for u, (s, t, ts) in sorted(self.units.items())]


# ====================================================================== configuration checks
def config_checks(sites: list[dict], channels: list[dict], tgs: list[dict], patches: list[dict],
                  plan: list[dict], devices: list[dict], backend: str = "native") -> list[dict]:
    """Human-readable problems an operator should fix: [{scope, message}].
    With the DVM backend, digital channels are modems (no GPIO, no fixed talkgroup) so those checks are skipped."""
    out: list[dict] = []
    site_ids = {s["id"] for s in sites}
    tg_ids = {t["id"] for t in tgs}

    def add(scope: str, msg: str) -> None:
        out.append({"scope": scope, "message": msg})

    for c in channels:
        scope = f"channel {c['name']} (site {c['site_id']})"
        if not c.get("enabled"):
            continue
        digital_modem = backend == "dvm" and c.get("mode") != "ANALOG"
        if c.get("role", "voice") == "voice" and not c.get("talkgroup_id") and not digital_modem:
            add(scope, "voice channel has no talkgroup bound; it will never be keyed")
        if plan:
            for p in locate_channel(c.get("tx_mhz") or 0, c.get("rx_mhz") or 0, plan).problems:
                add(scope, p)
        if c.get("signaling") == "serial" and not c.get("serial_port"):
            add(scope, "serial signalling selected but no serial port set")
    taken: dict[tuple, str] = {}
    for c in channels:
        if backend == "dvm":
            break
        if c.get("enabled") and c.get("signaling", "gpio") == "gpio":
            for pin in (c.get("ptt_gpio"), c.get("cor_gpio")):
                k = (c["site_id"], pin)
                if k in taken and taken[k] != c["name"]:
                    add(f"site {c['site_id']}", f"GPIO {pin} used by both {taken[k]} and {c['name']}")
                taken[k] = c["name"]
    for t in tgs:
        unknown = site_list(t.get("site_ids")) - site_ids
        if unknown:
            add(f"talkgroup {t['id']}", f"site limit names unknown site(s) {sorted(unknown)}")
        if t.get("parrot") and t.get("allow_sip"):
            add(f"talkgroup {t['id']}", "parrot talkgroups echo SIP callers too")
    for p in patches:
        for tg in (p["tg_a"], p["tg_b"]):
            if tg not in tg_ids:
                add(f"patch {p['name']}", f"talkgroup {tg} does not exist")
        if p["tg_a"] == p["tg_b"]:
            add(f"patch {p['name']}", "patches a talkgroup to itself")
    active = {d["site_id"] for d in devices if d.get("status") == "active"}
    for s in sites:
        if s.get("enabled") and s["id"] not in active:
            add(f"site {s['id']}", "no approved edge device enrolled")
        if s["id"] > 255:
            add(f"site {s['id']}", "site numbers above 255 cannot be broadcast by most trunking protocols")
    return out
