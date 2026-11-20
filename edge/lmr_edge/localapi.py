"""Tiny local HTTP API (127.0.0.1 only) for field troubleshooting and lab simulation.

  GET  /status                                   agent + channel status (JSON)
  POST /sim/key?channel=ID&seconds=2&unit=1001   simulate a radio keying up (sim backend)
  POST /test/tone?channel=ID                     play a 1 kHz test tone on a channel
"""
from __future__ import annotations

import asyncio
import json
import logging
from urllib.parse import parse_qs, urlparse

from lmrcommon import audio

log = logging.getLogger("localapi")


async def serve(agent, host: str, port: int):
    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        try:
            line = (await asyncio.wait_for(reader.readline(), 5)).decode()
            while (await reader.readline()) not in (b"\r\n", b"\n", b""):
                pass
            method, target, _ = line.split(" ", 2)
            u = urlparse(target)
            q = {k: v[0] for k, v in parse_qs(u.query).items()}
            code, body = 200, {}
            if u.path == "/status":
                body = agent.status()
            elif u.path == "/sim/key" and method == "POST":
                r = agent.radios.get(int(q.get("channel", 0)))
                if not r or not hasattr(r, "simulate_rx"):
                    code, body = 404, {"error": "no such sim channel"}
                else:
                    asyncio.get_running_loop().create_task(
                        r.simulate_rx(float(q.get("seconds", 2)), int(q.get("unit", 0)), float(q.get("freq", 1000))))
                    body = {"ok": True}
            elif u.path == "/test/tone" and method == "POST":
                r = agent.radios.get(int(q.get("channel", 0)))
                if r:
                    asyncio.get_running_loop().create_task(r.play(audio.tone(1000, 1000)))
                body = {"ok": bool(r)}
            else:
                code, body = 404, {"error": "not found"}
            data = json.dumps(body, default=str).encode()
            writer.write(f"HTTP/1.1 {code} OK\r\nContent-Type: application/json\r\nContent-Length: {len(data)}\r\n"
                         f"Connection: close\r\n\r\n".encode() + data)
            await writer.drain()
        except Exception as e:
            log.debug("local api error: %s", e)
        finally:
            writer.close()

    server = await asyncio.start_server(handler, host, port)
    log.info("local API on http://%s:%d", host, port)
    return server
