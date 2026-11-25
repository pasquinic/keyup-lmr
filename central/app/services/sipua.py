"""A small SIP user agent client (RFC 3261) for sending recordings to another server.

It places one outgoing call, streams G.711 mu-law RTP (RFC 3550, payload type 0) in real time, and
hangs up:  INVITE -> (401/407 digest auth, RFC 2617/7616 MD5) -> 200 OK -> ACK -> RTP -> BYE.

Two flavours, chosen by the caller:
  * plain SIP:  an ordinary call to a recorder extension / voicemail / PBX (SDP offer, sendonly)
  * SIPREC:     a recording session (RFC 7866) -- multipart/mixed body with the SDP and an
                rs-metadata XML document (RFC 7865) describing the talkgroup call and its talkers;
                Contact carries the +sip.src feature tag and Require: siprec.

UDP and TCP transports. No TLS / SRTP (run it over a VPN or LAN to the recorder).
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import random
import re
import secrets
import socket
import struct
import time
from dataclasses import dataclass, field

log = logging.getLogger("sipua")
RTP_HDR = struct.Struct("!BBHII")


class SipError(RuntimeError):
    pass


@dataclass
class SipTarget:
    user: str
    host: str
    port: int = 5060
    transport: str = "udp"

    @classmethod
    def parse(cls, uri: str, transport: str = "udp") -> "SipTarget":
        m = re.match(r"^(?:sips?:)?(?:([^@;]+)@)?([^:;]+)(?::(\d+))?(.*)$", uri.strip())
        if not m:
            raise SipError(f"bad SIP URI {uri!r}")
        params = m.group(4) or ""
        t = re.search(r";transport=(\w+)", params, re.I)
        return cls(m.group(1) or "", m.group(2), int(m.group(3) or 5060), (t.group(1) if t else transport).lower())

    @property
    def uri(self) -> str:
        return f"sip:{self.user + '@' if self.user else ''}{self.host}:{self.port}"


@dataclass
class SipResponse:
    status: int
    reason: str
    headers: dict = field(default_factory=dict)   # lower-case name -> list of values
    body: bytes = b""

    def h(self, name: str, default: str = "") -> str:
        v = self.headers.get(name.lower())
        return v[0] if v else default


def parse_message(data: bytes) -> SipResponse | None:
    head, _, body = data.partition(b"\r\n\r\n")
    lines = head.decode(errors="replace").split("\r\n")
    m = re.match(r"^SIP/2\.0 (\d{3}) ?(.*)$", lines[0])
    if not m:
        return None
    hdrs: dict = {}
    compact = {"v": "via", "f": "from", "t": "to", "i": "call-id", "m": "contact", "l": "content-length",
               "c": "content-type"}
    for line in lines[1:]:
        if ":" in line:
            k, v = line.split(":", 1)
            k = compact.get(k.strip().lower(), k.strip().lower())
            hdrs.setdefault(k, []).append(v.strip())
    return SipResponse(int(m.group(1)), m.group(2), hdrs, body)


def digest_header(challenge: str, method: str, uri: str, user: str, password: str, nc: int = 1) -> str:
    """Authorization value for a WWW-/Proxy-Authenticate Digest challenge (MD5, qop=auth or none)."""
    params = dict(re.findall(r'(\w+)="?([^",]*)"?', challenge))
    realm, nonce = params.get("realm", ""), params.get("nonce", "")
    algo = params.get("algorithm", "MD5")
    if algo.upper() not in ("MD5", ""):
        raise SipError(f"unsupported digest algorithm {algo}")
    md5 = lambda s: hashlib.md5(s.encode()).hexdigest()  # noqa: E731 - SIP digest is MD5 by spec
    ha1 = md5(f"{user}:{realm}:{password}")
    ha2 = md5(f"{method}:{uri}")
    qops = [q.strip() for q in params.get("qop", "").split(",") if q.strip()]
    out = f'Digest username="{user}", realm="{realm}", nonce="{nonce}", uri="{uri}", algorithm=MD5'
    if "auth" in qops:
        cnonce = secrets.token_hex(8)
        ncs = f"{nc:08x}"
        resp = md5(f"{ha1}:{nonce}:{ncs}:{cnonce}:auth:{ha2}")
        out += f', response="{resp}", qop=auth, nc={ncs}, cnonce="{cnonce}"'
    else:
        out += f', response="{md5(f"{ha1}:{nonce}:{ha2}")}"'
    if "opaque" in params:
        out += f', opaque="{params["opaque"]}"'
    return out


def local_ip_for(host: str) -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((host, 9))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


class _Udp(asyncio.DatagramProtocol):
    def __init__(self, q: asyncio.Queue):
        self.q = q

    def datagram_received(self, data, addr):
        self.q.put_nowait(data)


class SipCall:
    """One outgoing call that streams a mu-law recording and hangs up."""

    def __init__(self, target: SipTarget, from_user: str, from_name: str, username: str = "", password: str = "",
                 extra_headers: dict | None = None, timeout: float = 8.0, contact_params: str = ""):
        self.t = target
        self.contact_params = contact_params   # e.g. ";+sip.src" for a SIPREC recording client
        self.from_user, self.from_name = from_user, from_name
        self.username, self.password = username, password
        self.extra = extra_headers or {}
        self.timeout = timeout
        self.call_id = f"{secrets.token_hex(12)}@keyup"
        self.from_tag = secrets.token_hex(6)
        self.to_tag = ""
        self.cseq = random.randint(1, 10000)
        self.local_ip = local_ip_for(target.host)
        self.q: asyncio.Queue = asyncio.Queue()
        self.sip_transport = None
        self.reader = self.writer = None
        self.local_port = 0
        self.route_uri = target.uri
        self.remote_rtp: tuple[str, int] | None = None
        self.rtp_transport = None
        self.rtp_port = 0
        self.packets_sent = 0

    # -------------------------------------------------------------- transport
    async def _open(self) -> None:
        loop = asyncio.get_running_loop()
        rtp_t, _ = await loop.create_datagram_endpoint(asyncio.DatagramProtocol, local_addr=("0.0.0.0", 0))
        self.rtp_transport, self.rtp_port = rtp_t, rtp_t.get_extra_info("sockname")[1]
        if self.t.transport == "tcp":
            self.reader, self.writer = await asyncio.wait_for(asyncio.open_connection(self.t.host, self.t.port), self.timeout)
            self.local_port = self.writer.get_extra_info("sockname")[1]
            loop.create_task(self._tcp_reader())
        else:
            tr, _ = await loop.create_datagram_endpoint(lambda: _Udp(self.q), local_addr=("0.0.0.0", 0),
                                                        remote_addr=(self.t.host, self.t.port))
            self.sip_transport = tr
            self.local_port = tr.get_extra_info("sockname")[1]

    async def _tcp_reader(self) -> None:
        buf = b""
        try:
            while True:
                chunk = await self.reader.read(65536)
                if not chunk:
                    return
                buf += chunk
                while b"\r\n\r\n" in buf:
                    head, rest = buf.split(b"\r\n\r\n", 1)
                    m = re.search(rb"(?im)^(?:content-length|l)\s*:\s*(\d+)", head)
                    n = int(m.group(1)) if m else 0
                    if len(rest) < n:
                        break
                    self.q.put_nowait(head + b"\r\n\r\n" + rest[:n])
                    buf = rest[n:]
        except Exception:
            return

    def _send(self, data: bytes) -> None:
        if self.writer:
            self.writer.write(data)
        else:
            self.sip_transport.sendto(data)

    def close(self) -> None:
        for tr in (self.sip_transport, self.rtp_transport):
            if tr:
                tr.close()
        if self.writer:
            self.writer.close()

    # -------------------------------------------------------------- messages
    def _request(self, method: str, body: bytes = b"", ctype: str = "", auth: str = "", auth_hdr: str = "",
                 extra: dict | None = None, branch: str | None = None) -> bytes:
        tp = "TCP" if self.t.transport == "tcp" else "UDP"
        branch = branch or "z9hG4bK" + secrets.token_hex(8)
        to = f"<{self.t.uri}>" + (f";tag={self.to_tag}" if self.to_tag else "")
        lines = [f"{method} {self.route_uri} SIP/2.0",
                 f"Via: SIP/2.0/{tp} {self.local_ip}:{self.local_port};branch={branch};rport",
                 "Max-Forwards: 70",
                 f'From: "{self.from_name}" <sip:{self.from_user}@{self.local_ip}>;tag={self.from_tag}',
                 f"To: {to}", f"Call-ID: {self.call_id}", f"CSeq: {self.cseq} {method}",
                 "User-Agent: KeyUp-Recorder/1.0"]
        if method == "INVITE":
            lines.append(f"Contact: <sip:{self.from_user}@{self.local_ip}:{self.local_port}"
                         f"{';transport=tcp' if self.writer else ''}>{self.contact_params}")
        if auth:
            lines.append(f"{auth_hdr}: {auth}")
        for k, v in {**(extra or {})}.items():
            lines.append(f"{k}: {v}")
        if ctype:
            lines.append(f"Content-Type: {ctype}")
        lines.append(f"Content-Length: {len(body)}")
        return ("\r\n".join(lines) + "\r\n\r\n").encode() + body

    async def _await_final(self, deadline: float) -> SipResponse:
        while True:
            left = deadline - time.time()
            if left <= 0:
                raise SipError("SIP request timed out")
            try:
                data = await asyncio.wait_for(self.q.get(), left)
            except asyncio.TimeoutError:
                raise SipError("SIP request timed out")
            r = parse_message(data)
            if r is None or self.call_id not in r.h("call-id"):
                continue
            if r.status >= 200:
                return r

    async def _transact(self, method: str, body: bytes = b"", ctype: str = "", extra: dict | None = None) -> SipResponse:
        auth, auth_hdr = "", ""
        for attempt in range(3):
            msg = self._request(method, body, ctype, auth, auth_hdr, extra)
            deadline = time.time() + self.timeout
            resend = 0.5
            self._send(msg)
            while True:   # UDP retransmission (timer A) until a final response or the deadline
                try:
                    r = await asyncio.wait_for(self._await_final(deadline), resend if not self.writer else self.timeout)
                    break
                except (asyncio.TimeoutError, SipError):
                    if time.time() >= deadline:
                        raise SipError(f"{method}: no answer from {self.t.host}:{self.t.port}")
                    self._send(msg)
                    resend = min(resend * 2, 4.0)
            if r.status in (401, 407) and attempt == 0 and self.username:
                hdr = "www-authenticate" if r.status == 401 else "proxy-authenticate"
                auth = digest_header(r.h(hdr), method, self.route_uri, self.username, self.password)
                auth_hdr = "Authorization" if r.status == 401 else "Proxy-Authorization"
                if method == "INVITE":
                    self._send(self._ack(r))
                self.cseq += 1
                continue
            return r
        raise SipError(f"{method}: authentication failed")

    def _ack(self, r: SipResponse) -> bytes:
        """ACK for an INVITE final response (same CSeq number; for 2xx a new transaction)."""
        tag = re.search(r";tag=([^;>\s]+)", r.h("to"))
        saved = self.to_tag
        self.to_tag = tag.group(1) if tag else self.to_tag
        msg = self._request("ACK")
        if r.status >= 300:
            self.to_tag = saved
        return msg

    # -------------------------------------------------------------- public
    def sdp_offer(self, label: str | None = None) -> str:
        sess = random.randint(1, 2 ** 31)
        lines = ["v=0", f"o=keyup {sess} {sess} IN IP4 {self.local_ip}", "s=KeyUp recording",
                 f"c=IN IP4 {self.local_ip}", "t=0 0", f"m=audio {self.rtp_port} RTP/AVP 0",
                 "a=rtpmap:0 PCMU/8000", "a=ptime:20", "a=sendonly"]
        if label:
            lines.append(f"a=label:{label}")
        return "\r\n".join(lines) + "\r\n"

    async def start(self, body: bytes, ctype: str, extra: dict | None = None) -> SipResponse:
        await self._open()
        if callable(body):
            body = body(self)
        r = await self._transact("INVITE", body, ctype, {**self.extra, **(extra or {})})
        if r.status != 200:
            raise SipError(f"INVITE refused: {r.status} {r.reason}")
        tag = re.search(r";tag=([^;>\s]+)", r.h("to"))
        self.to_tag = tag.group(1) if tag else ""
        contact = re.search(r"<([^>]+)>", r.h("contact"))
        if contact:
            self.route_uri = contact.group(1)   # in-dialog requests (ACK, BYE) go to the remote target
        self._send(self._ack(r))
        self.remote_rtp = self._answer_rtp(r.body)
        return r

    def _answer_rtp(self, body: bytes) -> tuple[str, int] | None:
        text = body.decode(errors="replace")
        if "multipart" in text[:200].lower() or "--" in text[:4]:
            m = re.search(r"(?s)v=0.*?(?=\r?\n--|\Z)", text)
            text = m.group(0) if m else text
        c = re.search(r"(?m)^c=IN IP4 (\S+)", text)
        m = re.search(r"(?m)^m=audio (\d+)", text)
        if not (c and m) or int(m.group(1)) == 0:
            return None
        return c.group(1), int(m.group(1))

    async def stream_ulaw(self, ulaw: bytes, pace: float = 1.0) -> int:
        """Send the recording as 20 ms RTP packets, in real time (pace=1) or faster."""
        if not self.remote_rtp:
            return 0
        ssrc, seq, ts = random.getrandbits(32), random.getrandbits(16), random.getrandbits(32)
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        for i in range(0, len(ulaw), 160):
            chunk = ulaw[i:i + 160].ljust(160, b"\xff")
            mark = 0x80 if i == 0 else 0
            self.rtp_transport.sendto(RTP_HDR.pack(0x80, mark | 0, seq, ts, ssrc) + chunk, self.remote_rtp)
            seq, ts = (seq + 1) & 0xFFFF, (ts + 160) & 0xFFFFFFFF
            self.packets_sent += 1
            delay = t0 + (i // 160 + 1) * 0.02 / max(pace, 0.01) - loop.time()
            if delay > 0:
                await asyncio.sleep(delay)
        return self.packets_sent

    async def hangup(self) -> None:
        self.cseq += 1
        try:
            await self._transact("BYE")
        except SipError as e:
            log.debug("BYE: %s", e)
