#!/usr/bin/env python3
"""Stands in for the ssh hop in the P23.4 harness.

In the product the `program` Codex spawns is `ccnm internal exec-transport`,
which execs `/usr/bin/ssh <runtime> ccnm internal exec-serve --payload ...`.
This harness has no second machine and no local sshd key, and Codex runs
inside a no-egress Seatbelt where a nested exec-server cannot apply its own
sandbox -- so the "ssh" here is a TCP pipe to `harness.py`, which runs
`ccnm internal exec-serve` outside the sandbox. Bytes are relayed unchanged in
both directions; every JSON line is logged.

Mode `drop`: exit right after the first `process/exited` from the far side,
so the next command finds a dead transport.

argv: relay_client.py <out dir> <mode: basic|drop> <port>
"""
import json
import os
import socket
import sys
import threading
import time

out, mode, port = sys.argv[1], sys.argv[2], int(sys.argv[3])
pid = os.getpid()
with open(os.path.join(out, "agent-relay-spawns.jsonl"), "a") as f:
    f.write(json.dumps({"t": time.time(), "pid": pid, "ppid": os.getppid(), "pgid": os.getpgid(0),
                        "codex_home": os.environ.get("CODEX_HOME"), "env_keys": sorted(os.environ)}) + "\n")

log_path = os.path.join(out, f"agent-relay-{pid}.jsonl")
lock = threading.Lock()


def log(**fields):
    fields["t"] = round(time.time(), 3)
    with lock, open(log_path, "a") as f:
        f.write(json.dumps(fields) + "\n")


def parse(line):
    try:
        return json.loads(line)
    except Exception:
        return {"raw": line.decode(errors="replace")}


sock = socket.create_connection(("127.0.0.1", port), timeout=10)
sock.settimeout(None)
first_exited = threading.Event()


def c2s():
    for line in sys.stdin.buffer:
        log(dir="c2s", msg=parse(line))
        sock.sendall(line)
    log(dir="c2s", event="stdin-eof")
    try:
        sock.shutdown(socket.SHUT_WR)
    except OSError:
        pass


def s2c():
    f = sock.makefile("rb")
    for line in f:
        msg = parse(line)
        log(dir="s2c", msg=msg)
        sys.stdout.buffer.write(line)
        sys.stdout.buffer.flush()
        if msg.get("method") == "process/exited":
            first_exited.set()
    log(dir="s2c", event="socket-eof")


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
