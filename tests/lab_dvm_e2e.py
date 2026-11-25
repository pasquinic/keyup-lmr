#!/usr/bin/env python3
"""End-to-end lab test of the DVM backend with real DVMProject binaries (null modems, no RF).

  DVM_BIN_DIR=/path/to/dvmhost/build python3 tests/lab_dvm_e2e.py

Runs KeyUp central, the DVM supervisor (dvmfne + dvmbridges) and two edge agents
(each supervising dvmhost instances) on one machine, then checks:
  * KeyUp-generated configs are accepted: every dvmhost and dvmbridge logs into the FNE
  * a P25 trunked site (control + voice channel) registers its voice channel with the CC
  * SIP-side audio -> dvmbridge -> P25 call on the FNE, recorded in KeyUp call history
  * cross-mode patch: that P25 call is re-vocoded onto a DMR talkgroup
  * PoC phones (protocol client): activation, talkgroup rules, a phone transmits as its own
    radio ID, phone-to-phone and radio-to-phone audio, busy / not-permitted denials, inhibit
  * dispatch console (the KeyUp console's own network code, headless): activation, codeplug and
    WireGuard file, FNE login as its own peer, P25 transmit as its radio ID, receive radio traffic,
    restricted talkgroups left out, codeplug re-sync, revoke
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
import asyncio
import select
import urllib.error
import urllib.request

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)
from lmrcommon import audio  # noqa: E402
sys.path.insert(0, os.path.join(ROOT, "tools"))
from poc_client import PocClient, activate  # noqa: E402

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


async def poc_checks(tokens: dict, tmp: str) -> str:
    a, b = PocClient(BASE, tokens[1001]), PocClient(BASE, tokens[2002])
    pa, pb = await a.connect(), await b.connect()
    assert pa["t"] == "auth_ok" and [t["id"] for t in pa["talkgroups"]] == [100], pa
    await a.select(100)
    await b.select(100)
    # phone A talks: the FNE must see A's own radio ID; phone B hears it locally
    g = await a.talk(100, 2.0)
    assert g["t"] == "grant", g
    await asyncio.sleep(1.5)
    log = open(f"{tmp}/dvm.log").read()
    assert "P25, Call Start, peer = 9100000" in log and "srcId = 1001, dstId = 100" in log, "PoC srcId not on FNE"
    assert b.rx_frames.get(100, 0) > 60 and 1001 in b.rx_srcs, (b.rx_frames, b.rx_srcs)
    assert "DMR, Call Start, peer = 92000" in log, "patch did not re-vocode the PoC call onto DMR"
    calls = req("GET", "/api/calls")
    assert any(c["source_id"] == 1001 and c["talkgroup_id"] == 100 for c in calls), calls[:3]
    fne = req("GET", "/api/dvm/status")["fne"] or {}
    assert fne.get("event_feed") == "keyup-fork", f"FNE fork call-event feed not active: {fne.get('event_feed')}"
    # radio -> phone: a P25 call from the patch bridge reaches phone B, and A can't key over it (busy)
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    b.rx_frames.clear()
    b.events = [e for e in b.events if e.get("t") != "rx_start"]
    await asyncio.sleep(0.8)
    for i in range(100):
        s.sendto(struct.pack("!BBHII", 0x80, 0, i, i * 160, 9) + audio.pcm_to_ulaw(audio.tone(1200, 20, 0.4, i * 160)),
                 ("127.0.0.1", 36003))  # patch-1-b UDP input -> P25 TG 100 on the FNE
        if i == 60:
            await a.send({"t": "ptt", "on": True, "tg": 100})
        await asyncio.sleep(0.02)
    deny = await a.wait_event_any(("deny", "grant"))
    assert deny == {"t": "deny", "tg": 100, "reason": "busy"}, deny
    await asyncio.sleep(1.0)
    assert b.rx_frames.get(100, 0) > 40 and 9200003 in b.rx_srcs, (b.rx_frames, b.rx_srcs)
    rx = await b.wait_event("rx_start")
    assert rx["src"] == 9200003, rx
    # not permitted
    await b.send({"t": "ptt", "on": True, "tg": 1})
    assert (await b.wait_event("deny"))["reason"] == "not_permitted"
    # inhibit unit 1001 from the console: its phone is cut off like a radio
    sub = [x for x in req("GET", "/api/subscribers") if x["id"] == 1001][0]
    req("PUT", "/api/subscribers/1001", {**sub, "enabled": False})
    inh = await a.wait_event("inhibited")
    assert inh["reason"] == "inhibited"
    a2 = PocClient(BASE, tokens[1001])
    assert (await a2.connect())["reason"] == "inhibited"
    # the mobile app's own TypeScript client (Node runtime, synthetic mic) against the live server
    mob = os.path.join(ROOT, "mobile")
    if shutil.which("npx") and os.path.isdir(os.path.join(mob, "node_modules")):
        b.rx_frames.clear()
        out = subprocess.run(["npx", "tsx", "scripts/live-check.ts", BASE, tokens[2002], "2"], cwd=mob,
                             capture_output=True, text=True, timeout=60)
        res = json.loads(out.stdout.strip().splitlines()[-1])
        assert res["link"] == "online" and res["granted"] and "talkPermit" in res["tones"], res
        await asyncio.sleep(1.0)
        log = open(f"{tmp}/dvm.log").read()
        assert "srcId = 2002, dstId = 100" in log, "app client's transmission not seen on the FNE"
        extra = "; mobile app TS client keyed TG 100 as RID 2002"
    else:
        extra = "; (mobile TS client check skipped: run npm install in mobile/)"
    await b.close()
    return (f"phone transmitted as RID 1001 (FNE + call history), phone-to-phone audio {sum(b.rx_frames.values())}+ "
            f"frames, radio-to-phone with talker {rx['src']}, busy/not-permitted denials, inhibit enforced{extra}")


def console_checks(tmp: str, peers) -> str:
    """Run the dispatch console's real network code (console/tests/PeerCheck) against the live system."""
    if not shutil.which("dotnet"):
        return "SKIPPED (install the .NET 8 SDK to run the dispatch console check)"
    out = f"{tmp}/peercheck"
    b = subprocess.run(["dotnet", "build", "-c", "Release", "-o", out, f"{ROOT}/console/tests/PeerCheck"],
                       capture_output=True, text=True, timeout=600)
    assert b.returncode == 0, b.stdout[-3000:]
    req("POST", "/api/subscribers", {"id": 3001, "alias": "Dispatch", "talkgroup_ids": []})
    req("POST", "/api/talkgroups", {"id": 500, "name": "Secure", "restricted": True, "bridge_mode": "P25P1"})
    con = req("POST", "/api/consoles", {"name": "Dispatch 1", "subscriber_id": 3001})
    pid = con["fne_peer_id"]
    code = req("POST", f"/api/consoles/{con['id']}/activation", {})["code"]
    try:
        req("POST", "/api/console/activate", {"code": f"{con['id']}-AAAA-AAAA"}, auth=False)
        raise AssertionError("bad console code accepted")
    except urllib.error.HTTPError as e:
        assert e.code == 403, e
    p = subprocess.Popen(["dotnet", f"{out}/PeerCheck.dll", BASE, code], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                         stderr=open(f"{tmp}/peercheck.log", "w"), text=True, bufsize=1)

    def line(timeout=30):
        if not select.select([p.stdout], [], [], timeout)[0]:
            raise AssertionError(f"console harness silent ({tmp}/peercheck.log)")
        msg = json.loads(p.stdout.readline())
        assert msg["event"] != "error" or msg.get("refused") is not None, msg
        return msg

    def cmd(c, timeout=30):
        p.stdin.write(c + "\n")
        p.stdin.flush()
        return line(timeout)
    try:
        act = line()
        assert act["event"] == "activated" and sorted(act["talkgroups"]) == ["1", "100"], act   # restricted TG 500 left out
        assert act["alias_ok"] and act["wireguard_ok"] and act["tunnel_ip"] and act["fne"] == "127.0.0.1:62131", act
        assert line()["event"] == "connected"
        wait(lambda: pid in peers(), 30, "console peer on the FNE")
        wait(lambda: req("GET", "/api/consoles")[0]["online"], 15, "console shown online in the web UI")
        row = req("GET", "/api/consoles")[0]
        assert row["activated"] and row["talkgroups"] == [1, 100] and row["wg_ip"] == act["tunnel_ip"], row
        # console PTT on TG 100: reaches the FNE as the console's radio ID, recorded in call history
        tx = cmd("tx 100 3", 30)
        assert tx["event"] == "tx_done" and tx["ldus"] > 10, tx
        time.sleep(1.5)
        log = open(f"{tmp}/dvm.log").read()
        assert f"P25, Call Start, peer = {pid}" in log and "srcId = 3001, dstId = 100" in log, "console call not on the FNE"
        wait(lambda: any(c["source_id"] == 3001 and c["talkgroup_id"] == 100 for c in req("GET", "/api/calls")), 10,
             "console call in history")
        # radio traffic on TG 100 (patch bridge) is heard on the console
        cmd("rx")
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        for i in range(100):
            s.sendto(struct.pack("!BBHII", 0x80, 0, i, i * 160, 9) + audio.pcm_to_ulaw(audio.tone(900, 20, 0.4, i * 160)),
                     ("127.0.0.1", 36003))
            time.sleep(0.02)
        time.sleep(1.5)
        rx = cmd("rx")
        assert rx["frames"].get("100", 0) >= 8 and 9200003 in rx["sources"].get("100", []), rx
        # talkgroup change in the web UI reaches the console on sync
        req("PUT", f"/api/consoles/{con['id']}", {"name": "Dispatch 1", "subscriber_id": 3001, "talkgroup_ids": [100]})
        sy = cmd("sync")
        assert sy["event"] == "sync" and sy["changed"] and sy["talkgroups"] == ["100"], sy
        assert cmd("sync")["changed"] is False
        # revoke: the console's token stops working
        req("POST", f"/api/consoles/{con['id']}/revoke", {})
        rv = cmd("sync")
        assert rv["event"] == "error" and rv["refused"], rv
        p.stdin.write("quit\n")
    finally:
        p.kill()
    return (f"console peer {pid} logged into the FNE, keyed TG 100 as RID 3001 (FNE + call history), heard "
            f"{rx['frames']['100']} LDUs from radio 9200003, restricted TG left out, re-sync and revoke work")


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
        wait(lambda: peers()[100102]["control_channel"] == 100101, 120, "P25 voice channel registered with its CC")
        print("ok  FNE accepted all generated configs: 3 dvmhost + 3 dvmbridge peers; P25 VC attached to CC")

        # ---------------------------------------------------------------- PoC phones
        req("POST", "/api/subscribers", {"id": 1001, "alias": "Engine 1", "talkgroup_ids": [100]})
        req("POST", "/api/subscribers", {"id": 2002, "alias": "Chief 2", "talkgroup_ids": [100]})
        tokens = {}
        for unit in (1001, 2002):
            act = req("POST", f"/api/subscribers/{unit}/poc/activation", {})
            assert act["qr_svg"].startswith("<svg") and act["uri"].startswith("keyup://activate")
            tokens[unit] = activate(BASE, unit, act["code"])["token"]
        try:
            activate(BASE, 1001, act["code"])
            raise AssertionError("activation code reused")
        except Exception as e:
            assert "403" in str(e), e
        wait(lambda: len(peers()) == 6 and 9100000 in peers(), 60, "interconnect bridge for TG 100 up")
        ps = asyncio.run(poc_checks(tokens, tmp))
        print(f"ok  PoC: {ps}")
        print(f"ok  Dispatch console: {console_checks(tmp, peers)}")

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
