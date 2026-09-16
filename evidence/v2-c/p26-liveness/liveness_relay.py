#!/usr/bin/env python3
"""The transport program for the ccnm P26.1 probe: the P23 relay, plus liveness.

Every PING seconds it writes a server-to-client request Codex does not know,
`{"id": "ccnm-liveness-<n>", "method": "ccnm/liveness", "params": {}}`, on Codex's
stdin, and takes Codex's replies to those ids out of the stream instead of
forwarding them. Everything else is relayed unchanged to the harness, which runs
the real `ccnm internal exec-serve` and `codex exec-server`.

argv: liveness_relay.py <out dir> <port> <ping seconds>
"""
import json
import os
import socket
import sys
import threading
import time

out, port, every = sys.argv[1], int(sys.argv[2]), float(sys.argv[3])
pid = os.getpid()
log_path = os.path.join(out, f"liveness-relay-{pid}.jsonl")
lock, write_lock = threading.Lock(), threading.Lock()


def log(**fields):
    fields["t"] = round(time.time(), 3)
    with lock, open(log_path, "a") as f:
        f.write(json.dumps(fields) + "\n")


def to_codex(data):
    with write_lock:
        sys.stdout.buffer.write(data)
        sys.stdout.buffer.flush()


sock = socket.create_connection(("127.0.0.1", port), timeout=10)
sock.settimeout(None)
log(event="connected")


def c2s():
    for line in sys.stdin.buffer:
        try:
            msg = json.loads(line)
        except ValueError:
            msg = {}
        rid = msg.get("id")
        if "method" not in msg and isinstance(rid, str) and rid.startswith("ccnm-liveness-"):
            log(dir="codex-reply", id=rid, error=msg.get("error"), result=msg.get("result"))
            continue
        log(dir="c2s", method=msg.get("method"), id=rid)
        sock.sendall(line)
    log(event="codex-stdin-eof")
    try:
        sock.shutdown(socket.SHUT_WR)
    except OSError:
        pass


def s2c():
    f = sock.makefile("rb")
    for line in f:
        try:
            msg = json.loads(line)
        except ValueError:
            msg = {}
        log(dir="s2c", method=msg.get("method"), id=msg.get("id"))
        to_codex(line)
    log(event="server-eof")


def pinger():
    n = 0
    while True:
        time.sleep(every)
        n += 1
        rid = f"ccnm-liveness-{n}"
        try:
            to_codex((json.dumps({"id": rid, "method": "ccnm/liveness", "params": {}}) + "\n").encode())
        except OSError as exc:
            log(event="ping-write-failed", id=rid, error=repr(exc))
            return
        log(dir="ping", id=rid)


threading.Thread(target=c2s, daemon=True).start()
threading.Thread(target=pinger, daemon=True).start()
t = threading.Thread(target=s2c, daemon=True)
t.start()
t.join()
log(event="relay-exit")
