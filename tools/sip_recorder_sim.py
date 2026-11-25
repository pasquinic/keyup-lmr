#!/usr/bin/env python3
"""A minimal SIP recording server (UAS) for testing KeyUp's recording shipping.

  python3 tools/sip_recorder_sim.py --port 15160 --user rec --password secret --out /tmp/rec.json

Answers INVITEs over UDP after a digest challenge, accepts plain SDP or SIPREC multipart bodies,
counts the RTP it receives, and on BYE appends a JSON line per call to --out:
  {"call_id", "from", "siprec", "require", "contact", "x_headers", "metadata", "rtp_packets", "rtp_bytes"}
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
import secrets

REALM = "keyup-lab"


def parse(data: bytes):
    head, _, body = data.partition(b"\r\n\r\n")
    lines = head.decode(errors="replace").split("\r\n")
    hdrs = {}
    for line in lines[1:]:
        if ":" in line:
            k, v = line.split(":", 1)
            hdrs.setdefault(k.strip().lower(), []).append(v.strip())
    return lines[0], hdrs, body


class Rtp(asyncio.DatagramProtocol):
    def __init__(self, call):
        self.call = call

    def datagram_received(self, data, addr):
        if len(data) > 12 and data[1] & 0x7F == 0:
            self.call["rtp_packets"] += 1
            self.call["rtp_bytes"] += len(data) - 12


class Uas(asyncio.DatagramProtocol):
    def __init__(self, args):
        self.a = args
        self.calls: dict[str, dict] = {}
        self.nonces: set[str] = set()

    def connection_made(self, transport):
        self.tr = transport

    def reply(self, addr, req_hdrs, code, reason, extra=None, body=b"", ctype=""):
        lines = [f"SIP/2.0 {code} {reason}"]
        for h in ("via", "from", "call-id", "cseq"):
            for v in req_hdrs.get(h, []):
                lines.append(f"{h.title() if h != 'cseq' else 'CSeq'}: {v}" if h != "call-id" else f"Call-ID: {v}")
        to = req_hdrs.get("to", [""])[0]
        if code >= 200 and ";tag=" not in to:
            to += ";tag=" + self.calls.get(req_hdrs.get("call-id", [""])[0], {}).get("tag", "uas" + secrets.token_hex(3))
        lines.append(f"To: {to}")
        for k, v in (extra or {}).items():
            lines.append(f"{k}: {v}")
        if ctype:
            lines.append(f"Content-Type: {ctype}")
        lines.append(f"Content-Length: {len(body)}")
        self.tr.sendto(("\r\n".join(lines) + "\r\n\r\n").encode() + body, addr)

    def check_auth(self, hdrs, method) -> bool:
        auth = hdrs.get("authorization", [""])[0]
        if not auth.startswith("Digest"):
            return False
        p = dict(re.findall(r'(\w+)="?([^",]*)"?', auth))
        if p.get("nonce") not in self.nonces or p.get("username") != self.a.user:
            return False
        md5 = lambda s: hashlib.md5(s.encode()).hexdigest()  # noqa: E731
        ha1, ha2 = md5(f"{self.a.user}:{REALM}:{self.a.password}"), md5(f"{method}:{p.get('uri')}")
        if p.get("qop") == "auth":
            exp = md5(f"{ha1}:{p['nonce']}:{p['nc']}:{p['cnonce']}:auth:{ha2}")
        else:
            exp = md5(f"{ha1}:{p['nonce']}:{ha2}")
        return exp == p.get("response")

    def datagram_received(self, data, addr):
        first, hdrs, body = parse(data)
        method = first.split(" ", 1)[0]
        cid = hdrs.get("call-id", [""])[0]
        if method == "INVITE":
            if not self.check_auth(hdrs, "INVITE"):
                nonce = secrets.token_hex(8)
                self.nonces.add(nonce)
                self.reply(addr, hdrs, 401, "Unauthorized",
                           {"WWW-Authenticate": f'Digest realm="{REALM}", nonce="{nonce}", qop="auth", algorithm=MD5'})
                return
            asyncio.get_running_loop().create_task(self.answer(addr, hdrs, body, cid))
        elif method == "ACK":
            pass
        elif method == "BYE":
            self.reply(addr, hdrs, 200, "OK")
            call = self.calls.pop(cid, None)
            if call:
                call.pop("tag", None)
                tr = call.pop("_rtp", None)
                if tr:
                    tr.close()
                with open(self.a.out, "a") as f:
                    f.write(json.dumps(call) + "\n")

    async def answer(self, addr, hdrs, body, cid):
        if cid in self.calls:
            return
        call = {"call_id": cid, "from": hdrs.get("from", [""])[0], "require": hdrs.get("require", [""])[0],
                "contact": hdrs.get("contact", [""])[0], "siprec": False, "metadata": "", "rtp_packets": 0,
                "rtp_bytes": 0, "tag": "uas" + secrets.token_hex(3),
                "x_headers": {k: v[0] for k, v in hdrs.items() if k.startswith("x-keyup")}}
        ctype = hdrs.get("content-type", [""])[0]
        text = body.decode(errors="replace")
        if ctype.lower().startswith("multipart/mixed"):
            call["siprec"] = True
            m = re.search(r"(?s)<\?xml.*?</recording>", text)
            call["metadata"] = m.group(0) if m else ""
        self.calls[cid] = call
        tr, _ = await asyncio.get_running_loop().create_datagram_endpoint(lambda: Rtp(call), local_addr=("127.0.0.1", 0))
        call["_rtp"] = tr
        port = tr.get_extra_info("sockname")[1]
        sdp = ("v=0\r\no=rec 1 1 IN IP4 127.0.0.1\r\ns=rec\r\nc=IN IP4 127.0.0.1\r\nt=0 0\r\n"
               f"m=audio {port} RTP/AVP 0\r\na=rtpmap:0 PCMU/8000\r\na=recvonly\r\n" + ("a=label:1\r\n" if call["siprec"] else ""))
        self.reply(addr, hdrs, 200, "OK", {"Contact": f"<sip:rec@127.0.0.1:{self.a.port}>"}, sdp.encode(), "application/sdp")


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=15160)
    ap.add_argument("--user", default="rec")
    ap.add_argument("--password", default="secret")
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    loop = asyncio.get_running_loop()
    await loop.create_datagram_endpoint(lambda: Uas(a), local_addr=("127.0.0.1", a.port))
    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
