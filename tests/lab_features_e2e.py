#!/usr/bin/env python3
"""End-to-end lab for KeyUp's endpoint features: central + one simulated edge site on one machine.

  python3 tests/lab_features_e2e.py

Checks:
  * zones: a phone gets its radio ID's zones, filtered by talkgroup rules
  * PoC phones (native router legs): phone -> radio and phone -> phone audio with the phone's radio
    ID, radio -> phone, busy / not-permitted denials, emergency priority, inhibit logs the phone out
"""
import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path[:0] = [ROOT, os.path.join(ROOT, "tests"), os.path.join(ROOT, "tools")]

import lab_e2e as lab  # noqa: E402
from lab_e2e import wait  # noqa: E402
from poc_client import PocClient, activate  # noqa: E402

PORT = 18180
lab.PORT, lab.BASE = PORT, f"http://127.0.0.1:{PORT}"
BASE = lab.BASE
EDGE = "http://127.0.0.1:18191"


def req(method, path, body=None, base=None, auth=True):
    return lab.req(method, path, body, base or BASE, auth)


def edge_chan(name):
    return next(c for c in req("GET", "/status", base=EDGE, auth=False)["channels"] if c["name"] == name)


def sim_key(channel, seconds, unit):
    req("POST", f"/sim/key?channel={channel}&seconds={seconds}&unit={unit}", {}, base=EDGE, auth=False)


def poc_token(unit):
    code = req("POST", f"/api/subscribers/{unit}/poc/activation", {})["code"]
    return activate(BASE, unit, code, name=f"lab-{unit}")["token"]


async def poc_checks():
    ch = {c["name"]: c["id"] for c in req("GET", "/status", base=EDGE, auth=False)["channels"]}
    a, b = PocClient(BASE, poc_token(1001)), PocClient(BASE, poc_token(2002))
    pa, pb = await a.connect(), await b.connect()
    assert pa["t"] == "auth_ok" and pb["t"] == "auth_ok", (pa, pb)
    # zones: 1001 may use 100 and 200 (restricted, listed); "Ops" zone first by position
    assert [(z["name"], z["talkgroups"]) for z in pa["zones"]] == [("Ops", [100]), ("Admin", [200])], pa["zones"]
    assert [(z["name"], z["talkgroups"]) for z in pb["zones"]] == [("Ops", [100, 300])], pb["zones"]  # 200 restricted
    await a.select(100)
    await b.select(100)

    # phone -> radio site + other phone, with the phone's radio ID
    before = edge_chan("N-FD")["voice_frames_played"]
    g = await a.talk(100, 2.0)
    assert g["t"] == "grant", g
    await asyncio.sleep(1.0)
    played = edge_chan("N-FD")["voice_frames_played"] - before
    assert played >= 80, f"radio site played only {played} frames"
    assert b.rx_frames.get(100, 0) >= 80 and 1001 in b.rx_srcs, (b.rx_frames, b.rx_srcs)
    assert edge_chan("N-PW")["frames_played"] == 0
    wait(lambda: any(c["source_id"] == 1001 and c["talkgroup_id"] == 100 and c["result"] == "granted"
                     for c in req("GET", "/api/calls")), 10, "phone call in history")
    await asyncio.sleep(2.0)   # hang time

    # radio -> phones
    a.rx_frames.clear(); b.rx_srcs.clear()
    sim_key(ch["N-FD"], 1.5, 3003)
    await asyncio.sleep(2.5)
    assert a.rx_frames.get(100, 0) >= 50 and 3003 in b.rx_srcs, (a.rx_frames, b.rx_srcs)
    ev = await a.wait_event("rx_start")
    assert ev["src"] == 3003 and ev["tg"] == 100, ev
    await asyncio.sleep(2.0)

    # busy while a radio talks; emergency takes the floor
    sim_key(ch["N-FD"], 4, 3003)
    await asyncio.sleep(0.6)
    d = await b.talk(100, 0.5)
    assert d["t"] == "deny" and d["reason"] == "busy", d
    g = await a.talk(100, 1.0, emergency=True)
    assert g["t"] == "grant" and g.get("emergency"), g
    wait(lambda: any(c["result"] == "preempted" for c in req("GET", "/api/calls")), 10, "radio call preempted")
    await asyncio.sleep(3.5)

    # not permitted: 2002 may not use restricted TG 200
    d = await b.talk(200, 0.5)
    assert d["t"] == "deny" and d["reason"] == "not_permitted", d

    # inhibit: disabling the radio ID logs the phone out
    sub = req("GET", "/api/subscribers")
    s2002 = next(s for s in sub if s["id"] == 2002)
    req("PUT", "/api/subscribers/2002", {**s2002, "enabled": False})
    ev = await b.wait_event_any(("inhibited", "auth_fail"))
    assert ev["reason"] == "inhibited", ev
    await a.close()
    await b.close()
    return "phone->radio+phone as RID 1001, radio->phones, busy, emergency preempts a radio, restricted TG denied, inhibit logs out"


def main():
    tmp = tempfile.mkdtemp(prefix="keyup-features-")
    env = {**os.environ, "PYTHONPATH": f"{ROOT}:{ROOT}/central", "LMR_DATA_DIR": tmp,
           "DATABASE_URL": f"sqlite:///{tmp}/lmr.db", "WG_ENABLED": "false", "ALLOW_UNTUNNELED_EDGES": "true",
           "HTTP_PORT": str(PORT), "VOICE_PORT": "15100", "HTTP_HOST": "127.0.0.1", "LOG_LEVEL": "WARNING",
           "PUBLIC_URL": BASE}
    procs = []
    central = subprocess.Popen([sys.executable, "-m", "app.main"], cwd=f"{ROOT}/central", env=env,
                               stdout=open(f"{tmp}/central.log", "a"), stderr=subprocess.STDOUT)
    procs.append(central)
    try:
        wait(lambda: req("GET", "/healthz", auth=False)["ok"], msg="central")
        lab.TOKEN = req("POST", "/api/auth/login", {"username": "admin", "password": "admin"}, auth=False)["token"]
        req("POST", "/api/sites", {"id": 1, "name": "North"})
        req("POST", "/api/talkgroups", {"id": 100, "name": "Fire", "hang_time_ms": 1500})
        req("POST", "/api/talkgroups", {"id": 200, "name": "PW", "restricted": True})
        req("POST", "/api/talkgroups", {"id": 300, "name": "Tac"})
        for slot, name, tg in [(1, "N-FD", 100), (2, "N-PW", 200)]:
            req("POST", "/api/channels", {"site_id": 1, "slot": slot, "name": name, "mode": "ANALOG", "talkgroup_id": tg})
        req("POST", "/api/zones", {"name": "Admin", "position": 2, "talkgroup_ids": [200]})
        req("POST", "/api/zones", {"name": "Ops", "position": 1, "talkgroup_ids": [100, 300]})
        req("POST", "/api/subscribers", {"id": 1001, "alias": "E1", "talkgroup_ids": [100, 200]})
        req("POST", "/api/subscribers", {"id": 2002, "alias": "PW3"})
        req("POST", "/api/subscribers", {"id": 3003, "alias": "Portable"})
        tok = req("POST", "/api/enrollment/tokens", {"site_id": 1, "auto_approve": True})["token"]
        os.makedirs(f"{tmp}/edge1")
        with open(f"{tmp}/edge1/config.yaml", "w") as f:
            f.write(f"server_url: {BASE}\ncontrol_url: {BASE}\nenrollment_token: {tok}\nstate_dir: {tmp}/edge1/state\n"
                    "radio_backend: sim\nwireguard: {enabled: false}\nlocal_api: {port: 18191}\nlog_level: WARNING\n")
        procs.append(subprocess.Popen([sys.executable, "-m", "lmr_edge", "-c", f"{tmp}/edge1/config.yaml"],
                                      cwd=f"{ROOT}/edge", env={**env, "PYTHONPATH": f"{ROOT}:{ROOT}/edge"},
                                      stdout=open(f"{tmp}/edge1.log", "w"), stderr=subprocess.STDOUT))
        wait(lambda: req("GET", "/healthz", auth=False)["sites"] == 1, msg="edge connected")
        wait(lambda: len(req("GET", "/status", base=EDGE, auth=False)["channels"]) == 2, msg="config push")
        try:
            req("POST", "/api/poc/activate", {"unit": 1001, "code": "WRONG123"}, auth=False)
            raise AssertionError("bad PoC code accepted")
        except urllib.error.HTTPError as e:
            assert e.code == 403
        print(f"ok  PoC: {asyncio.run(poc_checks())}")
        print("\nALL FEATURE LAB TESTS PASSED")
    finally:
        for p in procs:
            p.terminate()
        time.sleep(0.5)
        if os.getenv("KEEP") is None:
            shutil.rmtree(tmp, ignore_errors=True)
        else:
            print("logs in", tmp)


if __name__ == "__main__":
    main()
