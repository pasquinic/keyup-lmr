"""Tiny HTTP control API for the lab simulators: GET/POST path?query -> JSON."""
from __future__ import annotations

import asyncio
import json
from urllib.parse import parse_qs, urlparse


async def serve(port: int, handler) -> None:
    async def conn(reader, writer):
        try:
            line = (await reader.readline()).decode()
            method, target, _ = line.split(" ", 2)
            while (await reader.readline()) not in (b"\r\n", b""):
                pass
            u = urlparse(target)
            q = {k: v[0] for k, v in parse_qs(u.query).items()}
            res = await handler(method, u.path, q)
            body = json.dumps(res).encode()
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: "
                         + str(len(body)).encode() + b"\r\nConnection: close\r\n\r\n" + body)
            await writer.drain()
        except Exception as e:  # pragma: no cover - lab helper
            body = json.dumps({"error": str(e)}).encode()
            writer.write(b"HTTP/1.1 500 Error\r\nContent-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body)
        finally:
            writer.close()

    await asyncio.start_server(conn, "127.0.0.1", port)
