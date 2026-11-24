"""Unit tests for the DVM configuration compiler (no DVM binaries needed).

Run:  PYTHONPATH=.:central pytest -q tests
"""
import os
import sys
import tempfile

import pytest
import yaml

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
_tmp = tempfile.mkdtemp()
os.environ.setdefault("LMR_DATA_DIR", _tmp)
os.environ["DATABASE_URL"] = f"sqlite:///{_tmp}/t.db"
sys.path[:0] = [ROOT, os.path.join(ROOT, "central")]

from app.db import Base, engine, session_scope  # noqa: E402
from app.models import (Channel, IdenEntry, Patch, Site, Subscriber, SystemSetting, TalkGroup,  # noqa: E402
                        DEFAULT_SYSTEM_SETTINGS)
from app.services import dvm  # noqa: E402


@pytest.fixture(scope="module")
def plan():
    Base.metadata.create_all(engine)
    with session_scope() as db:
        for k, v in DEFAULT_SYSTEM_SETTINGS.items():
            db.add(SystemSetting(key=k, value=v))
        db.get(SystemSetting, "dvm_license_acknowledged").value = "true"
        db.add(IdenEntry(id=2, name="UHF", base_mhz=450.0, spacing_khz=6.25, offset_mhz=5.0, bandwidth_khz=12.5))
        db.add_all([Site(id=1, name="North", callsign="N0CALL"), Site(id=2, name="South")])
        db.add_all([TalkGroup(id=100, name="Fire", allow_sip=True), TalkGroup(id=200, name="PW", restricted=True, site_ids="2"),
                    TalkGroup(id=1, name="DMR Local", bridge_mode="DMR")])
        db.flush()
        db.add_all([
            Channel(site_id=1, slot=1, name="CC", mode="P25P1", role="control", tx_mhz=451.0, rx_mhz=456.0, peer_id=100101),
            Channel(site_id=1, slot=2, name="VC", mode="P25P1", role="voice", tx_mhz=451.0125, rx_mhz=456.0125, peer_id=100102),
            Channel(site_id=2, slot=1, name="CONV", mode="DMR", role="voice", tx_mhz=452.6, rx_mhz=450.0, peer_id=100201),
            Channel(site_id=2, slot=2, name="ANA", mode="ANALOG", role="voice", talkgroup_id=100, audio_device="1,1",
                    modem_port="/dev/ttyUSB1", peer_id=100202),
            Channel(site_id=2, slot=3, name="OFFPLAN", mode="NXDN", role="voice", tx_mhz=145.5, rx_mhz=145.5, peer_id=100203),
        ])
        pw = TalkGroup  # noqa
        s = Subscriber(id=2002, alias="PW Truck, 3", enabled=True)
        s.talkgroups = [db.get(TalkGroup, 200)]
        db.add_all([s, Subscriber(id=1001, alias="E1", enabled=False)])
        db.add(Patch(name="DMR<->P25", tg_a=1, mode_a="DMR", tg_b=100, mode_b="P25P1"))
    with session_scope() as db:
        yield dvm.DvmPlan(db)


def test_channel_number_math():
    idens = [{"id": 2, "base_mhz": 450.0, "spacing_khz": 6.25, "offset_mhz": 5.0}]
    iden, n, w = dvm.channel_number(451.0125, 456.0125, idens)
    assert iden["id"] == 2 and n == 162 and not w          # (451.0125-450)/0.00625
    _, _, w = dvm.channel_number(451.0125, 451.0125, idens)
    assert w and "offset" in w[0]
    iden, _, w = dvm.channel_number(146.52, 0, idens)
    assert iden is None and w


def test_yaml_emitter_is_dvm_safe():
    out = dvm.to_yaml({"a": "", "b": None, "c": [], "d": [{"x": 1, "y": "two words"}], "e": {"f": True}, "g": "0123"})
    assert "a:" not in out and "b:" not in out      # DVM's parser rejects "" and bare keys
    assert 'y: "two words"' in out and 'g: "0123"' in out
    assert yaml.safe_load(out) == {"c": [], "d": [{"x": 1, "y": "two words"}], "e": {"f": True}, "g": "0123"}


def test_fne_files(plan):
    inst = plan.central_instances()
    assert set(inst) == {"fne", "sip-tg100", "patch-1-a", "patch-1-b"}
    files = inst["fne"]["files"]
    cfg = yaml.safe_load(files["config.yml"])
    assert cfg["iAgreeNotToBeStupid"] is True and cfg["system"]["peer_acl"]["enable"] is True
    assert cfg["system"]["restEnable"] is True and cfg["master"]["allowAnalogTraffic"] is True
    tgs = {g["source"]["tgid"]: g for g in yaml.safe_load(files["talkgroup_rules.yml"])["groupVoice"]}
    assert tgs[200]["config"]["rid_permitted"] == [2002]          # restricted TG
    assert sorted(tgs[200]["config"]["inclusion"]) == [100201, 100202, 100203]  # site 2 peers only
    assert tgs[100]["config"]["inclusion"] == [] and tgs[100]["config"]["rid_permitted"] == []
    rids = files["rid_acl.dat"].splitlines()
    assert "2002,1,PW Truck  3,," in rids and "1001,0,E1,," in rids  # commas stripped, disabled flagged
    peers = [l for l in files["peer_list.dat"].splitlines() if l and not l.startswith("#")]
    assert len(peers) == 5 + 3                                     # 5 channels + SIP bridge + 2 patch bridges
    adj = yaml.safe_load(files["adj_site_map.yml"])
    assert adj["peers"] == [{"peerId": 100101, "active": True, "neighbors": []}]
    assert "2,450000000,6.25,5.00000,12.5," in files["iden_table.dat"]


def test_bridges(plan):
    inst = plan.central_instances()
    sip = yaml.safe_load(inst["sip-tg100"]["files"]["config.yml"])
    assert sip["network"]["destinationId"] == 100 and sip["system"]["txMode"] == 2
    assert sip["system"]["udpAudio"] and sip["system"]["udpUseULaw"]
    assert (sip["system"]["udpReceivePort"], sip["system"]["udpSendPort"]) == plan.sip_ports(100)
    a = yaml.safe_load(inst["patch-1-a"]["files"]["config.yml"])["system"]
    b = yaml.safe_load(inst["patch-1-b"]["files"]["config.yml"])["system"]
    assert a["txMode"] == 1 and b["txMode"] == 2                     # DMR <-> P25
    assert a["udpSendPort"] == b["udpReceivePort"] and b["udpSendPort"] == a["udpReceivePort"]


def test_site_trunked_p25(plan):
    inst = plan.site_instances(1)
    cc = yaml.safe_load(inst["ch1"]["files"]["config.yml"])
    vc = yaml.safe_load(inst["ch2"]["files"]["config.yml"])
    assert cc["protocols"]["p25"]["control"]["dedicated"] is True and cc["system"]["config"]["supervisor"] is True
    assert vc["protocols"]["p25"]["control"]["enable"] is False
    assert cc["system"]["config"]["channelNo"] == "A0"              # 160 hex
    assert [v["channelNo"] for v in cc["system"]["config"]["voiceChNo"]] == ["A2"]
    assert vc["system"]["config"]["controlCh"]["rpcPort"] == cc["network"]["rpcPort"]
    assert vc["system"]["config"]["controlCh"]["notifyEnable"] is True
    assert cc["system"]["config"]["controlCh"]["notifyEnable"] is False   # a CC must not notify itself
    assert cc["system"]["config"]["netId"] == "BB800" and cc["system"]["config"]["sysId"] == "1A1"
    assert cc["network"]["updateLookups"] and cc["network"]["saveLookups"]  # keeps working in site trunking
    assert cc["system"]["cwId"]["enable"] is True


def test_site_analog_and_warnings(plan):
    inst = plan.site_instances(2)
    assert set(inst) == {"ch1", "ch3", "ana2"}
    ana = inst["ana2"]
    assert ana["binary"] == "dvmbridge" and ana["args"][-4:] == ["-i", "1", "-o", "1"]
    sysc = yaml.safe_load(ana["files"]["config.yml"])["system"]
    assert sysc["localAudio"] and sysc["rtsPttEnable"] and sysc["ctsCorPort"] == "/dev/ttyUSB1"
    dmr = yaml.safe_load(inst["ch1"]["files"]["config.yml"])
    assert dmr["protocols"]["dmr"]["enable"] and not dmr["protocols"]["p25"]["enable"]
    warns = " ".join(m for v in plan.warnings.values() for m in v)
    assert "doesn't match band plan" in warns and "not on any band plan" in warns
    ports = {i.get("rest_port") for s in (1, 2) for i in plan.site_instances(s).values() if i.get("rest_port")}
    assert len(ports) == 4  # unique across sites
