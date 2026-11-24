#!/usr/bin/env python3
"""End-to-end lab test of the NATIVE backend: central + two simulated edge sites on one machine (no radios, no WireGuard).

  python3 tests/lab_e2e.py

Checks: enrollment, config push, wide-area call routing with hang-time talkback,
talkgroup permission denial, site trunking on central loss (local repeat + data
notifications), wide-area restoration and upload of site-trunking call records.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
PORT = 18080
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
        except Exception:
            pass
        time.sleep(0.3)
    raise AssertionError(f"timeout waiting for {msg}")


def edge_status(n):
    return req("GET", "/status", base=f"http://127.0.0.1:{18090 + n}", auth=False)


def chan(n, name):
    return next(c for c in edge_status(n)["channels"] if c["name"] == name)


def main():
    global TOKEN
    tmp = tempfile.mkdtemp(prefix="lmr-lab-")
    env = {**os.environ, "PYTHONPATH": f"{ROOT}:{ROOT}/central", "LMR_DATA_DIR": tmp,
           "DATABASE_URL": f"sqlite:///{tmp}/lmr.db", "WG_ENABLED": "false", "ALLOW_UNTUNNELED_EDGES": "true",
           "HTTP_PORT": str(PORT), "VOICE_PORT": "15000", "HTTP_HOST": "127.0.0.1", "LOG_LEVEL": "WARNING",
           "KEYUP_DEFAULT_BACKEND": "native"}
    procs = []

    def start_central():
        p = subprocess.Popen([sys.executable, "-m", "app.main"], cwd=f"{ROOT}/central", env=env,
                             stdout=open(f"{tmp}/central.log", "a"), stderr=subprocess.STDOUT)
        wait(lambda: req("GET", "/healthz", auth=False)["ok"], msg="central")
        return p

    central = start_central()
    try:
        TOKEN = req("POST", "/api/auth/login", {"username": "admin", "password": "admin"}, auth=False)["token"]
        req("POST", "/api/sites", {"id": 1, "name": "North"})
        req("POST", "/api/sites", {"id": 2, "name": "South"})
        req("POST", "/api/talkgroups", {"id": 100, "name": "Fire", "hang_time_ms": 1500})
        req("POST", "/api/talkgroups", {"id": 200, "name": "PW"})
        for site, slot, name, tg in [(1, 1, "N-FD", 100), (1, 2, "N-FD2", 100), (2, 1, "S-FD", 100), (2, 2, "S-PW", 200)]:
            req("POST", "/api/channels", {"site_id": site, "slot": slot, "name": name, "mode": "ANALOG", "talkgroup_id": tg})
        req("POST", "/api/subscribers", {"id": 1001, "alias": "E1", "home_site_id": 1, "talkgroup_ids": [100]})
        req("POST", "/api/subscribers", {"id": 2002, "alias": "PW3", "home_site_id": 2, "talkgroup_ids": [200]})
        for n in (1, 2):
            tok = req("POST", "/api/enrollment/tokens", {"site_id": n, "auto_approve": True})["token"]
            os.makedirs(f"{tmp}/edge{n}")
            with open(f"{tmp}/edge{n}/config.yaml", "w") as f:
                f.write(f"server_url: {BASE}\ncontrol_url: {BASE}\nenrollment_token: {tok}\nstate_dir: {tmp}/edge{n}/state\n"
                        f"radio_backend: sim\nwireguard: {{enabled: false}}\nlocal_api: {{port: {18090 + n}}}\n"
                        f"site_trunking: {{timeout_s: 3}}\nnotifications: {{data: {{targets: [talkgroups, subscribers]}}}}\n"
                        "log_level: WARNING\n")
            procs.append(subprocess.Popen([sys.executable, "-m", "lmr_edge", "-c", f"{tmp}/edge{n}/config.yaml"],
                                          cwd=f"{ROOT}/edge", env={**env, "PYTHONPATH": f"{ROOT}:{ROOT}/edge"},
                                          stdout=open(f"{tmp}/edge{n}.log", "w"), stderr=subprocess.STDOUT))
        wait(lambda: req("GET", "/healthz", auth=False)["sites"] == 2, msg="edges connected")
        wait(lambda: len(edge_status(1)["channels"]) == 2 and len(edge_status(2)["channels"]) == 2, msg="config push")
        print("ok  enrollment + config push")

        # wide-area call with talkback inside hang time
        req("POST", "/sim/key?channel=1&seconds=1.5&unit=1001", {}, base="http://127.0.0.1:18091", auth=False)
        time.sleep(2.1)
        s_fd = chan(2, "S-FD")["id"]
        req("POST", f"/sim/key?channel={s_fd}&seconds=1&unit=0", {}, base="http://127.0.0.1:18092", auth=False)
        wait(lambda: not req("GET", "/api/dashboard")["call_processing"]["active_calls"], msg="call end")
        calls = [c for c in req("GET", "/api/calls") if c["result"] == "granted"]
        assert len(calls) == 1 and set(calls[0]["dest_sites"]) == {"site:1"}, calls  # last talker was site 2
        assert chan(2, "S-FD")["voice_frames_played"] >= 70, chan(2, "S-FD")
        assert chan(1, "N-FD2")["voice_frames_played"] >= 120, chan(1, "N-FD2")
        assert chan(1, "N-FD")["voice_frames_played"] >= 45, chan(1, "N-FD")  # heard the reply
        assert chan(2, "S-PW")["frames_played"] == 0
        print("ok  wide-area routing, hang-time talkback, TG isolation")

        # permission denial: unit 2002 is only allowed on TG 200
        req("POST", f"/sim/key?channel={s_fd}&seconds=0.5&unit=2002", {}, base="http://127.0.0.1:18092", auth=False)
        wait(lambda: any(c["result"] == "denied:not_permitted" for c in req("GET", "/api/calls")), msg="deny")
        print("ok  talkgroup permission enforced")

        # central down -> site trunking
        central.terminate()
        central.wait()
        wait(lambda: edge_status(1)["mode"] == "site_trunking" and edge_status(2)["mode"] == "site_trunking", msg="site trunking")
        msgs = {(n["target_type"], n["target_id"], n["text"]) for n in edge_status(1)["notifications"]}
        assert ("group", 100, "SITE TRUNKING") in msgs and ("unit", 1001, "SITE TRUNKING") in msgs, msgs
        before = chan(1, "N-FD2")["voice_frames_played"]
        req("POST", "/sim/key?channel=1&seconds=1&unit=1001", {}, base="http://127.0.0.1:18091", auth=False)
        wait(lambda: chan(1, "N-FD2")["voice_frames_played"] >= before + 45, msg="local repeat")
        wait(lambda: edge_status(1)["pending_logs"] == 1, msg="local call log")
        print("ok  site trunking: local repeat + data notifications to talkgroups/subscribers")

        # central back -> wide area, logs uploaded
        central = start_central()
        wait(lambda: edge_status(1)["mode"] == "wide_area" and edge_status(2)["mode"] == "wide_area", 30, "restore")
        wait(lambda: any(c["site_trunking"] for c in req("GET", "/api/calls")), msg="log upload")
        assert any(n["text"] == "WIDE AREA RESTORED" for n in edge_status(1)["notifications"])
        print("ok  wide area restored, site-trunking call records uploaded")
        print("\nALL LAB TESTS PASSED")
    finally:
        for p in procs + [central]:
            p.terminate()
        time.sleep(0.5)
        if os.getenv("KEEP") is None:
            shutil.rmtree(tmp, ignore_errors=True)
        else:
            print("logs in", tmp)


if __name__ == "__main__":
    main()
