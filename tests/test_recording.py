"""Recorder and shipping helpers (no network).

Run:  PYTHONPATH=.:central pytest -q tests
"""
import json
import os
import sys
import tempfile
import time
import xml.dom.minidom
from types import SimpleNamespace

_tmp = tempfile.mkdtemp()
os.environ.setdefault("LMR_DATA_DIR", _tmp)
os.environ.setdefault("DATABASE_URL", f"sqlite:///{_tmp}/r.db")
sys.path[:0] = [os.path.join(os.path.dirname(__file__), ".."), os.path.join(os.path.dirname(__file__), "..", "central")]

from app.config import settings  # noqa: E402
from app.db import Base, engine, session_scope  # noqa: E402
from app.models import CallRecord  # noqa: E402
from app.services import recorder as R  # noqa: E402
from app.services.shipping import Shipper  # noqa: E402
from app.services.sipua import SipTarget, digest_header, parse_message  # noqa: E402
from lmrcommon.modes import Codec  # noqa: E402
from lmrcommon.protocol import FrameType, VoiceFrame  # noqa: E402


class Ctl:
    def unit_alias(self, u):
        return {1001: "E1"}.get(u, str(u))

    def endpoint_label(self, k):
        return k


def call(record_id, tg=100):
    return SimpleNamespace(call_id=record_id + 1000, record_id=record_id, tg=tg, parrot=None, src_key="site:1")


def setup_module():
    Base.metadata.create_all(engine)


def test_digital_call_without_vocoder_keeps_raw_frames():
    settings.data_dir = _tmp
    rec = R.Recorder(Ctl())
    with session_scope() as db:
        db.add(CallRecord(id=1, call_id=1001, talkgroup_id=100))
    c = call(1)
    for i in range(5):
        rec.frame(c, VoiceFrame(FrameType.VOICE, Codec.IMBE, 1, c.call_id, 100, 1001, i, 0, bytes(11)), None)
    rec.call_ended(c)
    with session_scope() as db:
        r = db.get(CallRecord, 1)
        assert r.recording.endswith(".imbe") and r.recording_bytes == 5 * 13 and "raw IMBE" in r.recording_note
        assert json.loads(r.talkers)[0]["alias"] == "E1"


def test_ulaw_call_keeps_gaps_and_retention_removes_old_files():
    settings.data_dir = _tmp
    rec = R.Recorder(Ctl())
    with session_scope() as db:
        db.add(CallRecord(id=2, call_id=1002, talkgroup_id=100))
    c = call(2)
    f = VoiceFrame(FrameType.VOICE, Codec.ULAW, 1, c.call_id, 100, 1001, 0, 0, b"\x10" * 160)
    rec.frame(c, f, f.payload)
    rec.takes[c.call_id].last_frame -= 0.5          # half a second of silence before the next talker
    rec.frame(c, VoiceFrame(FrameType.VOICE, Codec.ULAW, 1, c.call_id, 100, 2002, 1, 0, f.payload), f.payload)
    rec.call_ended(c)
    with session_scope() as db:
        r = db.get(CallRecord, 2)
        assert r.recording_bytes == 160 * (2 + 24) and [t["src"] for t in json.loads(r.talkers)] == [1001, 2002]
        path = R.Recorder.path_of(r)
    wav = R.Recorder.wav_bytes(path)
    assert wav[:4] == b"RIFF" and len(wav) == 44 + 2 * r.recording_bytes
    old = time.time() - 40 * 86400
    os.utime(path, (old, old))
    assert rec.enforce_retention(30, 20) == 1 and not os.path.exists(path)
    with session_scope() as db:
        assert db.get(CallRecord, 2).recording_note == "deleted by retention"


def test_siprec_metadata_is_well_formed():
    meta = {"system": "KeyUp", "record_id": 7, "talkgroup": 100, "talkgroup_name": "Fire & Rescue", "source_id": 1001,
            "started_at": "2026-11-25T12:00:00Z", "duration_s": 3.2, "emergency": False,
            "talkers": [{"src": 1001, "alias": "E1 <lead>"}, {"src": 2002, "alias": "PW"}]}
    doc = xml.dom.minidom.parseString(Shipper.rs_metadata(meta))
    assert doc.documentElement.getAttribute("xmlns") == "urn:ietf:params:xml:ns:recording:1"
    assert len(doc.getElementsByTagName("participant")) == 2
    assert doc.getElementsByTagName("k:talkgroup")[0].getAttribute("name") == "Fire & Rescue"


def test_sip_helpers():
    t = SipTarget.parse("sip:srs@rec.example.net:5070;transport=tcp")
    assert (t.user, t.host, t.port, t.transport) == ("srs", "rec.example.net", 5070, "tcp")
    assert SipTarget.parse("10.0.0.5").port == 5060
    r = parse_message(b"SIP/2.0 401 Unauthorized\r\nWWW-Authenticate: Digest realm=\"x\", nonce=\"n1\", qop=\"auth\"\r\n"
                      b"i: abc\r\nl: 0\r\n\r\n")
    assert r.status == 401 and r.h("call-id") == "abc"
    h = digest_header(r.h("www-authenticate"), "INVITE", "sip:srs@x", "u", "p")
    assert 'username="u"' in h and "qop=auth" in h and "response=" in h
