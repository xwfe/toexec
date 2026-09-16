#!/usr/bin/env python3
"""The `program` named in environments.toml: what Codex spawns as its exec-server
transport. Connects to an exec-server the harness started *outside* Codex's
no-egress sandbox (a nested Seatbelt cannot apply a second profile) and relays
stdio lines <-> WebSocket text frames, logging every message. Mode `drop` kills
itself right after the first command has finished, to see what Codex does then.

argv: relay.py <out dir> <mode: basic|drop> <ws://host:port>
"""
import base64
import json
import os
import socket
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "native-surface"))
from probe import read_frame, text_frame  # noqa: E402

out, mode, url = sys.argv[1], sys.argv[2], sys.argv[3]
host, port = url.removeprefix("ws://").split(":")
pid = os.getpid()

with open(os.path.join(out, "relay-spawns.jsonl"), "a") as f:
    f.write(json.dumps({"t": time.time(), "pid": pid, "argv": sys.argv, "cwd": os.getcwd(),
                        "env_keys": sorted(os.environ), "codex_home": os.environ.get("CODEX_HOME"),
                        "pgid": os.getpgid(0), "ppid": os.getppid()}) + "\n")

log_path = os.path.join(out, f"relay-{pid}.jsonl")
lock = threading.Lock()


def log(**fields):
    fields["t"] = round(time.time(), 3)
    with lock, open(log_path, "a") as f:
        f.write(json.dumps(fields) + "\n")


sock = socket.create_connection((host, int(port)), timeout=10)
key = base64.b64encode(os.urandom(16)).decode()
sock.sendall((f"GET / HTTP/1.1\r\nHost: {host}:{port}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
              f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
buf = b""
while b"\r\n\r\n" not in buf:
    buf += sock.recv(65536)
head, buf = buf.split(b"\r\n\r\n", 1)
assert b" 101 " in head.split(b"\r\n")[0], head
sock.settimeout(None)
first_exited = threading.Event()


def parse(line):
    try:
        return json.loads(line)
    except Exception:
        return {"raw": line.decode(errors="replace")}


def c2s():
    for line in sys.stdin.buffer:
        log(dir="c2s", msg=parse(line))
        sock.sendall(text_frame(line.rstrip(b"\n"), masked=True))
    log(dir="c2s", event="stdin-eof")
    try:
        sock.shutdown(socket.SHUT_WR)
    except OSError:
        pass


def s2c():
    global buf
    while True:
        while (frame := read_frame(buf)) is None:
            chunk = sock.recv(65536)
            if not chunk:
                log(dir="s2c", event="ws-eof")
                return
            buf += chunk
        raw, _, opcode, payload = frame
        buf = buf[len(raw):]
        if opcode != 1:
            continue
        msg = parse(payload)
        log(dir="s2c", msg=msg)
        sys.stdout.buffer.write(payload + b"\n")
        sys.stdout.buffer.flush()
        if msg.get("method") == "process/exited":
            first_exited.set()


threading.Thread(target=c2s, daemon=True).start()
t = threading.Thread(target=s2c, daemon=True)
t.start()

if mode == "drop":
    first_exited.wait()
    log(event="relay-dropping")
    sock.close()
    os._exit(1)

t.join()
log(event="relay-exit")
