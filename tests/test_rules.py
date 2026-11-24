"""Unit tests for band plan, talkgroup links, affiliations, config checks and CW ID.

Run:  PYTHONPATH=.:central pytest -q tests
"""
import os
import sys
import time

sys.path[:0] = [os.path.join(os.path.dirname(__file__), ".."), os.path.join(os.path.dirname(__file__), "..", "central")]

from app.services import rules  # noqa: E402
from lmrcommon import morse  # noqa: E402

PLAN = [{"id": 2, "base_mhz": 450.0, "spacing_khz": 6.25, "offset_mhz": 5.0}]


def test_channel_location():
    s = rules.locate_channel(451.0125, 456.0125, PLAN)
    assert (s.identity, s.number, s.problems) == (2, 162, [])
    assert "differs" in rules.locate_channel(451.0125, 451.0125, PLAN).problems[0]
    off = rules.locate_channel(146.52, 0, PLAN)
    assert off.identity is None and "not on any band plan" in off.problems[0]
    assert rules.locate_channel(451.003, 0, PLAN).identity is None  # between channel steps


def test_link_groups_are_transitive():
    g = rules.link_groups([{"tg_a": 1, "tg_b": 100}, {"tg_a": 100, "tg_b": 200}, {"tg_a": 5, "tg_b": 6, "enabled": False}])
    assert g[1] == g[200] == frozenset({1, 100, 200})
    assert 5 not in g


def test_affiliations_expire():
    a = rules.Affiliations()
    a.heard(1001, 2, 100)
    a.heard(1002, 3, 200)
    assert a.sites_for(100, 60) == {2}
    a.units[1001] = (2, 100, time.time() - 120)
    assert a.sites_for(100, 60) == set()
    assert [r["unit"] for r in a.table(60)] == [1002]


def test_config_checks():
    checks = rules.config_checks(
        sites=[{"id": 1, "enabled": True}, {"id": 2, "enabled": True}],
        channels=[{"name": "A", "site_id": 1, "enabled": True, "role": "voice", "talkgroup_id": None, "tx_mhz": 146.52,
                   "rx_mhz": 146.52, "ptt_gpio": 17, "cor_gpio": 27, "signaling": "gpio"},
                  {"name": "B", "site_id": 1, "enabled": True, "role": "voice", "talkgroup_id": 100, "tx_mhz": 0,
                   "rx_mhz": 0, "ptt_gpio": 17, "cor_gpio": 22, "signaling": "gpio"},
                  {"name": "C", "site_id": 2, "enabled": True, "role": "voice", "talkgroup_id": 100, "tx_mhz": 0,
                   "rx_mhz": 0, "signaling": "serial", "serial_port": ""}],
        tgs=[{"id": 100, "site_ids": "1,9"}],
        patches=[{"name": "p", "tg_a": 100, "tg_b": 300}],
        plan=PLAN, devices=[{"site_id": 1, "status": "active"}])
    text = " | ".join(f"{c['scope']}: {c['message']}" for c in checks)
    for expected in ("no talkgroup bound", "not on any band plan", "GPIO 17 used by both", "no serial port",
                     "unknown site(s) [9]", "talkgroup 300 does not exist", "site 2: no approved edge"):
        assert expected in text, (expected, text)


def test_morse():
    assert morse.timing("ET") == [(True, 1), (False, 3), (True, 3)]
    assert morse.timing("E E") == [(True, 1), (False, 7), (True, 1)]
    pcm = morse.to_pcm("K", wpm=20)  # dah dit dah = 3+1+1+1+3 = 9 units of 60 ms
    assert len(pcm) == 2 * 8000 * 9 * 60 // 1000


def test_serial_signalling_with_fake_port(monkeypatch=None):
    import asyncio
    import types
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "edge"))
    from lmr_edge.radio.serial_sig import SerialSignalChannel

    class FakePort:
        def __init__(self, *a, **kw):
            self.rts, self.dtr, self.cts = False, False, False

        def close(self):
            pass

    sys.modules["serial"] = types.SimpleNamespace(Serial=FakePort)

    async def run():
        events = []
        loop = asyncio.get_running_loop()
        ch = SerialSignalChannel({"id": 7, "serial_port": "/dev/fake", "cor_invert": False}, loop,
                                 lambda cid, on, unit: events.append(on), lambda cid, pcm: None)
        ch._start_signaling()
        ch._port.cts = True
        await asyncio.sleep(0.15)
        ch._port.cts = False
        await asyncio.sleep(0.15)
        ch.set_ptt(True)
        assert ch._port.rts is True and ch.ptt
        ch.set_ptt(False)
        assert ch._port.rts is False
        ch._stop.set()
        return events

    try:
        assert asyncio.run(run()) == [True, False]
    finally:
        sys.modules.pop("serial", None)
