#!/usr/bin/env python3
"""Packet-voice lab: DFSI, IP Site Connect and Capacity Plus links on one edge, with simulated equipment.

  python3 tests/lab_packet_e2e.py

Runs central (with fake DVSI vocoders on pseudo-terminals, driven by the real DVSI driver), one edge
with an analog radio (sim), an IPSC link acting as master, a DFSI link and a Capacity Plus link, plus
two simulated MOTOTRBO repeaters, a simulated Cap+ master and a simulated P25 fixed station.

Checks:
  * links come up: repeaters register to the edge (with IPSC authentication), the edge joins the
    Cap+ master, the DFSI control link connects
  * DMR -> DMR pass-through: an IPSC call reaches the Cap+ system untouched (no vocoder)
  * P25 DFSI -> analog radio, DMR repeaters and a PoC phone, transcoded through the DVSI vocoders,
    talkgroup and radio ID carried from the P25 link control into the DMR headers
  * analog radio -> DFSI station (IMBE) and repeaters (AMBE+2) with the radio's ID
  * a digital call is recorded as audio
"""
import asyncio
import os
import shutil
import subprocess
import sys
import tempfile
import time

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path[:0] = [ROOT, os.path.join(ROOT, "tests"), os.path.join(ROOT, "tools")]

import lab_e2e as lab  # noqa: E402
from lab_e2e import wait  # noqa: E402
from poc_client import PocClient, activate  # noqa: E402

PORT = 18280
lab.PORT, lab.BASE = PORT, f"http://127.0.0.1:{PORT}"
BASE = lab.BASE
EDGE = "http://127.0.0.1:18291"
REP_A, REP_B, CAPP, P25 = "http://127.0.0.1:18301", "http://127.0.0.1:18302", "http://127.0.0.1:18309", "http://127.0.0.1:18310"


def req(method, path, body=None, base=None, auth=True):
    return lab.req(method, path, body, base or BASE, auth)


def sim(base, path, method="GET"):
    return lab.req(method, path, {} if method == "POST" else None, base, False)


def chan(name):
    return next(c for c in req("GET", "/status", base=EDGE, auth=False)["channels"] if c["name"] == name)


def last_call(base, tg, src=None):
    calls = [c for c in sim(base, "/status")["calls"] if c["tg"] == tg and (src is None or c["src"] == src)]
    return calls[-1] if calls else None


def dvsi_counts(tmp):
    try:
        stats = eval(open(f"{tmp}/dvsi/stats").read())  # written by tools/fake_dvsi.py
    except Exception:
        return 0, 0
    return sum(v["encode"] for v in stats.values()), sum(v["decode"] for v in stats.values())


def main():
    tmp = tempfile.mkdtemp(prefix="keyup-packet-")
    procs = []

    def spawn(args, name, env=None, cwd=ROOT):
        p = subprocess.Popen(args, cwd=cwd, env=env, stdout=open(f"{tmp}/{name}.log", "w"), stderr=subprocess.STDOUT)
        procs.append(p)
        return p

    try:
        spawn([sys.executable, f"{ROOT}/tools/fake_dvsi.py", "--count", "6", "--dir", f"{tmp}/dvsi"], "fake_dvsi")
        wait(lambda: os.path.exists(f"{tmp}/dvsi/dvsi5"), 10, "fake DVSI devices")
        env = {**os.environ, "PYTHONPATH": f"{ROOT}:{ROOT}/central", "LMR_DATA_DIR": tmp,
               "DATABASE_URL": f"sqlite:///{tmp}/lmr.db", "WG_ENABLED": "false", "ALLOW_UNTUNNELED_EDGES": "true",
               "HTTP_PORT": str(PORT), "VOICE_PORT": "15300", "HTTP_HOST": "127.0.0.1", "LOG_LEVEL": "WARNING",
               "PUBLIC_URL": BASE, "DVSI_PORTS": ",".join(f"{tmp}/dvsi/dvsi{i}" for i in range(6))}
        spawn([sys.executable, "-m", "app.main"], "central", env, f"{ROOT}/central")
        wait(lambda: req("GET", "/healthz", auth=False)["ok"], msg="central")
        lab.TOKEN = req("POST", "/api/auth/login", {"username": "admin", "password": "admin"}, auth=False)["token"]
        req("POST", "/api/sites", {"id": 1, "name": "Hilltop"})
        req("POST", "/api/talkgroups", {"id": 9, "name": "DMR Local", "hang_time_ms": 500})
        req("POST", "/api/talkgroups", {"id": 100, "name": "Fire", "hang_time_ms": 500})
        req("POST", "/api/channels", {"site_id": 1, "slot": 1, "name": "ANA-100", "mode": "ANALOG", "talkgroup_id": 100})
        req("POST", "/api/channels", {"site_id": 1, "slot": 2, "name": "IPSC-M", "mode": "DMR", "talkgroup_id": 9,
                                      "extra_talkgroups": "100", "timeslot": 1, "interface": "ipsc",
                                      "packet_config": {"role": "master", "local_port": 15200, "peer_id": 9990,
                                                        "auth_key": "1A2B3C", "timeslots": "both"}})
        req("POST", "/api/channels", {"site_id": 1, "slot": 3, "name": "DFSI-1", "mode": "P25P1", "talkgroup_id": 100,
                                      "extra_talkgroups": "9", "interface": "dfsi",
                                      "packet_config": {"local_port": 15211, "remote_host": "127.0.0.1", "remote_port": 15210,
                                                        "heartbeat_s": 2}})
        req("POST", "/api/channels", {"site_id": 1, "slot": 4, "name": "CAPP", "mode": "DMR", "talkgroup_id": 9,
                                      "interface": "capplus",
                                      "packet_config": {"local_port": 15208, "remote_host": "127.0.0.1", "remote_port": 15209,
                                                        "peer_id": 9991}})
        for uid, alias in [(1001, "Analog portable"), (3101, "DMR portable"), (4101, "P25 portable"), (5001, "Phone")]:
            req("POST", "/api/subscribers", {"id": uid, "alias": alias, "poc_enabled": uid == 5001})
        checks = [c for c in req("GET", "/api/network")["checks"] if c["scope"].startswith("channel")]
        assert not checks, checks
        # equipment
        spawn([sys.executable, f"{ROOT}/tools/dfsi_sim.py", "--port", "15210", "--api", "18310"], "dfsi_sim")
        spawn([sys.executable, f"{ROOT}/tools/ipsc_sim.py", "--peer-id", "311009", "--port", "15209", "--be-master",
               "--capplus", "--api", "18309"], "capp_sim")
        tok = req("POST", "/api/enrollment/tokens", {"site_id": 1, "auto_approve": True})["token"]
        os.makedirs(f"{tmp}/edge1")
        with open(f"{tmp}/edge1/config.yaml", "w") as f:
            f.write(f"server_url: {BASE}\ncontrol_url: {BASE}\nenrollment_token: {tok}\nstate_dir: {tmp}/edge1/state\n"
                    "radio_backend: sim\nwireguard: {enabled: false}\nlocal_api: {port: 18291}\nlog_level: INFO\n")
        spawn([sys.executable, "-m", "lmr_edge", "-c", f"{tmp}/edge1/config.yaml"], "edge1",
              {**env, "PYTHONPATH": f"{ROOT}:{ROOT}/edge"}, f"{ROOT}/edge")
        wait(lambda: req("GET", "/healthz", auth=False)["sites"] == 1, msg="edge connected")
        wait(lambda: len(req("GET", "/status", base=EDGE, auth=False)["channels"]) == 4, msg="config push")
        for pid, port, api in [(311001, 15201, 18301), (311002, 15202, 18302)]:
            spawn([sys.executable, f"{ROOT}/tools/ipsc_sim.py", "--peer-id", str(pid), "--port", str(port),
                   "--master", "127.0.0.1:15200", "--auth-key", "1A2B3C", "--api", str(api)], f"rep{pid}")
        wait(lambda: chan("IPSC-M")["peers"] == [311001, 311002], 20, "repeaters registered (authenticated)")
        wait(lambda: chan("DFSI-1")["link_up"] and sim(P25, "/status")["connected"], 20, "DFSI link")
        wait(lambda: chan("CAPP")["registered"], 20, "Cap+ registration")
        print("ok  links: 2 repeaters registered to the edge's IPSC master (HMAC auth), Cap+ joined, DFSI connected")

        # DMR -> DMR: untouched pass-through to the Cap+ system
        enc0, dec0 = dvsi_counts(tmp)
        sim(REP_A, "/call?tg=9&src=3101&seconds=2", "POST")
        time.sleep(1.5)
        c = last_call(CAPP, 9, 3101)
        assert c and c["frames"] >= 90 and c["header"], c
        print(f"ok  DMR -> DMR pass-through: Cap+ system got TG 9 from radio 3101, {c['frames']} AMBE+2 frames")

        # P25 DFSI -> analog radio + DMR repeaters + phone (transcoded), talkgroup and radio ID carried
        token = activate(BASE, 5001, req("POST", "/api/subscribers/5001/poc/activation", {})["code"])["token"]

        async def p25_call():
            phone = PocClient(BASE, token)
            assert (await phone.connect())["t"] == "auth_ok"
            await phone.select(100)
            before = chan("ANA-100")["voice_frames_played"]
            await asyncio.to_thread(sim, P25, "/call?tg=100&src=4101&seconds=2", "POST")
            await asyncio.sleep(1.5)
            played = chan("ANA-100")["voice_frames_played"] - before
            await phone.close()
            return played, phone.rx_frames.get(100, 0), phone.rx_srcs
        played, phone_frames, srcs = asyncio.run(p25_call())
        ca, cb = last_call(REP_A, 100, 4101), last_call(REP_B, 100, 4101)
        assert played >= 80, f"analog radio played {played}"
        assert ca and cb and ca["frames"] >= 80 and cb["frames"] >= 80, (ca, cb)
        assert phone_frames >= 80 and 4101 in srcs, (phone_frames, srcs)
        enc1, dec1 = dvsi_counts(tmp)
        assert dec1 - dec0 >= 80 and enc1 - enc0 >= 80, (enc1 - enc0, dec1 - dec0)
        rec = next(c for c in req("GET", "/api/calls?limit=20") if c["talkgroup_id"] == 100 and c["source_id"] == 4101)
        assert rec["source_codec"] == "IMBE" and rec["transcoded"], rec
        wait(lambda: next(c for c in req("GET", "/api/calls?limit=20") if c["id"] == rec["id"])["recording"].endswith(".ul"),
             10, "digital call recorded as audio")
        print(f"ok  P25 DFSI -> analog ({played} frames), both DMR repeaters ({ca['frames']} AMBE+2 frames, "
              f"TG 100 / radio 4101 in the headers), PoC phone ({phone_frames}); transcoded on DVSI, recorded")

        # analog radio -> P25 station + DMR repeaters
        time.sleep(1)
        ana = chan("ANA-100")["id"]
        req("POST", f"/sim/key?channel={ana}&seconds=1.5&unit=1001", {}, base=EDGE, auth=False)
        time.sleep(2.5)
        p = last_call(P25, 100, 1001)
        ca = last_call(REP_A, 100, 1001)
        assert p and p["frames"] >= 60 and p["header_tg"] == 100, p
        assert ca and ca["frames"] >= 60, ca
        print(f"ok  analog radio 1001 -> P25 station ({p['frames']} IMBE frames, link control TG 100 / radio 1001) "
              f"and DMR repeaters ({ca['frames']} AMBE+2 frames)")

        # DMR repeater -> P25 station on TG 9 (carried as an extra talkgroup by the DFSI link)
        time.sleep(1)
        sim(REP_B, "/call?tg=9&src=3101&seconds=1.5", "POST")
        time.sleep(1.5)
        p = last_call(P25, 9, 3101)
        assert p and p["frames"] >= 60, p
        print(f"ok  DMR repeater -> P25 station on TG 9 ({p['frames']} IMBE frames)")
        print("\nALL PACKET-VOICE LAB TESTS PASSED")
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
