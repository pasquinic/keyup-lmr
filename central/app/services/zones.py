"""What a radio ID may see: its permitted talkgroups, arranged in zones.

The same rules decide what a PoC phone, a dispatch console and a programmed radio get:
  * a disabled talkgroup is never shown;
  * a radio with a talkgroup list may use only those talkgroups (an empty list means any);
  * a restricted talkgroup needs to be on the radio's list;
  * PoC phones additionally skip talkgroups with "Allow PoC" off.

Zones: the radio's assigned zones (all zones when none are assigned), each filtered by those rules,
in zone order and member order. With no zones defined at all, one zone holds every permitted talkgroup.
"""
from __future__ import annotations

from ..models import Subscriber, TalkGroup, Zone


def permitted(tg: TalkGroup, allowed: set[int], poc: bool = False) -> bool:
    if not tg.enabled:
        return False
    if allowed and tg.id not in allowed:
        return False
    if tg.restricted and tg.id not in allowed:
        return False
    if poc and not tg.allow_poc:
        return False
    return True


def tg_info(t: TalkGroup) -> dict:
    return {"id": t.id, "name": t.name, "priority": t.priority, "emergency": t.emergency_enabled}


def unit_view(db, sub: Subscriber, zones: list[Zone] | None = None, poc: bool = False) -> dict:
    """{"talkgroups": [tg...], "zones": [{"id","name","color","talkgroups":[ids]}]}.

    `zones` overrides the subscriber's own zone assignment (dispatch consoles have their own)."""
    allowed = {t.id for t in sub.talkgroups}
    tgs = {t.id: t for t in db.query(TalkGroup).order_by(TalkGroup.id).all() if permitted(t, allowed, poc)}
    chosen = zones if zones else list(sub.zones)
    if not chosen:
        chosen = db.query(Zone).order_by(Zone.position, Zone.id).all()
    else:
        chosen = sorted(chosen, key=lambda z: (z.position, z.id))
    out_zones = []
    for z in chosen:
        ids = [m.talkgroup_id for m in z.members if m.talkgroup_id in tgs]
        if ids:
            out_zones.append({"id": z.id, "name": z.name, "color": z.color, "talkgroups": ids})
    if not out_zones and not db.query(Zone).count():
        out_zones = [{"id": 0, "name": "All talkgroups", "color": "", "talkgroups": list(tgs)}]
    seen: list[int] = []
    for z in out_zones:
        seen += [t for t in z["talkgroups"] if t not in seen]
    return {"talkgroups": [tg_info(tgs[t]) for t in seen], "zones": out_zones}
