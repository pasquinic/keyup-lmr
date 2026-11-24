"""REST API for the management web UI, plus the public enrollment endpoints."""

import datetime as dt
import json
import secrets
import threading
import time
from collections import Counter

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func
from sqlalchemy.orm import Session

from lmrcommon.modes import MODE_CODEC, MODE_LABELS, SystemMode

from . import schemas as S
from .config import settings
from .db import get_db
from .models import (CallRecord, Channel, EdgeDevice, EnrollmentToken, IdenEntry, Patch, PocDevice, SipRoute,
                     Site, Subscriber, SystemSetting, TalkGroup, User, now)
from .services import dvm, rules
from .security import current_user, hash_secret, issue_token, verify_secret
from .services.call_controller import get_system_settings
from .services.events import bus
from .services.wireguard import valid_public_key
from .state import state

router = APIRouter(prefix="/api")
_enroll_lock = threading.Lock()  # serialises tunnel IP allocation
admin = [Depends(current_user)]


async def changed():
    if state.fne is not None:
        try:
            await dvm.apply(state.fne)
        except Exception as e:  # never block a UI save on config generation
            import logging
            logging.getLogger("api").exception("DVM apply failed: %s", e)
    if state.ctl:
        await state.ctl.push_all_configs()
    if state.ctl and state.ctl.hub:
        state.ctl.hub.refresh()
    if state.poc:
        await state.poc.refresh()


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


def _fixup(db, obj) -> None:
    """Model-specific defaults/validation shared by create and update."""
    if isinstance(obj, Channel):
        if not obj.peer_id:
            obj.peer_id = dvm.default_peer_id(obj.site_id, obj.slot)
        clash = db.query(Channel).filter(Channel.peer_id == obj.peer_id, Channel.id != obj.id).first()
        if clash:
            raise HTTPException(409, f"peer id {obj.peer_id} already used by channel {clash.name}")


def crud(path: str, model, schema, order=None, pk_in_body=True):
    @router.get(f"/{path}", dependencies=admin, name=f"list_{path}")
    def list_(db: Session = Depends(get_db)):
        q = db.query(model)
        if order is not None:
            q = q.order_by(order)
        return [o.to_dict() for o in q.all()]

    @router.post(f"/{path}", dependencies=admin, name=f"create_{path}")
    async def create(body: schema, db: Session = Depends(get_db)):
        data = body.model_dump()
        if pk_in_body and db.get(model, data["id"]):
            raise HTTPException(409, f"id {data['id']} already exists")
        obj = model(**data)
        _fixup(db, obj)
        db.add(obj)
        db.commit()
        await changed()
        return obj.to_dict()

    @router.put(f"/{path}/{{pk}}", dependencies=admin, name=f"update_{path}")
    async def update(pk: int, body: schema, db: Session = Depends(get_db)):
        obj = _get_or_404(db, model, pk)
        for k, v in body.model_dump().items():
            if k != "id":
                setattr(obj, k, v)
        _fixup(db, obj)
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
crud("idens", IdenEntry, S.IdenIn, IdenEntry.id)
crud("patches", Patch, S.PatchIn, Patch.id, pk_in_body=False)


# subscribers need the talkgroup many-to-many
@router.get("/subscribers", dependencies=admin)
def list_subscribers(db: Session = Depends(get_db)):
    return [s.to_dict() for s in db.query(Subscriber).order_by(Subscriber.id).all()]


def _apply_sub(db, sub: Subscriber, body: S.SubscriberIn):
    data = body.model_dump()
    tg_ids = data.pop("talkgroup_ids")
    for k, v in data.items():
        if k != "id" or sub.id is None:
            setattr(sub, k, v)
    sub.talkgroups = db.query(TalkGroup).filter(TalkGroup.id.in_(tg_ids)).all() if tg_ids else []


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
    idens = [i.to_dict() for i in db.query(IdenEntry).order_by(IdenEntry.id).all()]
    tx = Counter(round(c.tx_mhz, 5) for c in chans if c.tx_mhz)
    rows = []
    for c in chans:
        warn = []
        if c.tx_mhz and tx[round(c.tx_mhz, 5)] > 1:
            warn.append("TX frequency reused at another site/slot (check spacing & simulcast intent)")
        if c.rx_mhz and c.tx_mhz and abs(c.rx_mhz - c.tx_mhz) < 0.0001 and c.mode != "ANALOG":
            warn.append("simplex: RX = TX")
        slot = rules.locate_channel(c.tx_mhz, c.rx_mhz, idens) if idens else rules.ChannelSlot(None, 0, [])
        if c.mode == "P25P2" and _backend(db) == "dvm":
            warn.append("P25 Phase II is not supported by DVM")
        rows.append({**c.to_dict(), "site_name": sites.get(c.site_id, "?"),
                     "codec": MODE_CODEC[SystemMode(c.mode)].name, "warnings": warn + slot.problems,
                     "identity": slot.identity, "channel_no": slot.number,
                     "iden": slot.identity, "ch_no": slot.number})
    return rows


# ============================================================== network view
def _checks(db) -> list[dict]:
    return rules.config_checks([x.to_dict() for x in db.query(Site).all()],
                               [x.to_dict() for x in db.query(Channel).all()],
                               [x.to_dict() for x in db.query(TalkGroup).all()],
                               [x.to_dict() for x in db.query(Patch).all()],
                               [x.to_dict() for x in db.query(IdenEntry).all()],
                               [x.to_dict() for x in db.query(EdgeDevice).all()], _backend(db))


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


def _backend(db) -> str:
    return get_system_settings(db).get("call_backend", "dvm")


# ============================================================== DVM
@router.get("/dvm/status", dependencies=admin)
def dvm_status(db: Session = Depends(get_db)):
    plan = dvm.DvmPlan(db)
    central = plan.central_instances() if plan.ack else {}
    for sid in plan.sites:
        plan.site_instances(sid)  # collects warnings
    return {"backend": _backend(db), "license_acknowledged": plan.ack, "dvm_ref": dvm.DVM_REF,
            "fne": state.fne.status() if state.fne else None,
            "central_instances": sorted(central), "warnings": plan.warnings}


@router.get("/dvm/preview", dependencies=admin)
def dvm_preview(site_id: int | None = None, db: Session = Depends(get_db)):
    """Show the generated DVM files (FNE when site_id is omitted)."""
    plan = dvm.DvmPlan(db)
    inst = plan.site_instances(site_id) if site_id else plan.central_instances()
    return {name: {"binary": i["binary"], "args": i["args"], "files": i["files"]} for name, i in inst.items()}


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
    used = {d.wg_ip for d in db.query(EdgeDevice).all()}
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
    return state.poc.status() if state.poc else {"sessions": [], "emergencies": {}, "disabled": True}


# ============================================================== calls & dashboard
@router.get("/calls", dependencies=admin)
def list_calls(limit: int = Query(100, le=1000), tg: int | None = None, site: int | None = None,
               db: Session = Depends(get_db)):
    q = db.query(CallRecord)
    if tg:
        q = q.filter(CallRecord.talkgroup_id == tg)
    if site:
        q = q.filter(CallRecord.source_site_id == site)
    return [c.to_dict() for c in q.order_by(CallRecord.started_at.desc()).limit(limit)]


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
                      "peers_up": sum(1 for p in (state.fne.peers.values() if state.fne else []) if p.get("site_id") == s.id),
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
        "dvm": state.fne.status() if state.fne else None,
        "poc": state.poc.status() if state.poc else None,
        "transcoder": state.transcoder.status(),
        "sip": state.sip.status() if state.sip else {"enabled": False},
        "wireguard": {"enabled": state.wg.enabled, "interface": state.wg.interface,
                      "server_ip": state.wg.server_ip, "endpoint": state.wg.endpoint,
                      "public_key": state.wg.public_key, "port": state.wg.port},
    }
