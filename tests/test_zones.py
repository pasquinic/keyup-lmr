"""Zones: what a radio ID (and so a PoC phone or dispatch console bound to it) gets.

Run:  PYTHONPATH=.:central pytest -q tests
"""
import os
import sys
import tempfile

_tmp = tempfile.mkdtemp()
os.environ["DATABASE_URL"] = f"sqlite:///{_tmp}/z.db"
os.environ.setdefault("LMR_DATA_DIR", _tmp)
sys.path[:0] = [os.path.join(os.path.dirname(__file__), ".."), os.path.join(os.path.dirname(__file__), "..", "central")]

from app.db import Base, engine, session_scope  # noqa: E402
from app.models import Subscriber, TalkGroup, Zone, ZoneMember  # noqa: E402
from app.services.zones import unit_view  # noqa: E402


def setup_module():
    Base.metadata.create_all(engine)
    with session_scope() as db:
        db.add_all([TalkGroup(id=100, name="Fire"), TalkGroup(id=200, name="PW", restricted=True),
                    TalkGroup(id=300, name="Tac", allow_poc=False), TalkGroup(id=400, name="Off", enabled=False),
                    TalkGroup(id=500, name="Ops")])
        db.add_all([Subscriber(id=1, alias="open"), Subscriber(id=2, alias="pw")])
        db.flush()
        db.get(Subscriber, 2).talkgroups = [db.get(TalkGroup, 200), db.get(TalkGroup, 500)]


def test_no_zones_defined_gives_one_zone_of_permitted_tgs():
    with session_scope() as db:
        v = unit_view(db, db.get(Subscriber, 1))
        assert v["zones"] == [{"id": 0, "name": "All talkgroups", "color": "", "talkgroups": [100, 300, 500]}]
        assert [t["id"] for t in unit_view(db, db.get(Subscriber, 1), poc=True)["talkgroups"]] == [100, 500]
        assert [t["id"] for t in unit_view(db, db.get(Subscriber, 2))["talkgroups"]] == [200, 500]


def test_zones_order_filtering_and_assignment():
    with session_scope() as db:
        z1 = Zone(name="Ops", position=2, members=[ZoneMember(talkgroup_id=t, position=i) for i, t in enumerate([500, 100, 400])])
        z2 = Zone(name="Admin", position=1, members=[ZoneMember(talkgroup_id=t, position=i) for i, t in enumerate([200, 300])])
        db.add_all([z1, z2])
        db.flush()
        open_unit, pw_unit = db.get(Subscriber, 1), db.get(Subscriber, 2)
        v = unit_view(db, open_unit)   # no assignment: every zone, by position; restricted/disabled dropped
        assert [(z["name"], z["talkgroups"]) for z in v["zones"]] == [("Admin", [300]), ("Ops", [500, 100])]
        assert [t["id"] for t in v["talkgroups"]] == [300, 500, 100]
        assert [(z["name"], z["talkgroups"]) for z in unit_view(db, pw_unit)["zones"]] == [("Admin", [200]), ("Ops", [500])]
        pw_unit.zones = [z1]          # assigned zones only
        assert [z["name"] for z in unit_view(db, pw_unit)["zones"]] == ["Ops"]
        assert [z["name"] for z in unit_view(db, pw_unit, zones=[z2])["zones"]] == ["Admin"]   # console override
        assert unit_view(db, open_unit, poc=True)["zones"][0]["name"] == "Ops"   # Admin has only non-PoC TG 300
