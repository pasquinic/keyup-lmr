#!/usr/bin/env python3
"""End-to-end lab test of the routing rules on the native router (no radios, no WireGuard).

  python3 tests/lab_rules_e2e.py

Two simulated sites. Checks talkgroup patches, site limits, parrot, affiliated-only
routing, restricted talkgroups, CW ID, band plan channel numbers and the Network API.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
PORT = 18280
BASE = f"http://127.0.0.1:{PORT}"
TOKEN = ""


def req(method, path, body=None, base=BASE, auth=True):
    r = urllib.request.Request(base + path, method=method, data=json.dumps(body).encode() if body is not None else None)
    r.add_header("content-type", "application/json")
    if auth and TOKEN:
        r.add_header("authorization", f"Bearer {TOKEN}")
    with urllib.request.urlopen(r, timeout=10) as resp:
        return json.loads(resp.read() or b"{}")


def wait(pred, timeout=20, msg=""):
    t = time.time()
    while time.time() - t < timeout:
        try:
            if pred():
                return
        except (urllib.error.URLError, KeyError, StopIteration, ConnectionError):
            pass
        time.sleep(0.3)
    raise AssertionError(f"timeout waiting for {msg}")


def edge(n, path="/status", method="GET"):
    return req(method, path, {} if method == "POST" else None, base=f"http://127.0.0.1:{18290 + n}", auth=False)


def heard(n, name):
    return next(c for c in edge(n)["channels"] if c["name"] == name)["voice_frames_played"]


def key(n, name, seconds=1.0, unit=0):
    cid = next(c for c in edge(n)["channels"] if c["name"] == name)["id"]
    edge(n, f"/sim/key?channel={cid}&seconds={seconds}&unit={unit}", "POST")


def idle():
    wait(lambda: not req("GET", "/api/dashboard")["call_processing"]["active_calls"], 15, "calls to end")
    time.sleep(0.6)


def snapshot():
    return {(n, c["name"]): c["voice_frames_played"] for n in (1, 2) for c in edge(n)["channels"]}


def delta(before):
    after = snapshot()
    return {k: after[k] - before.get(k, 0) for k in after if after[k] - before.get(k, 0) > 0}


def main():
    global TOKEN
    tmp = tempfile.mkdtemp(prefix="keyup-rules-")
    env = {**os.environ, "PYTHONPATH": f"{ROOT}:{ROOT}/central", "LMR_DATA_DIR": tmp,
           "DATABASE_URL": f"sqlite:///{tmp}/k.db", "WG_ENABLED": "false", "ALLOW_UNTUNNELED_EDGES": "true",
           "HTTP_PORT": str(PORT), "VOICE_PORT": "15200", "HTTP_HOST": "127.0.0.1", "LOG_LEVEL": "WARNING"}
    procs = [subprocess.Popen([sys.executable, "-m", "app.main"], cwd=f"{ROOT}/central", env=env,
                              stdout=open(f"{tmp}/central.log", "w"), stderr=subprocess.STDOUT)]
    try:
        wait(lambda: req("GET", "/healthz", auth=False)["ok"], msg="central")
        TOKEN = req("POST", "/api/auth/login", {"username": "admin", "password": "admin"}, auth=False)["token"]
        req("PUT", "/api/settings", {"cw_id_interval_s": "5", "parrot_delay_ms": "500"})
        req("POST", "/api/bandplan", {"id": 2, "name": "UHF", "base_mhz": 450.0, "spacing_khz": 6.25, "offset_mhz": 5.0})
        for sid, name in ((1, "North"), (2, "South")):
            req("POST", "/api/sites", {"id": sid, "name": name})
        tgs = [(100, "Fire", {}), (200, "PW", {}), (300, "Ops", {"site_ids": "1"}), (400, "Parrot", {"parrot": True}),
               (500, "Aff", {"affiliated_only": True}), (600, "Secure", {"restricted": True})]
        for tg, name, extra in tgs:
            req("POST", "/api/talkgroups", {"id": tg, "name": name, "hang_time_ms": 300, **extra})
        req("POST", "/api/patches", {"name": "Fire-PW", "tg_a": 100, "tg_b": 200})
        chans = [(1, "N-FD", 100), (1, "N-PW", 200), (1, "N-OPS", 300), (1, "N-PAR", 400), (1, "N-AFF", 500),
                 (1, "N-SEC", 600), (2, "S-FD", 100), (2, "S-PW", 200), (2, "S-OPS", 300), (2, "S-AFF", 500)]
        for i, (sid, name, tg) in enumerate(chans):
            req("POST", "/api/channels", {"site_id": sid, "slot": i + 1, "name": name, "mode": "ANALOG",
                                          "talkgroup_id": tg, "tx_mhz": 451.0 + i * 0.0125, "rx_mhz": 456.0 + i * 0.0125,
                                          "ptt_gpio": 2 + i, "cor_gpio": 20 + i})
        req("POST", "/api/subscribers", {"id": 1001, "alias": "Open"})
        req("POST", "/api/subscribers", {"id": 1006, "alias": "Cleared", "talkgroup_ids": [600]})
        for n in (1, 2):
            tok = req("POST", "/api/enrollment/tokens", {"site_id": n, "auto_approve": True})["token"]
            os.makedirs(f"{tmp}/edge{n}")
            with open(f"{tmp}/edge{n}/config.yaml", "w") as f:
                f.write(f"server_url: {BASE}\ncontrol_url: {BASE}\nenrollment_token: {tok}\nstate_dir: {tmp}/edge{n}/state\n"
                        f"radio_backend: sim\nwireguard: {{enabled: false}}\nlocal_api: {{port: {18290 + n}}}\nlog_level: WARNING\n")
            procs.append(subprocess.Popen([sys.executable, "-m", "lmr_edge", "-c", f"{tmp}/edge{n}/config.yaml"],
                                          cwd=f"{ROOT}/edge", env={**env, "PYTHONPATH": f"{ROOT}:{ROOT}/edge"},
                                          stdout=open(f"{tmp}/edge{n}.log", "w"), stderr=subprocess.STDOUT))
        wait(lambda: len(edge(1)["channels"]) == 6 and len(edge(2)["channels"]) == 4, 30, "edges configured")
        time.sleep(1)

        fp = {r["name"]: r for r in req("GET", "/api/frequencies")}
        assert (fp["N-PW"]["identity"], fp["N-PW"]["channel_no"]) == (2, 162) and not fp["N-PW"]["warnings"], fp["N-PW"]
        print("ok  band plan: channel numbers computed, offsets checked")

        b = snapshot()
        key(1, "N-FD", 1.0, 1001)
        idle()
        d = delta(b)
        assert all(d.get(k, 0) >= 40 for k in [(1, "N-PW"), (2, "S-FD"), (2, "S-PW")]), d
        calls = req("GET", "/api/calls")
        assert calls[0]["talkgroup_id"] == 100 and calls[0]["result"] == "granted"
        print("ok  patch: a call on TG 100 was heard on TG 200 channels at both sites")

        pid = req("GET", "/api/patches")[0]["id"]
        req("PUT", f"/api/patches/{pid}", {"name": "Fire-PW-Ops", "talkgroups": [100, 200, 300]})
        time.sleep(1)
        b = snapshot()
        key(2, "S-PW", 1.0, 1001)
        idle()
        d = delta(b)
        assert all(d.get(k, 0) >= 40 for k in [(1, "N-FD"), (1, "N-PW"), (1, "N-OPS"), (2, "S-FD")]), d
        assert (2, "S-OPS") not in d, d          # TG 300 is limited to site 1, even when patched
        try:
            req("PUT", f"/api/patches/{pid}", {"name": "bad", "talkgroups": [100]})
            raise AssertionError("one-talkgroup patch accepted")
        except urllib.error.HTTPError as e:
            assert e.code == 422
        req("PUT", f"/api/patches/{pid}", {"name": "Fire-PW", "talkgroups": [100, 200]})
        time.sleep(1)
        print("ok  multi-talkgroup patch: a call on TG 200 was heard on TG 100 and 300 (site limit kept), "
              "one-talkgroup patch refused")

        key(2, "S-OPS", 0.5, 1001)
        wait(lambda: any(c["result"] == "denied:site_not_permitted" for c in req("GET", "/api/calls")), 10, "site deny")
        time.sleep(1.5)  # let S-OPS finish its own deny tone
        idle()
        b = snapshot()
        key(1, "N-OPS", 1.0, 1001)
        idle()
        assert (2, "S-OPS") not in delta(b), delta(b)
        print("ok  site limit: TG 300 refused at site 2 and not routed there")

        b = snapshot()
        key(1, "N-PAR", 1.0, 1001)
        wait(lambda: delta(b).get((1, "N-PAR"), 0) >= 40, 10, "parrot echo")
        idle()
        assert set(delta(b)) == {(1, "N-PAR")}, delta(b)
        print("ok  parrot: the transmission was echoed back to the talker's channel only")

        b = snapshot()
        key(1, "N-AFF", 1.0, 1001)
        idle()
        assert (2, "S-AFF") not in delta(b), delta(b)
        key(2, "S-AFF", 0.5, 1007)  # a radio at site 2 joins TG 500
        idle()
        b = snapshot()
        key(1, "N-AFF", 1.0, 1001)
        idle()
        assert delta(b).get((2, "S-AFF"), 0) >= 40, delta(b)
        print("ok  affiliated-only: TG 500 reached site 2 only after a radio there was heard on it")

        key(1, "N-SEC", 0.5, 1001)
        wait(lambda: any(c["result"] == "denied:not_permitted" and c["talkgroup_id"] == 600
                         for c in req("GET", "/api/calls")), 10, "restricted deny")
        time.sleep(1.5)
        idle()
        key(1, "N-SEC", 0.5, 1006)
        wait(lambda: any(c["result"] == "granted" and c["source_id"] == 1006 for c in req("GET", "/api/calls")), 10, "grant")
        print("ok  restricted: TG 600 refused unit 1001, granted unit 1006")

        net = req("GET", "/api/network")
        assert len(net["sites"]) == 2 and all(s["channels"] for s in net["sites"])
        aff = {(a["unit"], a["site"], a["tg"]) for a in net["affiliations"]}
        assert (1007, 2, 500) in aff and (1006, 1, 600) in aff, aff
        assert net["links"]["100"] == [100, 200]
        print("ok  network view: both edges with live channel state, affiliations, patch links")

        site1 = [s for s in req("GET", "/api/sites") if s["id"] == 1][0]
        req("PUT", "/api/sites/1", {**site1, "callsign": "N0CALL"})
        wait(lambda: req("GET", "/api/network/preview?site_id=1")["site"]["callsign"] == "N0CALL", 5, "preview")
        time.sleep(1)
        idle()
        key(1, "N-FD", 0.5, 1001)
        wait(lambda: edge(1)["cw_ids_sent"] >= 1, 30, "CW ID")
        assert edge(2)["cw_ids_sent"] == 0
        sent = edge(1)["cw_ids_sent"]
        time.sleep(8)  # longer than the 5 s interval: the ID itself must not trigger another ID
        assert edge(1)["cw_ids_sent"] == sent, (sent, edge(1)["cw_ids_sent"])
        print(f"ok  CW ID: site 1 identified {edge(1)['cw_ids_sent']} transmitter(s); site 2 (no callsign) none")
        print("\nALL RULES LAB TESTS PASSED")
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
