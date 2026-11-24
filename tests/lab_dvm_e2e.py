#!/usr/bin/env python3
"""End-to-end lab test of the DVM backend with real DVMProject binaries (null modems, no RF).

  DVM_BIN_DIR=/path/to/dvmhost/build python3 tests/lab_dvm_e2e.py

Runs KeyUp central, the DVM supervisor (dvmfne + dvmbridges) and two edge agents
(each supervising dvmhost instances) on one machine, then checks:
  * KeyUp-generated configs are accepted: every dvmhost and dvmbridge logs into the FNE
  * a P25 trunked site (control + voice channel) registers its voice channel with the CC
  * SIP-side audio -> dvmbridge -> P25 call on the FNE, recorded in KeyUp call history
  * cross-mode patch: that P25 call is re-vocoded onto a DMR talkgroup
  * FNE outage -> edges report SITE TRUNKING; FNE back -> wide area
"""
import json
import os
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import time
import urllib.request

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)
from lmrcommon import audio  # noqa: E402

BIN = os.environ.get("DVM_BIN_DIR", "")
PORT = 18180
BASE = f"http://127.0.0.1:{PORT}"
TOKEN = ""


def req(method, path, body=None, base=BASE, auth=True):
    r = urllib.request.Request(base + path, method=method, data=json.dumps(body).encode() if body is not None else None)
    r.add_header("content-type", "application/json")
    if auth and TOKEN:
        r.add_header("authorization", f"Bearer {TOKEN}")
    with urllib.request.urlopen(r, timeout=10) as resp:
        return json.loads(resp.read() or b"{}")


def wait(pred, timeout=30, msg=""):
    t = time.time()
    while time.time() - t < timeout:
        try:
            if pred():
                return
        except Exception:
            pass
        time.sleep(0.5)
    raise AssertionError(f"timeout waiting for {msg}")


def main():
    global TOKEN
    if not BIN or not os.path.exists(os.path.join(BIN, "dvmfne")):
        print("SKIP: set DVM_BIN_DIR to a directory containing dvmfne/dvmhost/dvmbridge")
        return
    tmp = tempfile.mkdtemp(prefix="keyup-dvm-lab-")
    env = {**os.environ, "PYTHONPATH": f"{ROOT}:{ROOT}/central", "LMR_DATA_DIR": tmp,
           "DATABASE_URL": f"sqlite:///{tmp}/k.db", "WG_ENABLED": "false", "ALLOW_UNTUNNELED_EDGES": "true",
           "HTTP_PORT": str(PORT), "HTTP_HOST": "127.0.0.1", "VOICE_PORT": "15100", "LOG_LEVEL": "WARNING",
           "FNE_REST_PORT": "19990", "FNE_REST_PASSWORD": "labfne", "SECRET_KEY": "lab", "KEYUP_DEFAULT_BACKEND": "dvm"}
    procs = {}

    def start_sup():
        procs["sup"] = subprocess.Popen([sys.executable, f"{ROOT}/lmrcommon/supervisor.py", f"{tmp}/dvm/instances",
                                         "--bin-dir", BIN], stdout=open(f"{tmp}/dvm.log", "a"), stderr=subprocess.STDOUT)

    procs["central"] = subprocess.Popen([sys.executable, "-m", "app.main"], cwd=f"{ROOT}/central", env=env,
                                        stdout=open(f"{tmp}/central.log", "w"), stderr=subprocess.STDOUT)
    try:
        wait(lambda: req("GET", "/healthz", auth=False)["ok"], msg="central")
        TOKEN = req("POST", "/api/auth/login", {"username": "admin", "password": "admin"}, auth=False)["token"]
        req("PUT", "/api/settings", {"dvm_license_acknowledged": "true", "fne_address": "127.0.0.1", "fne_port": "62131"})
        req("POST", "/api/idens", {"id": 2, "name": "UHF", "base_mhz": 450.0, "spacing_khz": 6.25, "offset_mhz": 5.0})
        req("POST", "/api/sites", {"id": 1, "name": "North", "callsign": "N0CALL"})
        req("POST", "/api/sites", {"id": 2, "name": "South"})
        req("POST", "/api/talkgroups", {"id": 100, "name": "Fire", "allow_sip": True, "bridge_mode": "P25P1"})
        req("POST", "/api/talkgroups", {"id": 1, "name": "DMR Local", "bridge_mode": "DMR"})
        for site, slot, name, mode, role, tx in [(1, 1, "N-CC", "P25P1", "control", 451.0), (1, 2, "N-VC", "P25P1", "voice", 451.0125),
                                                 (2, 1, "S-DMR", "DMR", "voice", 452.6)]:
            req("POST", "/api/channels", {"site_id": site, "slot": slot, "name": name, "mode": mode, "role": role,
                                          "tx_mhz": tx, "rx_mhz": tx + 5, "modem_type": "null"})
        req("POST", "/api/patches", {"name": "DMR-P25", "tg_a": 1, "mode_a": "DMR", "tg_b": 100, "mode_b": "P25P1"})
        assert not req("GET", "/api/dvm/status")["warnings"], req("GET", "/api/dvm/status")["warnings"]
        start_sup()
        for n in (1, 2):
            tok = req("POST", "/api/enrollment/tokens", {"site_id": n, "auto_approve": True})["token"]
            os.makedirs(f"{tmp}/edge{n}")
            with open(f"{tmp}/edge{n}/config.yaml", "w") as f:
                f.write(f"server_url: {BASE}\ncontrol_url: {BASE}\nenrollment_token: {tok}\nstate_dir: {tmp}/edge{n}/state\n"
                        f"radio_backend: sim\nwireguard: {{enabled: false}}\nlocal_api: {{port: {18190 + n}}}\n"
                        f"site_trunking: {{timeout_s: 3}}\ndvm: {{bin_dir: {BIN}}}\nlog_level: WARNING\n")
            procs[f"edge{n}"] = subprocess.Popen([sys.executable, "-m", "lmr_edge", "-c", f"{tmp}/edge{n}/config.yaml"],
                                                 cwd=f"{ROOT}/edge", env={**env, "PYTHONPATH": f"{ROOT}:{ROOT}/edge"},
                                                 stdout=open(f"{tmp}/edge{n}.log", "w"), stderr=subprocess.STDOUT)

        def peers():
            return {p["peer_id"]: p for p in (req("GET", "/api/dvm/status")["fne"] or {}).get("peers", [])}
        wait(lambda: len(peers()) == 6, 90, "3 dvmhosts + 3 dvmbridges logged into the FNE")
        wait(lambda: peers()[100102]["control_channel"] == 100101, 60, "P25 voice channel registered with its CC")
        print("ok  FNE accepted all generated configs: 3 dvmhost + 3 dvmbridge peers; P25 VC attached to CC")

        recv, _ = 32000, 34000  # first SIP talkgroup
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        for i in range(150):
            s.sendto(struct.pack("!BBHII", 0x80, 0, i, i * 160, 1) + audio.pcm_to_ulaw(audio.tone(900, 20, 0.4, i * 160)),
                     ("127.0.0.1", recv))
            time.sleep(0.02)
        wait(lambda: any(c["talkgroup_id"] == 100 and c["via_sip"] for c in req("GET", "/api/calls")), 15, "SIP P25 call record")
        log = open(f"{tmp}/dvm.log").read()
        assert "DMR, Call Start, peer = 9200000" in log or "DMR, Call Start, peer = 92000" in log, "patch did not transcode"
        print("ok  SIP audio -> dvmbridge -> P25 TG 100 call recorded; patch re-vocoded it onto DMR TG 1")

        procs.pop("sup").terminate()
        wait(lambda: all(req("GET", "/status", base=f"http://127.0.0.1:{18190 + n}", auth=False)["mode"] == "site_trunking"
                         for n in (1, 2)), 120, "site trunking")
        wait(lambda: {s["status"] for s in req("GET", "/api/dashboard")["sites"]} == {"site_trunking"}, 15, "central view")
        print("ok  FNE down -> both edges in SITE TRUNKING (dvmhost clears network-active on air)")

        start_sup()
        wait(lambda: all(req("GET", "/status", base=f"http://127.0.0.1:{18190 + n}", auth=False)["mode"] == "wide_area"
                         for n in (1, 2)), 120, "wide area")
        print("ok  FNE back -> wide area restored")
        print("\nALL DVM LAB TESTS PASSED")
    finally:
        for pr in procs.values():
            pr.terminate()
        time.sleep(1)
        if os.getenv("KEEP") is None:
            shutil.rmtree(tmp, ignore_errors=True)
        else:
            print("logs in", tmp)


if __name__ == "__main__":
    main()
