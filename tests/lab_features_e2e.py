#!/usr/bin/env python3
"""End-to-end lab for KeyUp's endpoint features: central + one simulated edge site on one machine.

  python3 tests/lab_features_e2e.py

Checks:
  * zones: a phone gets its radio ID's zones, filtered by talkgroup rules
  * PoC phones (native router legs): phone -> radio and phone -> phone audio with the phone's radio
    ID, radio -> phone, busy / not-permitted denials, emergency priority, inhibit logs the phone out
  * recording: per-call audio with talkers, WAV playback, CSV log export, shipping over HTTPS,
    SIPREC (RFC 7866 metadata) and plain SIP to a simulated recorder with digest auth, retry on failure
  * dispatch console: activation (token + WireGuard tunnel), zones, monitoring several talkgroups,
    radio -> console audio, console talks over a radio (call priority), inhibit, patch, revoke
  * the Windows console's own C# core (console/tests/HeadlessCheck, needs the .NET 8 SDK): activation,
    encrypted settings, WireGuard configuration blob, G.711, monitoring, mixer output, PTT, inhibit
"""
import asyncio
import http.server
import select
import io
import json
import threading
import wave
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path[:0] = [ROOT, os.path.join(ROOT, "tests"), os.path.join(ROOT, "tools")]

import lab_e2e as lab  # noqa: E402
from lab_e2e import wait  # noqa: E402
from poc_client import PocClient, activate  # noqa: E402
from console_client import ConsoleClient, activate as console_activate  # noqa: E402

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
    assert [(z["name"], z["talkgroups"]) for z in pb["zones"]] == [("Ops", [100, 300, 400])], pb["zones"]  # 200 restricted
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


def raw(path):
    r = urllib.request.Request(BASE + path, headers={"authorization": f"Bearer {lab.TOKEN}"})
    with urllib.request.urlopen(r, timeout=10) as resp:
        return resp.read()


class Sink(http.server.BaseHTTPRequestHandler):
    posts = []

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("content-length", 0)))
        Sink.posts.append({"auth": self.headers.get("authorization"), "ctype": self.headers.get("content-type"), "body": body})
        self.send_response(201)
        self.end_headers()

    def log_message(self, *a):
        pass


def recording_checks(tmp):
    calls = req("GET", "/api/calls?recorded=true&limit=50")
    call = max((c for c in calls if c["talkgroup_id"] == 100 and any(t["src"] == 1001 for t in c["talkers"])),
               key=lambda c: c["recording_bytes"])
    wav = wave.open(io.BytesIO(raw(f"/api/calls/{call['id']}/audio")))
    secs = wav.getnframes() / wav.getframerate()
    assert wav.getframerate() == 8000 and wav.getsampwidth() == 2 and secs >= 1.8, (secs, call)
    assert call["talkers"][0]["alias"] == "E1" and call["recording"].endswith(".ul"), call
    log = raw("/api/calls/export.csv?tg=100").decode()
    assert "record,started_utc" in log and "E1 (1001)" in log, log[:300]

    def ship(mode, target, **extra):
        req("PUT", "/api/settings", {"recording_ship_mode": mode, "recording_ship_target": target, **extra})
        req("POST", f"/api/calls/{call['id']}/ship", {})
        wait(lambda: next(c for c in req("GET", "/api/calls?recorded=true&limit=50") if c["id"] == call["id"])["ship_status"]
             not in ("queued",), 40, f"{mode} shipping")
        return next(c for c in req("GET", "/api/calls?recorded=true&limit=50") if c["id"] == call["id"])["ship_status"]

    # HTTPS (plain HTTP in the lab) upload
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 18199), Sink)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    st = ship("https", "http://127.0.0.1:18199/upload", recording_ship_token="t0k")
    assert st == "sent" and Sink.posts, (st, Sink.posts[:1])
    post = Sink.posts[-1]
    assert post["auth"] == "Bearer t0k" and b'"talkgroup": 100' in post["body"] and b"RIFF" in post["body"], post["ctype"]
    srv.shutdown()

    # SIPREC and plain SIP to a recorder that requires digest auth
    out = f"{tmp}/siprec.jsonl"
    sim = subprocess.Popen([sys.executable, f"{ROOT}/tools/sip_recorder_sim.py", "--port", "15160", "--user", "rec",
                            "--password", "secret", "--out", out])
    try:
        time.sleep(0.5)
        st = ship("siprec", "sip:srs@127.0.0.1:15160", recording_ship_username="rec", recording_ship_password="secret")
        assert st == "sent", st
        got = [json.loads(line) for line in open(out)]
        rec = got[-1]
        frames = int(secs * 50)
        assert rec["siprec"] and rec["require"] == "siprec" and "+sip.src" in rec["contact"], rec
        assert 'k:talkgroup id="100" name="Fire"' in rec["metadata"] and "<name>E1</name>" in rec["metadata"], rec["metadata"]
        assert rec["rtp_packets"] >= frames * 0.95, (rec["rtp_packets"], frames)
        st = ship("sip", "sip:recorder@127.0.0.1:15160")
        rec = [json.loads(line) for line in open(out)][-1]
        assert st == "sent" and not rec["siprec"] and rec["x_headers"]["x-keyup-talkgroup"] == "100", rec
        assert '"TG 100 Fire"' in rec["from"], rec["from"]
        # a recorder that does not answer: the call is retried, not lost
        st = ship("sip", "sip:recorder@127.0.0.1:15999")
        assert st.startswith("retry:1"), st
    finally:
        sim.terminate()
        req("PUT", "/api/settings", {"recording_ship_mode": "off"})
    return (f"call recorded ({secs:.1f}s WAV, talkers logged, CSV export), shipped over HTTPS, SIPREC "
            f"({rec['rtp_packets']} RTP packets + RFC 7865 metadata) and SIP with digest auth; failures retried")


async def console_checks():
    ch = {c["name"]: c["id"] for c in req("GET", "/status", base=EDGE, auth=False)["channels"]}
    req("POST", "/api/subscribers", {"id": 4004, "alias": "Dispatch"})
    con = req("POST", "/api/consoles", {"name": "Dispatch 1", "subscriber_id": 4004})
    code = req("POST", f"/api/consoles/{con['id']}/activation", {})["code"]
    try:
        req("POST", "/api/console/activate", {"code": f"{con['id']}-AAAA-AAAA"}, auth=False)
        raise AssertionError("bad console code accepted")
    except urllib.error.HTTPError as e:
        assert e.code == 403
    act = console_activate(BASE, code)
    wg = act["wireguard"]
    assert wg["private_key"] and wg["address"].endswith("/32") and wg["allowed_ips"].endswith("/32"), wg
    assert act["urls"]["tunnel"].startswith("ws://") and act["urls"]["public"].endswith("/console/ws"), act["urls"]
    c = ConsoleClient(act["urls"]["public"], act["token"])
    prof = await c.connect()
    assert prof["t"] == "auth_ok" and prof["rights"] == {"inhibit": True, "priority": True, "patch": True}, prof
    assert [z["name"] for z in prof["zones"]] == ["Ops"], prof["zones"]   # 4004 may not use restricted TG 200
    await c.monitor([100, 300])
    # radio -> console, tagged with the talkgroup and talker
    sim_key(ch["N-FD"], 1.5, 3003)
    st = await c.wait_event("call_start", 5, tg=100)
    assert st["src"] == 3003 and st["alias"] == "Portable", st
    await c.wait_event("call_end", 6, tg=100)
    assert c.rx_frames.get(100, 0) >= 60 and 3003 in c.rx_srcs[100], c.rx_frames
    # call priority: the console talks over a radio
    await asyncio.sleep(0.5)
    before = edge_chan("N-FD")["voice_frames_played"]
    sim_key(ch["N-FD"], 4, 3003)
    await asyncio.sleep(0.7)
    g = await c.talk(100, 1.5)
    assert g["t"] == "grant", g
    await asyncio.sleep(1.0)
    assert edge_chan("N-FD")["voice_frames_played"] - before >= 60
    assert any(x["source_id"] == 4004 and x["talkgroup_id"] == 100 for x in req("GET", "/api/calls?limit=20"))
    # inhibit / uninhibit a radio
    await c.send({"t": "inhibit", "unit": 3003, "on": True})
    r = await c.wait_event("inhibit_result")
    assert r["ok"] and not next(s for s in req("GET", "/api/subscribers") if s["id"] == 3003)["enabled"], r
    await c.send({"t": "inhibit", "unit": 3003, "on": False})
    assert (await c.wait_event("inhibit_result"))["ok"]
    assert next(s for s in req("GET", "/api/subscribers") if s["id"] == 3003)["enabled"]
    # patch two talkgroups and unpatch
    async def profile_where(pred):
        end = time.time() + 5
        while time.time() < end:
            p = await c.wait_event("profile", max(0.1, end - time.time()))
            if pred(p):
                return p
        raise AssertionError("no matching profile")
    await c.send({"t": "patch", "tgs": [100, 300], "on": True})
    res = await c.wait_event("patch_result")
    assert res["ok"] and res["tgs"] == [100, 300], res
    await profile_where(lambda p: any(set(x["talkgroups"]) == {100, 300} for x in p["patches"]))
    # add a third talkgroup to the same patch: a call on 400 is heard on 100 and 300
    await c.send({"t": "patch", "id": res["id"], "tgs": [400], "on": True})
    assert (await c.wait_event("patch_result"))["tgs"] == [100, 300, 400]
    await profile_where(lambda p: any(set(x["talkgroups"]) == {100, 300, 400} for x in p["patches"]))
    # take one talkgroup out: the other two stay patched
    await c.send({"t": "patch", "tgs": [300], "on": False})
    assert (await c.wait_event("patch_result"))["ok"]
    await profile_where(lambda p: any(set(x["talkgroups"]) == {100, 400} for x in p["patches"]))
    await c.send({"t": "patch", "id": res["id"], "on": False})
    assert (await c.wait_event("patch_result"))["ok"]
    await profile_where(lambda p: not p["patches"])
    # a three-way patch in one go, and one with a talkgroup the console may not use
    await c.send({"t": "patch", "tgs": [100, 300, 400], "on": True, "name": "All ops"})
    res = await c.wait_event("patch_result")
    assert res["ok"] and next(x for x in req("GET", "/api/patches"))["name"] == "All ops", res
    await c.send({"t": "patch", "tgs": [100, 200], "on": True})
    assert (await c.wait_event("patch_result"))["reason"] == "not_allowed"
    await c.send({"t": "patch", "id": res["id"], "on": False})
    await profile_where(lambda p: not p["patches"])
    # shown online with its transport, then revoked
    row = next(x for x in req("GET", "/api/consoles") if x["id"] == con["id"])
    assert row["session"] and row["session"]["transport"] == "https" and row["wg_ip"], row
    req("POST", f"/api/consoles/{con['id']}/revoke", {})
    ev = await c.wait_event("auth_fail", 5)
    assert ev["reason"] == "console_revoked", ev
    await c.close()
    return ("activated (token + WireGuard keys), zones, radio -> console audio with talker, console talks over a "
            "radio, inhibit / uninhibit, multi-talkgroup patch / add / remove / unpatch, revoke")


def csharp_console_checks(tmp):
    """The Windows console's C# core, headless, against this server."""
    if not shutil.which("dotnet"):
        return "SKIPPED (install the .NET 8 SDK to run the C# console check)"
    out = f"{tmp}/headless"
    b = subprocess.run(["dotnet", "build", "-c", "Release", "-o", out, "--configfile",
                        f"{ROOT}/console/tests/HeadlessCheck/nuget.config", f"{ROOT}/console/tests/HeadlessCheck"],
                       capture_output=True, text=True, timeout=600)
    assert b.returncode == 0, b.stdout[-3000:]
    ch = {c["name"]: c["id"] for c in req("GET", "/status", base=EDGE, auth=False)["channels"]}
    con = req("POST", "/api/consoles", {"name": "Dispatch 2", "subscriber_id": 4004})
    code = req("POST", f"/api/consoles/{con['id']}/activation", {})["code"]
    p = subprocess.Popen(["dotnet", f"{out}/HeadlessCheck.dll", BASE, code], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                         stderr=open(f"{tmp}/headless.log", "w"), text=True, bufsize=1)

    def line(timeout=30):
        if not select.select([p.stdout], [], [], timeout)[0]:
            raise AssertionError(f"C# check silent ({tmp}/headless.log)")
        m = json.loads(p.stdout.readline())
        assert m["event"] != "error", m
        return m

    def cmd(c, timeout=30):
        p.stdin.write(c + "\n")
        p.stdin.flush()
        return line(timeout)
    try:
        mx = line()
        assert mx["event"] == "mixer" and mx["ok"], mx
        a = line()
        assert a["g711"] and a["store_ok"] and a["blob_ok"] and a["plan"] == ["wireguard", "https"], a
        c = line()
        assert c["refused"] is None and c["zones"] == ["Ops"] and c["rights"]["Inhibit"], c
        cmd("monitor 100,300")
        sim_key(ch["N-FD"], 1.5, 3003)
        time.sleep(2.5)
        rx = cmd("rx")
        assert rx["frames"].get("100", 0) >= 60 and any(x["Src"] == 3003 for x in rx["calls"]), rx
        assert rx["peak"] > 1000, rx
        time.sleep(1)
        before = edge_chan("N-FD")["voice_frames_played"]
        t = cmd("talk 100 1.5")
        assert t["result"] == "grant:100" and t["sent"] >= 70, t
        time.sleep(1)
        assert edge_chan("N-FD")["voice_frames_played"] - before >= 60
        assert cmd("inhibit 3003 1")["result"] == "inhibit:3003:True:True"
        assert cmd("inhibit 3003 0")["result"] == "inhibit:3003:False:True"
        pa = cmd("patch 100,300,400")
        assert "patch:True:" in pa["events"] and [x["Talkgroups"] for x in pa["patches"]] == [[100, 300, 400]], pa
        pa = cmd(f"unpatch {pa['patches'][0]['Id']}")
        assert "patch:True:" in pa["events"] and pa["patches"] == [], pa
        p.stdin.write("quit\n")
        p.stdin.flush()
    finally:
        try:
            p.wait(5)
        except subprocess.TimeoutExpired:
            p.kill()
    return (f"G.711 bit-exact, DPAPI-style settings, {a['blob_bytes']}-byte WireGuardNT config, tunnel-first plan, "
            f"heard radio 3003 ({rx['frames']['100']} frames, mixer peak {rx['peak']:.0f}), PTT, inhibit, 3-way patch; "
            f"mixer routing / per-TG side / priority ducking / VU levels ({len(mx['checks'])} checks)")


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
        req("POST", "/api/talkgroups", {"id": 400, "name": "Mutual Aid"})
        for slot, name, tg in [(1, "N-FD", 100), (2, "N-PW", 200)]:
            req("POST", "/api/channels", {"site_id": 1, "slot": slot, "name": name, "mode": "ANALOG", "talkgroup_id": tg})
        req("POST", "/api/zones", {"name": "Admin", "position": 2, "talkgroup_ids": [200]})
        req("POST", "/api/zones", {"name": "Ops", "position": 1, "talkgroup_ids": [100, 300, 400]})
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
        print(f"ok  Recording: {recording_checks(tmp)}")
        print(f"ok  Dispatch console: {asyncio.run(console_checks())}")
        print(f"ok  Windows console core (C#): {csharp_console_checks(tmp)}")
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
