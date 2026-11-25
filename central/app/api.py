"""REST API for the management web UI, plus the public enrollment endpoints."""

import csv
import datetime as dt
import io
import json
import os
import secrets
import threading
import time
from collections import Counter

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Response
from sqlalchemy import func
from sqlalchemy.orm import Session

from lmrcommon.modes import MODE_CODEC, MODE_LABELS, SystemMode

from . import schemas as S
from .config import settings
from .db import get_db
from .models import (BandPlanEntry, CallRecord, Channel, DispatchConsole, EdgeDevice, EnrollmentToken, PocDevice, SipRoute, Site, Subscriber, SystemSetting, TalkGroup, TalkgroupPatch, User, Zone, ZoneMember, now)
from .services import rules
from .services.zones import unit_view
from .security import current_user, hash_secret, issue_token, verify_secret
from .services.call_controller import get_system_settings
from .services.events import bus
from .services.wireguard import valid_public_key
from .state import state

router = APIRouter(prefix="/api")
_enroll_lock = threading.Lock()  # serialises tunnel IP allocation
admin = [Depends(current_user)]


async def changed():
    if state.ctl:
        await state.ctl.push_all_configs()
    if state.poc:
        await state.poc.refresh()
    if state.dispatch:
        await state.dispatch.refresh()


# ============================================================== auth
@router.post("/auth/login")
def login(body: S.Login, db: Session = Depends(get_db)):
    u = db.query(User).filter(User.username == body.username).first()
    if not u or not verify_secret(body.password, u.password_hash):
        time.sleep(0.5)
        raise HTTPException(401, "invalid credentials")
    return {"token": issue_token(db, u), "user": u.to_dict()}


@router.get("/auth/me", dependencies=admin)
def me(user: User = Depends(current_user)):
    return user.to_dict()


@router.post("/auth/password")
def change_password(body: dict, user: User = Depends(current_user), db: Session = Depends(get_db)):
    if not verify_secret(body.get("old", ""), user.password_hash) or len(body.get("new", "")) < 8:
        raise HTTPException(400, "old password wrong or new password shorter than 8 chars")
    u = db.get(User, user.id)
    u.password_hash = hash_secret(body["new"])
    db.commit()
    return {"ok": True}


# ============================================================== generic CRUD
def _get_or_404(db, model, pk):
    obj = db.get(model, pk)
    if not obj:
        raise HTTPException(404, f"{model.__name__} {pk} not found")
    return obj


def _jsonable(data: dict) -> dict:
    """dict values (e.g. a channel's packet_config) are stored as JSON text."""
    return {k: json.dumps(v) if isinstance(v, dict) else v for k, v in data.items()}


def crud(path: str, model, schema, order=None, pk_in_body=True):
    @router.get(f"/{path}", dependencies=admin, name=f"list_{path}")
    def list_(db: Session = Depends(get_db)):
        q = db.query(model)
        if order is not None:
            q = q.order_by(order)
        return [o.to_dict() for o in q.all()]

    @router.post(f"/{path}", dependencies=admin, name=f"create_{path}")
    async def create(body: schema, db: Session = Depends(get_db)):
        data = _jsonable(body.model_dump())
        if pk_in_body and db.get(model, data["id"]):
            raise HTTPException(409, f"id {data['id']} already exists")
        obj = model(**data)
        db.add(obj)
        db.commit()
        await changed()
        return obj.to_dict()

    @router.put(f"/{path}/{{pk}}", dependencies=admin, name=f"update_{path}")
    async def update(pk: int, body: schema, db: Session = Depends(get_db)):
        obj = _get_or_404(db, model, pk)
        for k, v in _jsonable(body.model_dump()).items():
            if k != "id":
                setattr(obj, k, v)
        db.commit()
        await changed()
        return obj.to_dict()

    @router.delete(f"/{path}/{{pk}}", dependencies=admin, name=f"delete_{path}")
    async def delete(pk: int, db: Session = Depends(get_db)):
        db.delete(_get_or_404(db, model, pk))
        db.commit()
        await changed()
        return {"ok": True}


crud("sites", Site, S.SiteIn, Site.id)
crud("talkgroups", TalkGroup, S.TalkGroupIn, TalkGroup.id)
crud("channels", Channel, S.ChannelIn, Channel.site_id, pk_in_body=False)
crud("sip/routes", SipRoute, S.SipRouteIn, SipRoute.id, pk_in_body=False)
crud("bandplan", BandPlanEntry, S.BandPlanIn, BandPlanEntry.id)
crud("patches", TalkgroupPatch, S.PatchIn, TalkgroupPatch.id, pk_in_body=False)


# subscribers need the talkgroup many-to-many
@router.get("/subscribers", dependencies=admin)
def list_subscribers(db: Session = Depends(get_db)):
    return [s.to_dict() for s in db.query(Subscriber).order_by(Subscriber.id).all()]


def _apply_sub(db, sub: Subscriber, body: S.SubscriberIn):
    data = body.model_dump()
    tg_ids = data.pop("talkgroup_ids")
    zone_ids = data.pop("zone_ids")
    for k, v in data.items():
        if k != "id" or sub.id is None:
            setattr(sub, k, v)
    sub.talkgroups = db.query(TalkGroup).filter(TalkGroup.id.in_(tg_ids)).all() if tg_ids else []
    sub.zones = db.query(Zone).filter(Zone.id.in_(zone_ids)).all() if zone_ids else []


@router.post("/subscribers", dependencies=admin)
async def create_subscriber(body: S.SubscriberIn, db: Session = Depends(get_db)):
    if db.get(Subscriber, body.id):
        raise HTTPException(409, f"radio id {body.id} already exists")
    sub = Subscriber()
    _apply_sub(db, sub, body)
    db.add(sub)
    db.commit()
    await changed()
    return sub.to_dict()


@router.put("/subscribers/{pk}", dependencies=admin)
async def update_subscriber(pk: int, body: S.SubscriberIn, db: Session = Depends(get_db)):
    sub = _get_or_404(db, Subscriber, pk)
    _apply_sub(db, sub, body)
    db.commit()
    await changed()
    return sub.to_dict()


@router.delete("/subscribers/{pk}", dependencies=admin)
async def delete_subscriber(pk: int, db: Session = Depends(get_db)):
    db.delete(_get_or_404(db, Subscriber, pk))
    db.commit()
    await changed()
    return {"ok": True}


@router.post("/subscribers/bulk", dependencies=admin)
async def bulk_subscribers(rows: list[S.SubscriberIn], db: Session = Depends(get_db)):
    n = 0
    for body in rows:
        sub = db.get(Subscriber, body.id) or Subscriber()
        if sub.id is None:
            db.add(sub)
        _apply_sub(db, sub, body)
        n += 1
    db.commit()
    await changed()
    return {"imported": n}


# ============================================================== reference data
@router.get("/modes", dependencies=admin)
def modes():
    return [{"id": m.value, "label": MODE_LABELS[m], "codec": MODE_CODEC[m].name} for m in SystemMode]


@router.get("/frequencies", dependencies=admin)
def frequency_plan(db: Session = Depends(get_db)):
    """System-wide frequency plan with simple conflict detection."""
    chans = db.query(Channel).order_by(Channel.tx_mhz).all()
    sites = {s.id: s.name for s in db.query(Site).all()}
    tx = Counter(round(c.tx_mhz, 5) for c in chans if c.tx_mhz)
    plan = [b.to_dict() for b in db.query(BandPlanEntry).all()]
    rows = []
    for c in chans:
        warn = []
        if c.interface != "radio":   # packet-voice link: no RF of ours to plan; show its link checks instead
            probs: list[str] = []
            rules.add_packet_checks(c.to_dict(), lambda _scope, m: probs.append(m), "")
            rows.append({**c.to_dict(), "site_name": sites.get(c.site_id, "?"),
                         "codec": MODE_CODEC[SystemMode(c.mode)].name, "warnings": probs,
                         "identity": None, "channel_no": None})
            continue
        if c.tx_mhz and tx[round(c.tx_mhz, 5)] > 1:
            warn.append("TX frequency reused at another site/slot (check spacing & simulcast intent)")
        if c.rx_mhz and c.tx_mhz and abs(c.rx_mhz - c.tx_mhz) < 0.0001 and c.mode != "ANALOG":
            warn.append("simplex: RX = TX")
        slot = rules.locate_channel(c.tx_mhz, c.rx_mhz, plan) if plan else rules.ChannelSlot(None, 0, [])
        rows.append({**c.to_dict(), "site_name": sites.get(c.site_id, "?"),
                     "codec": MODE_CODEC[SystemMode(c.mode)].name, "warnings": warn + slot.problems,
                     "identity": slot.identity, "channel_no": slot.number})
    return rows


# ============================================================== network view
def _checks(db) -> list[dict]:
    return rules.config_checks([x.to_dict() for x in db.query(Site).all()],
                               [x.to_dict() for x in db.query(Channel).all()],
                               [x.to_dict() for x in db.query(TalkGroup).all()],
                               [x.to_dict() for x in db.query(TalkgroupPatch).all()],
                               [x.to_dict() for x in db.query(BandPlanEntry).all()],
                               [x.to_dict() for x in db.query(EdgeDevice).all()])


@router.get("/network", dependencies=admin)
def network(db: Session = Depends(get_db)):
    """Live network view: connected sites, each channel's COR/PTT state, affiliations, config checks."""
    net = state.ctl.network_status()
    names = {c.id: c for c in db.query(Channel).all()}
    for site in net["sites"]:
        dev = db.query(EdgeDevice).filter(EdgeDevice.site_id == site["site_id"], EdgeDevice.connected.is_(True)).first()
        tel = json.loads(dev.telemetry or "{}") if dev else {}
        site["channels"] = [{**ch, "slot": names[ch["id"]].slot if ch.get("id") in names else None}
                            for ch in tel.get("channels", [])]
        site["telemetry"] = {k: tel.get(k) for k in ("cpu_temp_c", "load", "uptime_s", "version", "local_calls")}
    subs = {s.id: s.alias for s in db.query(Subscriber).all()}
    for a in net["affiliations"]:
        a["alias"] = subs.get(a["unit"], "")
    net["checks"] = _checks(db)
    return net


@router.get("/network/preview", dependencies=admin)
def network_preview(site_id: int, db: Session = Depends(get_db)):
    """The configuration KeyUp pushes to a site's edge device."""
    _get_or_404(db, Site, site_id)
    return state.ctl.build_config(db, site_id)


# ============================================================== settings
@router.get("/settings", dependencies=admin)
def get_settings(db: Session = Depends(get_db)):
    return get_system_settings(db)


@router.put("/settings", dependencies=admin)
async def put_settings(body: dict, db: Session = Depends(get_db)):
    for k, v in body.items():
        row = db.get(SystemSetting, k)
        if row:
            row.value = str(v)
        else:
            db.add(SystemSetting(key=k, value=str(v)))
    db.commit()
    await changed()
    return get_system_settings(db)


# ============================================================== edge devices & enrollment (admin)
@router.get("/devices", dependencies=admin)
def list_devices(db: Session = Depends(get_db)):
    peers = state.wg.peer_status() if state.wg else {}
    out = []
    for d in db.query(EdgeDevice).order_by(EdgeDevice.site_id).all():
        row = d.to_dict()
        row["telemetry"] = json.loads(d.telemetry or "{}")
        row["wg"] = peers.get(d.wg_public_key)
        row["site_name"] = d.site.name if d.site else "?"
        out.append(row)
    return out


@router.post("/devices/{pk}/approve", dependencies=admin)
async def approve_device(pk: int, db: Session = Depends(get_db)):
    d = _get_or_404(db, EdgeDevice, pk)
    replaced = []
    for other in db.query(EdgeDevice).filter(EdgeDevice.site_id == d.site_id, EdgeDevice.id != d.id,
                                             EdgeDevice.status == "active"):
        other.status = "revoked"  # one active edge per site: approving a replacement retires the old one
        state.wg.remove_peer(other.wg_public_key)
        replaced.append(other.id)
    d.status = "active"
    db.commit()
    sess = state.ctl.sessions.get(d.site_id)
    if sess and sess.device_id in replaced:
        await state.ctl.unregister_edge(sess, "replaced")
        await sess.ws.close()
    state.wg.add_peer(d.wg_public_key, d.wg_ip)
    bus.publish("device", device_id=d.id, status="active")
    return d.to_dict()


@router.post("/devices/{pk}/revoke", dependencies=admin)
async def revoke_device(pk: int, db: Session = Depends(get_db)):
    d = _get_or_404(db, EdgeDevice, pk)
    d.status = "revoked"
    db.commit()
    state.wg.remove_peer(d.wg_public_key)
    sess = state.ctl.sessions.get(d.site_id)
    if sess and sess.device_id == d.id:
        await state.ctl.unregister_edge(sess, "revoked")
        await sess.ws.close()
    bus.publish("device", device_id=d.id, status="revoked")
    return d.to_dict()


@router.delete("/devices/{pk}", dependencies=admin)
async def delete_device(pk: int, db: Session = Depends(get_db)):
    d = _get_or_404(db, EdgeDevice, pk)
    state.wg.remove_peer(d.wg_public_key)
    db.delete(d)
    db.commit()
    return {"ok": True}


@router.post("/devices/{pk}/command", dependencies=admin)
async def device_command(pk: int, body: dict, db: Session = Depends(get_db)):
    """Send a maintenance command: test_tone, restart_agent, announce_site_trunking_test."""
    d = _get_or_404(db, EdgeDevice, pk)
    sess = state.ctl.sessions.get(d.site_id)
    if not sess or sess.device_id != d.id:
        raise HTTPException(409, "device not connected")
    await sess.send({"type": "command", **body})
    return {"ok": True}


@router.get("/enrollment/tokens", dependencies=admin)
def list_tokens(db: Session = Depends(get_db)):
    return [t.to_dict() for t in db.query(EnrollmentToken).order_by(EnrollmentToken.created_at.desc()).all()]


@router.post("/enrollment/tokens", dependencies=admin)
def create_token(body: S.TokenIn, db: Session = Depends(get_db)):
    _get_or_404(db, Site, body.site_id)
    tok = EnrollmentToken(token=secrets.token_urlsafe(18), site_id=body.site_id, auto_approve=body.auto_approve,
                          expires_at=now() + dt.timedelta(hours=body.valid_hours))
    db.add(tok)
    db.commit()
    d = tok.to_dict()
    d["install_command"] = (f"curl -fsSL {settings.public_url}/edge/install.sh | sudo bash -s -- "
                            f"--server {settings.public_url} --token {tok.token}")
    return d


@router.delete("/enrollment/tokens/{token}", dependencies=admin)
def delete_token(token: str, db: Session = Depends(get_db)):
    t = db.get(EnrollmentToken, token)
    if t:
        db.delete(t)
        db.commit()
    return {"ok": True}


# ============================================================== enrollment (public, called by edge)
def _wg_payload(d: EdgeDevice) -> dict:
    return {**state.wg.client_config(d.wg_ip), "control_url": f"http://{state.wg.server_ip}:{settings.http_port}",
            "voice_host": state.wg.server_ip, "voice_port": settings.voice_port}


@router.post("/enroll")
def enroll(body: S.EnrollIn, db: Session = Depends(get_db)):
    with _enroll_lock:
        return _enroll(body, db)


def _enroll(body: S.EnrollIn, db: Session):
    tok = db.get(EnrollmentToken, body.token)
    if not tok or tok.used_at or tok.expires_at < now():
        time.sleep(1)
        raise HTTPException(403, "invalid, used or expired enrollment token")
    if not valid_public_key(body.wg_public_key):
        raise HTTPException(400, "invalid WireGuard public key")
    if db.query(EdgeDevice).filter(EdgeDevice.wg_public_key == body.wg_public_key).first():
        raise HTTPException(409, "public key already enrolled")
    used = {d.wg_ip for d in db.query(EdgeDevice).all()} | {c.wg_ip for c in db.query(DispatchConsole).all() if c.wg_ip}
    secret = secrets.token_urlsafe(32)
    dev = EdgeDevice(site_id=tok.site_id, hostname=body.hostname, serial=body.serial, model=body.model,
                     agent_version=body.agent_version, wg_public_key=body.wg_public_key,
                     wg_ip=state.wg.allocate_ip(used), secret_hash=hash_secret(secret),
                     status="active" if tok.auto_approve else "pending")
    db.add(dev)
    db.flush()
    tok.used_at, tok.used_by_device = now(), dev.id
    if dev.status == "active":
        for other in db.query(EdgeDevice).filter(EdgeDevice.site_id == dev.site_id, EdgeDevice.id != dev.id,
                                                 EdgeDevice.status == "active"):
            other.status = "revoked"
            state.wg.remove_peer(other.wg_public_key)
        state.wg.add_peer(dev.wg_public_key, dev.wg_ip)
    db.commit()
    bus.publish("device", device_id=dev.id, status=dev.status, site_id=dev.site_id)
    resp = {"device_id": dev.id, "device_secret": secret, "site_id": dev.site_id, "status": dev.status}
    if dev.status == "active":
        resp["wireguard"] = _wg_payload(dev)
    return resp


@router.post("/enroll/status")
def enroll_status(body: dict, db: Session = Depends(get_db)):
    dev = db.get(EdgeDevice, int(body.get("device_id", 0)))
    if not dev or not verify_secret(body.get("device_secret", ""), dev.secret_hash):
        raise HTTPException(403, "unknown device")
    resp = {"device_id": dev.id, "site_id": dev.site_id, "status": dev.status}
    if dev.status == "active":
        resp["wireguard"] = _wg_payload(dev)
    return resp


# ============================================================== calls & dashboard
def _calls_query(db, tg=None, site=None, unit=None, since=None, until=None, recorded=False, result=None):
    q = db.query(CallRecord)
    if tg:
        q = q.filter(CallRecord.talkgroup_id == tg)
    if site:
        q = q.filter(CallRecord.source_site_id == site)
    if unit:
        q = q.filter((CallRecord.source_id == unit) | CallRecord.talkers.like(f'%"src": {int(unit)},%'))
    if since:
        q = q.filter(CallRecord.started_at >= since)
    if until:
        q = q.filter(CallRecord.started_at < until)
    if recorded:
        q = q.filter(CallRecord.recording != "")
    if result == "granted":
        q = q.filter(CallRecord.result.in_(["granted", "preempted"]))
    elif result == "denied":
        q = q.filter(CallRecord.result.like("denied:%"))
    return q


@router.get("/calls", dependencies=admin)
def list_calls(limit: int = Query(100, le=1000), tg: int | None = None, site: int | None = None,
               unit: int | None = None, since: dt.datetime | None = None, until: dt.datetime | None = None,
               recorded: bool = False, result: str | None = None, db: Session = Depends(get_db)):
    q = _calls_query(db, tg, site, unit, since, until, recorded, result)
    return [c.to_dict() for c in q.order_by(CallRecord.started_at.desc()).limit(limit)]


@router.get("/calls/export.csv", dependencies=admin)
def export_calls(tg: int | None = None, site: int | None = None, unit: int | None = None,
                 since: dt.datetime | None = None, until: dt.datetime | None = None, recorded: bool = False,
                 result: str | None = None, db: Session = Depends(get_db)):
    """Talkgroup activity log as CSV (one row per call, talkers listed)."""
    names = {t.id: t.name for t in db.query(TalkGroup).all()}
    aliases = {s.id: s.alias for s in db.query(Subscriber).all()}
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["record", "started_utc", "ended_utc", "duration_s", "talkgroup", "talkgroup_name", "source_id",
                "source_alias", "source_site", "result", "emergency", "talkers", "recording", "ship_status"])
    q = _calls_query(db, tg, site, unit, since, until, recorded, result).order_by(CallRecord.started_at)
    for c in q.limit(100000):
        talkers = "; ".join(f"{t.get('alias') or t['src']} ({t['src']}) +{t['at']}s {t['dur']}s"
                            for t in json.loads(c.talkers or "[]"))
        w.writerow([c.id, c.started_at, c.ended_at or "", c.duration_s, c.talkgroup_id, names.get(c.talkgroup_id, ""),
                    c.source_id, aliases.get(c.source_id, ""), c.source_site_id, c.result,
                    "yes" if c.emergency else "", talkers, c.recording, c.ship_status])
    return Response(buf.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": 'attachment; filename="keyup-call-log.csv"'})


@router.get("/calls/{pk}/audio", dependencies=admin)
def call_audio(pk: int, db: Session = Depends(get_db)):
    """The call recording as a 16-bit PCM WAV (mu-law recordings) or the raw voice-frame file."""
    from .services.recorder import Recorder
    rec = _get_or_404(db, CallRecord, pk)
    path = Recorder.path_of(rec)
    if not path:
        raise HTTPException(404, rec.recording_note or "no recording for this call")
    if path.endswith(".ul"):
        return Response(Recorder.wav_bytes(path), media_type="audio/wav",
                        headers={"Content-Disposition": f'inline; filename="call-{pk}-tg{rec.talkgroup_id}.wav"'})
    with open(path, "rb") as f:
        return Response(f.read(), media_type="application/octet-stream",
                        headers={"Content-Disposition": f'attachment; filename="{os.path.basename(path)}"'})


@router.post("/calls/{pk}/ship", dependencies=admin)
def call_reship(pk: int, db: Session = Depends(get_db)):
    rec = _get_or_404(db, CallRecord, pk)
    if not rec.recording:
        raise HTTPException(409, "this call has no recording")
    rec.ship_status = "queued"
    db.commit()
    if state.recorder and state.recorder.shipper:
        state.recorder.shipper.next_try.pop(pk, None)
        state.recorder.shipper.wake()
    return {"ok": True}


@router.get("/recording/status", dependencies=admin)
def recording_status():
    if not state.recorder:
        return {"enabled": False}
    return {**state.recorder.usage(), "shipping": state.recorder.shipper.status() if state.recorder.shipper else {}}


@router.post("/sip/originate", dependencies=admin)
async def sip_originate(body: S.OriginateIn):
    if not state.sip:
        raise HTTPException(409, "SIP gateway disabled (ARI_ENABLED=false)")
    return await state.sip.originate(body.endpoint, body.talkgroup_id, body.ptt_mode, caller_id=body.caller_id)


@router.get("/dashboard", dependencies=admin)
def dashboard(db: Session = Depends(get_db)):
    since = now() - dt.timedelta(hours=24)
    calls_24h = db.query(CallRecord).filter(CallRecord.started_at >= since)
    granted = calls_24h.filter(CallRecord.result == "granted")
    airtime = db.query(func.coalesce(func.sum(CallRecord.duration_s), 0)).filter(
        CallRecord.started_at >= since).scalar()
    top_tgs = db.query(CallRecord.talkgroup_id, func.count()).filter(CallRecord.started_at >= since) \
        .group_by(CallRecord.talkgroup_id).order_by(func.count().desc()).limit(5).all()
    tg_names = {t.id: t.name for t in db.query(TalkGroup).all()}
    cur = now().replace(minute=0, second=0, microsecond=0)
    buckets = [cur - dt.timedelta(hours=h) for h in range(23, -1, -1)]
    hourly = {b.strftime("%H:00Z"): 0 for b in buckets}
    for (ts,) in db.query(CallRecord.started_at).filter(CallRecord.started_at >= buckets[0]):
        k = ts.strftime("%H:00Z")
        if k in hourly:
            hourly[k] += 1
    sites = []
    for s in db.query(Site).order_by(Site.id).all():
        sess = state.ctl.sessions.get(s.id)
        sites.append({**s.to_dict(), "channels": len(s.channels),
                      "mode_mix": sorted({c.mode for c in s.channels}),
                      "codec": sess.codec.name if sess else None,
                      "edge_mode": sess.mode if sess else None})
    return {
        "system": get_system_settings(db),
        "uptime_s": round(time.time() - state.started),
        "counts": {
            "sites": db.query(Site).count(), "talkgroups": db.query(TalkGroup).count(),
            "subscribers": db.query(Subscriber).count(), "channels": db.query(Channel).count(),
            "devices_pending": db.query(EdgeDevice).filter(EdgeDevice.status == "pending").count(),
        },
        "calls_24h": calls_24h.count(), "granted_24h": granted.count(),
        "denied_24h": calls_24h.filter(CallRecord.result.like("denied%")).count(),
        "site_trunking_calls_24h": calls_24h.filter(CallRecord.site_trunking.is_(True)).count(),
        "airtime_24h_s": round(airtime or 0, 1),
        "top_talkgroups": [{"tg": t, "name": tg_names.get(t, str(t)), "calls": n} for t, n in top_tgs],
        "hourly": hourly,
        "sites": sites,
        "call_processing": state.ctl.status(),
        "transcoder": state.transcoder.status(),
        "sip": state.sip.status() if state.sip else {"enabled": False},
        "wireguard": {"enabled": state.wg.enabled, "interface": state.wg.interface,
                      "server_ip": state.wg.server_ip, "endpoint": state.wg.endpoint,
                      "public_key": state.wg.public_key, "port": state.wg.port},
    }


# ============================================================== zones
def _apply_zone(db, z: Zone, body: S.ZoneIn) -> None:
    data = body.model_dump()
    ids = list(dict.fromkeys(data.pop("talkgroup_ids")))
    known = {t.id for t in db.query(TalkGroup).filter(TalkGroup.id.in_(ids)).all()} if ids else set()
    missing = [i for i in ids if i not in known]
    if missing:
        raise HTTPException(400, f"unknown talkgroup(s): {', '.join(map(str, missing))}")
    for k, v in data.items():
        setattr(z, k, v)
    z.members = [ZoneMember(talkgroup_id=t, position=i) for i, t in enumerate(ids)]


@router.get("/zones", dependencies=admin)
def list_zones(db: Session = Depends(get_db)):
    return [z.to_dict() for z in db.query(Zone).order_by(Zone.position, Zone.id).all()]


@router.post("/zones", dependencies=admin)
async def create_zone(body: S.ZoneIn, db: Session = Depends(get_db)):
    z = Zone()
    _apply_zone(db, z, body)
    db.add(z)
    db.commit()
    await changed()
    return z.to_dict()


@router.put("/zones/{pk}", dependencies=admin)
async def update_zone(pk: int, body: S.ZoneIn, db: Session = Depends(get_db)):
    z = _get_or_404(db, Zone, pk)
    z.members = []
    db.flush()
    _apply_zone(db, z, body)
    db.commit()
    await changed()
    return z.to_dict()


@router.delete("/zones/{pk}", dependencies=admin)
async def delete_zone(pk: int, db: Session = Depends(get_db)):
    db.delete(_get_or_404(db, Zone, pk))
    db.commit()
    await changed()
    return {"ok": True}


@router.get("/subscribers/{pk}/view", dependencies=admin)
def subscriber_view(pk: int, poc: bool = False, db: Session = Depends(get_db)):
    """The zones and talkgroups this radio ID gets (what a phone or console bound to it shows)."""
    return unit_view(db, _get_or_404(db, Subscriber, pk), poc=poc)


@router.get("/zones/export.csv", dependencies=admin)
def export_zones(subscriber: int | None = None, db: Session = Depends(get_db)):
    """Channel plan for programming radios: zone, position, channel name, talkgroup, priority."""
    tgs = {t.id: t for t in db.query(TalkGroup).all()}
    if subscriber:
        view = unit_view(db, _get_or_404(db, Subscriber, subscriber))
        zones = [(z["name"], z["talkgroups"]) for z in view["zones"]]
    else:
        zones = [(z.name, [m.talkgroup_id for m in z.members]) for z in db.query(Zone).order_by(Zone.position, Zone.id)]
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["zone", "position", "channel_name", "talkgroup_id", "priority", "emergency"])
    for name, ids in zones:
        for i, t in enumerate(ids, 1):
            tg = tgs.get(t)
            if tg:
                w.writerow([name, i, tg.name[:16], tg.id, tg.priority, "yes" if tg.emergency_enabled else "no"])
    fname = f"keyup-zones{'-' + str(subscriber) if subscriber else ''}.csv"
    return Response(buf.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="{fname}"'})


# ============================================================== PoC provisioning
_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


@router.post("/subscribers/{pk}/poc/activation", dependencies=admin)
async def poc_activation(pk: int, db: Session = Depends(get_db)):
    """Enable PoC for a radio ID and issue a one-time activation code (+ QR) for the phone app."""
    import segno
    sub = _get_or_404(db, Subscriber, pk)
    code = "".join(secrets.choice(_CODE_ALPHABET) for _ in range(8))
    sub.poc_enabled = True
    sub.poc_activation_hash = hash_secret(code)
    sub.poc_activation_expires = now() + dt.timedelta(hours=24)
    db.commit()
    uri = f"keyup://activate?s={settings.public_url}&u={sub.id}&c={code}"
    await changed()
    return {"unit": sub.id, "alias": sub.alias, "code": code, "server": settings.public_url, "uri": uri,
            "expires_at": sub.poc_activation_expires.isoformat() + "Z",
            "qr_svg": segno.make(uri, error="m").svg_inline(scale=5, dark="#000", light="#fff")}


@router.post("/poc/activate")
def poc_activate(body: S.PocActivateIn, db: Session = Depends(get_db)):
    """Called by the phone app: trade a one-time activation code for a device token."""
    from .services.poc import new_token, token_hash
    sub = db.get(Subscriber, body.unit)
    code = body.code.strip().upper().replace("-", "").replace(" ", "")
    if (not sub or not sub.poc_activation_hash or not sub.poc_activation_expires
            or sub.poc_activation_expires < now() or not verify_secret(code, sub.poc_activation_hash)):
        time.sleep(1)
        raise HTTPException(403, "invalid or expired activation code")
    if not sub.enabled:
        raise HTTPException(403, "this radio is inhibited")
    token = new_token()
    sub.poc_activation_hash, sub.poc_activation_expires = "", None
    db.add(PocDevice(subscriber_id=sub.id, token_hash=token_hash(token), name=body.device_name[:64],
                     platform=body.platform[:16], app_version=body.app_version[:16]))
    db.commit()
    bus.publish("poc", action="activated", unit=sub.id, alias=sub.alias, platform=body.platform)
    return {"token": token, "unit": sub.id, "alias": sub.alias, "ws_path": "/poc/ws"}


@router.get("/poc/devices", dependencies=admin)
def poc_devices(db: Session = Depends(get_db)):
    online = {s["device_id"] for s in (state.poc.status()["sessions"] if state.poc else [])}
    return [{**d.to_dict(), "online": d.id in online} for d in db.query(PocDevice).order_by(PocDevice.subscriber_id)]


@router.post("/poc/devices/{pk}/revoke", dependencies=admin)
async def poc_revoke(pk: int, db: Session = Depends(get_db)):
    d = _get_or_404(db, PocDevice, pk)
    d.revoked = True
    db.commit()
    if state.poc:
        await state.poc.refresh()
    return {"ok": True}


@router.get("/poc/status", dependencies=admin)
def poc_status():
    return state.poc.status() if state.poc else {"sessions": [], "emergencies": {}}


# ============================================================== dispatch consoles
def _apply_console(db, con: DispatchConsole, body: S.ConsoleIn) -> None:
    if not db.get(Subscriber, body.subscriber_id):
        raise HTTPException(400, f"radio id {body.subscriber_id} is not a subscriber; add it under Subscriber Radios first")
    data = body.model_dump()
    zone_ids = data.pop("zone_ids")
    for k, v in data.items():
        setattr(con, k, v)
    con.zones = db.query(Zone).filter(Zone.id.in_(zone_ids)).all() if zone_ids else []


@router.get("/consoles", dependencies=admin)
def list_consoles(db: Session = Depends(get_db)):
    online = {s["console"]: s for s in (state.dispatch.status()["sessions"] if state.dispatch else [])}
    wg = state.wg.peer_status() if state.wg else {}
    out = []
    for c in db.query(DispatchConsole).order_by(DispatchConsole.id).all():
        d = c.to_dict()
        sub = db.get(Subscriber, c.subscriber_id)
        d["subscriber_alias"] = sub.alias if sub else "?"
        d["session"] = online.get(c.id)
        d["wg"] = wg.get(c.wg_public_key) if c.wg_public_key else None
        d["talkgroups"] = [t["id"] for t in unit_view(db, sub, zones=list(c.zones))["talkgroups"]] if sub else []
        out.append(d)
    return out


@router.post("/consoles", dependencies=admin)
async def create_console(body: S.ConsoleIn, db: Session = Depends(get_db)):
    con = DispatchConsole()
    _apply_console(db, con, body)
    db.add(con)
    db.commit()
    await changed()
    return con.to_dict()


@router.put("/consoles/{pk}", dependencies=admin)
async def update_console(pk: int, body: S.ConsoleIn, db: Session = Depends(get_db)):
    con = _get_or_404(db, DispatchConsole, pk)
    _apply_console(db, con, body)
    db.commit()
    await changed()
    return con.to_dict()


@router.delete("/consoles/{pk}", dependencies=admin)
async def delete_console(pk: int, db: Session = Depends(get_db)):
    con = _get_or_404(db, DispatchConsole, pk)
    if con.wg_public_key and state.wg:
        state.wg.remove_peer(con.wg_public_key)
    db.delete(con)
    db.commit()
    await changed()
    return {"ok": True}


@router.post("/consoles/{pk}/activation", dependencies=admin)
def console_activation(pk: int, db: Session = Depends(get_db)):
    """One-time code the console trades for its login token and its WireGuard tunnel."""
    con = _get_or_404(db, DispatchConsole, pk)
    part = "".join(secrets.choice(_CODE_ALPHABET) for _ in range(8))
    con.activation_hash = hash_secret(part)
    con.activation_expires = now() + dt.timedelta(hours=24)
    db.commit()
    return {"console_id": con.id, "name": con.name, "server": settings.public_url,
            "code": f"{con.id}-{part[:4]}-{part[4:]}", "expires_at": con.activation_expires.isoformat() + "Z"}


@router.post("/consoles/{pk}/revoke", dependencies=admin)
async def console_revoke(pk: int, db: Session = Depends(get_db)):
    con = _get_or_404(db, DispatchConsole, pk)
    if con.wg_public_key and state.wg:
        state.wg.remove_peer(con.wg_public_key)
    con.token_hash = con.wg_public_key = con.activation_hash = ""
    db.commit()
    await changed()
    bus.publish("console", action="revoked", console=con.id, name=con.name)
    return con.to_dict()


@router.post("/console/activate")
async def console_activate(body: S.ActivateIn, db: Session = Depends(get_db)):
    """Called by the console: trade the code for a token, the tunnel (keys + addresses) and the server URLs."""
    from .services.poc import new_token, token_hash
    from .services.wireguard import generate_keypair
    head, _, rest = body.code.strip().upper().replace(" ", "").partition("-")
    con = db.get(DispatchConsole, int(head)) if head.isdigit() else None
    if (not con or not con.activation_hash or not con.activation_expires or con.activation_expires < now()
            or not verify_secret(rest.replace("-", ""), con.activation_hash)):
        time.sleep(1)
        raise HTTPException(403, "invalid or expired activation code")
    with _enroll_lock:
        if con.wg_public_key and state.wg:
            state.wg.remove_peer(con.wg_public_key)
        priv, pub = generate_keypair()
        if not con.wg_ip:
            used = {d.wg_ip for d in db.query(EdgeDevice).all()} | {
                c.wg_ip for c in db.query(DispatchConsole).all() if c.wg_ip}
            con.wg_ip = state.wg.allocate_ip(used)
        con.wg_public_key = pub
        token = new_token()
        con.token_hash = token_hash(token)
        con.activation_hash, con.activation_expires = "", None
        con.activated_at = now()
        db.commit()
        state.wg.add_peer(pub, con.wg_ip)
        wg = state.wg.client_config(con.wg_ip)
    await changed()
    bus.publish("console", action="activated", console=con.id, name=con.name, machine=body.name[:64])
    public = settings.public_url.rstrip("/")
    return {"token": token, "console_id": con.id, "name": con.name, "server": public,
            "urls": {"tunnel": f"ws://{wg['server_ip']}:{settings.http_port}/console/ws",
                     "public": public.replace("https://", "wss://").replace("http://", "ws://") + "/console/ws"},
            "wireguard": {"private_key": priv, "address": wg["address"], "server_public_key": wg["server_public_key"],
                          "endpoint": wg["endpoint"], "allowed_ips": wg["allowed_ips"],
                          "persistent_keepalive": wg["persistent_keepalive"]}}


@router.get("/consoles/status", dependencies=admin)
def consoles_status():
    return state.dispatch.status() if state.dispatch else {"sessions": []}
