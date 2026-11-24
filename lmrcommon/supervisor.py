#!/usr/bin/env python3
"""Process supervisor for DVMProject daemons (dvmfne / dvmhost / dvmbridge).

KeyUp generates one directory per daemon instance:

    <root>/<instance>/run.json      {"binary": "dvmhost", "args": ["-f", "-c", "config.yml"]}
    <root>/<instance>/config.yml    + any lookup files (talkgroup_rules.yml, rid_acl.dat, ...)

The supervisor starts every instance with its directory as the working directory
(so relative file paths in DVM configs work), restarts it when run.json or
config.yml change or when it exits, and stops instances whose directory is removed.
Lookup files (talkgroup rules, RID/peer ACLs, iden table) are hot-reloaded by DVM
itself and do not cause a restart.

Stdlib only, so it runs unchanged in the DVM container and inside the edge agent.
    python3 supervisor.py /etc/dvm/instances [--bin-dir /usr/local/bin]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import shutil
import signal
import subprocess
import threading
import time
from collections import deque
from typing import Callable

log = logging.getLogger("supervisor")
WATCHED = ("run.json", "config.yml")


def _sig(path: str) -> str:
    h = hashlib.sha256()
    for name in WATCHED:
        p = os.path.join(path, name)
        if os.path.exists(p):
            with open(p, "rb") as f:
                h.update(f.read())
    return h.hexdigest()


class Instance:
    def __init__(self, name: str, path: str):
        self.name, self.path = name, path
        self.proc: subprocess.Popen | None = None
        self.sig = ""
        self.restarts = 0
        self.started_at = 0.0
        self.next_start = 0.0
        self.backoff = 1.0
        self.last_exit: int | None = None
        self.tail: deque[str] = deque(maxlen=200)

    def status(self) -> dict:
        return {"name": self.name, "running": bool(self.proc and self.proc.poll() is None),
                "pid": self.proc.pid if self.proc else None, "restarts": self.restarts,
                "uptime_s": round(time.time() - self.started_at) if self.proc else 0,
                "last_exit": self.last_exit}


class Supervisor:
    def __init__(self, root: str, bin_dir: str = "", on_line: Callable[[str, str], None] | None = None,
                 interval: float = 2.0):
        self.root, self.bin_dir, self.on_line, self.interval = root, bin_dir, on_line, interval
        self.instances: dict[str, Instance] = {}
        self.lock = threading.Lock()
        self._stop = threading.Event()
        os.makedirs(root, exist_ok=True)

    # ------------------------------------------------------------------ process control
    def _binary(self, name: str) -> str:
        if self.bin_dir and os.path.exists(os.path.join(self.bin_dir, name)):
            return os.path.join(self.bin_dir, name)
        return shutil.which(name) or name

    def _start(self, inst: Instance) -> None:
        with open(os.path.join(inst.path, "run.json")) as f:
            spec = json.load(f)
        cmd = [self._binary(spec["binary"]), *spec.get("args", [])]
        inst.sig = _sig(inst.path)
        try:
            inst.proc = subprocess.Popen(cmd, cwd=inst.path, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                         stdin=subprocess.DEVNULL, text=True, bufsize=1,
                                         start_new_session=True)
        except OSError as e:
            log.error("%s: cannot start %s: %s", inst.name, cmd[0], e)
            inst.proc = None
            inst.next_start = time.time() + inst.backoff
            inst.backoff = min(inst.backoff * 2, 60)
            return
        inst.started_at = time.time()
        log.info("%s: started %s (pid %d)", inst.name, " ".join(cmd), inst.proc.pid)
        threading.Thread(target=self._reader, args=(inst, inst.proc), daemon=True).start()

    def _reader(self, inst: Instance, proc: subprocess.Popen) -> None:
        for line in proc.stdout:
            line = line.rstrip()
            inst.tail.append(line)
            if self.on_line:
                try:
                    self.on_line(inst.name, line)
                except Exception:
                    pass

    def _stop_proc(self, inst: Instance) -> None:
        p = inst.proc
        if p and p.poll() is None:
            try:
                os.killpg(p.pid, signal.SIGTERM)
                p.wait(5)
            except Exception:
                try:
                    os.killpg(p.pid, signal.SIGKILL)
                except Exception:
                    pass
        inst.proc = None

    # ------------------------------------------------------------------ reconcile loop
    def scan(self) -> None:
        with self.lock:
            present = {d for d in os.listdir(self.root)
                       if os.path.isfile(os.path.join(self.root, d, "run.json"))}
            for name in list(self.instances):
                if name not in present:
                    log.info("%s: removed, stopping", name)
                    self._stop_proc(self.instances.pop(name))
            now = time.time()
            for name in sorted(present):
                inst = self.instances.setdefault(name, Instance(name, os.path.join(self.root, name)))
                sig = _sig(inst.path)
                if inst.proc and inst.proc.poll() is None:
                    if sig != inst.sig:
                        log.info("%s: configuration changed, restarting", name)
                        self._stop_proc(inst)
                        inst.backoff = 1.0
                        self._start(inst)
                    elif now - inst.started_at > 60:
                        inst.backoff = 1.0
                    continue
                if inst.proc is not None:  # exited
                    inst.last_exit = inst.proc.returncode
                    log.warning("%s: exited with %s, restarting in %.0fs", name, inst.last_exit, inst.backoff)
                    inst.proc = None
                    inst.restarts += 1
                    inst.next_start = now + inst.backoff
                    inst.backoff = min(inst.backoff * 2, 60)
                if now >= inst.next_start:
                    self._start(inst)

    def status(self) -> list[dict]:
        with self.lock:
            return [i.status() for i in self.instances.values()]

    def run(self) -> None:
        while not self._stop.is_set():
            try:
                self.scan()
            except Exception:
                log.exception("supervisor scan failed")
            self._stop.wait(self.interval)
        with self.lock:
            for inst in self.instances.values():
                self._stop_proc(inst)

    def start_thread(self) -> threading.Thread:
        t = threading.Thread(target=self.run, daemon=True, name="dvm-supervisor")
        t.start()
        return t

    def stop(self) -> None:
        self._stop.set()


def write_instances(root: str, instances: dict[str, dict], prune_prefixes: tuple[str, ...] = ()) -> set[str]:
    """Write instance dirs; returns set of changed file paths ('<instance>/<file>')."""
    changed = set()
    os.makedirs(root, exist_ok=True)
    for name, spec in instances.items():
        d = os.path.join(root, name)
        os.makedirs(os.path.join(d, "log"), exist_ok=True)
        files = dict(spec["files"])
        files["run.json"] = json.dumps({"binary": spec["binary"], "args": spec["args"]}, indent=1)
        for fname, content in files.items():
            p = os.path.join(d, fname)
            old = open(p).read() if os.path.exists(p) else None
            if old != content:
                tmp = p + ".tmp"
                with open(tmp, "w") as f:
                    f.write(content)
                os.replace(tmp, p)
                changed.add(f"{name}/{fname}")
    for d in os.listdir(root):
        if d not in instances and d.startswith(prune_prefixes) and os.path.isfile(os.path.join(root, d, "run.json")):
            os.remove(os.path.join(root, d, "run.json"))  # supervisor stops it
            changed.add(f"{d}/run.json")
    return changed


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("root")
    ap.add_argument("--bin-dir", default="")
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s supervisor: %(message)s")

    def echo(name, line):
        print(f"[{name}] {line}", flush=True)

    sup = Supervisor(a.root, a.bin_dir, on_line=echo)
    signal.signal(signal.SIGTERM, lambda *_: sup.stop())
    signal.signal(signal.SIGINT, lambda *_: sup.stop())
    sup.run()


if __name__ == "__main__":
    main()
