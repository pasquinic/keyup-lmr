"""KeyUp (LMR Trunking System) -- central management & call processing server."""
from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import tarfile
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles

from lmrcommon.protocol import Msg
from lmrcommon.vocoder import VocoderPool

from .api import router
from .config import settings
from .db import Base, engine, session_scope, upgrade_schema
from .models import EdgeDevice, Site, SystemSetting, User, DEFAULT_SYSTEM_SETTINGS
from .security import hash_secret, user_for_token, verify_secret
from .services.call_controller import CallController, EdgeSession, VoiceProtocol
from .services.events import bus
from .services.transcoder import TranscoderService
from .services.wireguard import WireGuardManager
from .state import state

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"),
                    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
log = logging.getLogger("central")
HERE = os.path.dirname(__file__)
EDGE_SRC = os.getenv("EDGE_SRC_DIR", os.path.abspath(os.path.join(HERE, "..", "..")))


def seed() -> None:
    Base.metadata.create_all(engine)
    upgrade_schema(log)
    with session_scope() as db:
        if not db.query(User).count():
            db.add(User(username=settings.admin_user, password_hash=hash_secret(settings.admin_password)))
            log.warning("created admin user '%s' -- change the password", settings.admin_user)
        for k, v in DEFAULT_SYSTEM_SETTINGS.items():
            if not db.get(SystemSetting, k):
                if k == "call_backend" and os.getenv("KEYUP_DEFAULT_BACKEND"):
                    v = os.getenv("KEYUP_DEFAULT_BACKEND")
                db.add(SystemSetting(key=k, value=v))
        for s in db.query(Site).all():
            s.status = "offline"
        for d in db.query(EdgeDevice).all():
            d.connected = False


@asynccontextmanager
async def lifespan(app: FastAPI):
    seed()
    state.started = time.time()
    state.wg = WireGuardManager(settings.wg_interface, settings.wg_subnet, settings.wg_port,
                                settings.wg_endpoint, settings.data_dir, settings.wg_enabled)
    with session_scope() as db:
        peers = [(d.wg_public_key, d.wg_ip) for d in db.query(EdgeDevice).filter(EdgeDevice.status == "active")]
    try:
        state.wg.ensure_up(peers)
    except Exception as e:
        log.error("WireGuard setup failed: %s", e)
    pool = VocoderPool(settings.dvsi_ports, settings.dvsi_baud)
    state.transcoder = TranscoderService(pool)
    state.ctl = CallController(state.transcoder)
    state.ctl.refresh_names()
    loop = asyncio.get_running_loop()
    transport, _ = await loop.create_datagram_endpoint(lambda: VoiceProtocol(state.ctl),
                                                       local_addr=(settings.voice_bind, settings.voice_port))
    tasks = [asyncio.create_task(state.ctl.watchdog())]
    with session_scope() as db:
        from .services.call_controller import get_system_settings
        backend = get_system_settings(db).get("call_backend", "dvm")
    if backend == "dvm":
        from .services import dvm
        state.fne = dvm.FneMonitor(state.ctl)
        state.ctl.fne = state.fne
        try:
            res = await dvm.apply(state.fne)
            if not res["license_acknowledged"]:
                log.warning("DVM license not acknowledged yet: FNE not started (System Settings)")
        except Exception:
            log.exception("DVM config generation failed")
        tasks.append(asyncio.create_task(state.fne.run()))
        from .services.poc import PocGateway
        from .services.tg_audio import TgAudioHub
        state.ctl.hub = TgAudioHub(state.fne)
        state.ctl.hub.refresh()
        state.ctl.poc = state.poc = PocGateway(state.ctl)
        state.fne.poc = state.poc
        tasks.append(asyncio.create_task(state.poc.run()))
    if settings.ari_enabled:
        from .services.sip_gateway import SipGateway
        state.sip = SipGateway(state.ctl)
        state.ctl.sip_gateway = state.sip
        tasks.append(asyncio.create_task(state.sip.run()))
    log.info("central up: backend %s, http :%d, voice udp :%d, wg %s (%s), %d DVSI vocoders", backend,
             settings.http_port, settings.voice_port, state.wg.server_ip,
             "kernel" if state.wg.enabled else "mock", pool.hardware_count)
    yield
    for t in tasks:
        t.cancel()
    transport.close()


app = FastAPI(title="KeyUp — LMR Trunking Central", version="0.1.0", lifespan=lifespan)
app.include_router(router)
app.mount("/static", StaticFiles(directory=os.path.join(HERE, "static")), name="static")


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(os.path.join(HERE, "static", "index.html"))


@app.get("/healthz", include_in_schema=False)
def healthz():
    return {"ok": True, "sites": len(state.ctl.sessions) if state.ctl else 0}


# ------------------------------------------------------------ edge bootstrap downloads
@app.get("/edge/install.sh", include_in_schema=False)
def edge_install():
    return PlainTextResponse(open(os.path.join(EDGE_SRC, "edge", "install.sh")).read())


_agent_tgz: bytes | None = None


@app.get("/edge/agent.tar.gz", include_in_schema=False)
def edge_agent():
    global _agent_tgz
    if _agent_tgz is None:
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            for sub in ("lmrcommon", "edge"):
                tar.add(os.path.join(EDGE_SRC, sub), arcname=sub,
                        filter=lambda ti: None if "__pycache__" in ti.name else ti)
        _agent_tgz = buf.getvalue()
    return Response(_agent_tgz, media_type="application/gzip")


_dvm_tgz: bytes | None = None


@app.get("/edge/dvm-src.tar.gz", include_in_schema=False)
def edge_dvm_source():
    """Source of the KeyUp DVM fork (GPL-2.0) for edges to build dvmhost / dvmbridge."""
    global _dvm_tgz
    if _dvm_tgz is None:
        root = os.path.join(EDGE_SRC, "dvm", "dvmhost")
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            tar.add(root, arcname="dvmhost", filter=lambda ti: None if ti.name == "dvmhost/build" or ti.name.startswith("dvmhost/build/") else ti)
            fork = os.path.join(EDGE_SRC, "dvm", "FORK.md")
            if os.path.exists(fork):
                tar.add(fork, arcname="FORK.md")
        _dvm_tgz = buf.getvalue()
    return Response(_dvm_tgz, media_type="application/gzip")


# ------------------------------------------------------------ edge control channel
@app.websocket("/edge/ws")
async def edge_ws(ws: WebSocket):
    await ws.accept()
    remote_ip = ws.client.host if ws.client else ""
    try:
        hello = json.loads(await asyncio.wait_for(ws.receive_text(), 10))
    except Exception:
        await ws.close(code=4000)
        return
    if hello.get("type") != Msg.HELLO:
        await ws.close(code=4000)
        return
    with session_scope() as db:
        dev = db.get(EdgeDevice, int(hello.get("device_id", 0)))
        ok = dev and dev.status == "active" and verify_secret(hello.get("device_secret", ""), dev.secret_hash)
        if ok and not settings.allow_untunneled_edges and remote_ip != dev.wg_ip:
            log.warning("edge %s connected from %s, expected tunnel ip %s", dev.id, remote_ip, dev.wg_ip)
            ok = False
        site = dev.site if ok else None
        if ok:
            db.expunge(dev)
            db.expunge(site)
    if not ok:
        await ws.send_text(json.dumps({"type": "error", "error": "authentication failed"}))
        await ws.close(code=4003)
        return
    sess = EdgeSession(state.ctl, ws, dev, site, hello, remote_ip)
    await state.ctl.register_edge(sess)
    try:
        while True:
            msg = json.loads(await ws.receive_text())
            await state.ctl.handle_edge_message(sess, msg)
    except (WebSocketDisconnect, RuntimeError):
        pass
    except Exception:
        log.exception("edge ws error")
    finally:
        await state.ctl.unregister_edge(sess)


# ------------------------------------------------------------ PoC (cellular subscriber radios)
@app.websocket("/poc/ws")
async def poc_ws(ws: WebSocket):
    if state.poc is None:
        await ws.accept()
        await ws.send_text(json.dumps({"t": "auth_fail", "reason": "poc_requires_dvm_backend"}))
        await ws.close(code=4003)
        return
    await state.poc.handle(ws)


# ------------------------------------------------------------ dashboard live feed
@app.websocket("/ws/dashboard")
async def dashboard_ws(ws: WebSocket):
    with session_scope() as db:
        user = user_for_token(db, ws.query_params.get("token", ""))
    if not user:
        await ws.close(code=4001)
        return
    await ws.accept()
    q = bus.subscribe()
    try:
        await ws.send_text(json.dumps({"event": "snapshot", "call_processing": state.ctl.status()}))
        while True:
            try:
                evt = await asyncio.wait_for(q.get(), 5)
            except asyncio.TimeoutError:
                evt = {"event": "tick", "call_processing": state.ctl.status()}
            await ws.send_text(json.dumps(evt, default=str))
    except Exception:
        pass
    finally:
        bus.unsubscribe(q)


def run():
    import uvicorn
    uvicorn.run("app.main:app", host=settings.http_host, port=settings.http_port, proxy_headers=True)


if __name__ == "__main__":
    run()
