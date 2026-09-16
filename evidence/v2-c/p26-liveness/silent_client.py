#!/usr/bin/env python3
"""ccnm P26.3, real binaries at default timings: a client that goes silent.

Runs the real `ccnm internal exec-serve` (default 30 s / 10 min) in front of
the real `codex exec-server`, speaks the P21-captured handshake and one
`process/start` of a long sleep, then never sends another byte. Logs every
line the supervisor sends with its time, and afterwards what is left: exit
code, stderr, the write guard, and whether the sleep is still running.

usage: silent_client.py <new out dir>
"""
import json
import os
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "p23-stdio"))
import harness as h  # noqa: E402

CAPTURED = HERE / "../../../../ccnm/tests/fixtures/codex-0.154.0/exec-server/process-start-workspace-write.json"
SLEEP = "3593"  # an unusual length, so the process list check finds only this one


def sleeps():
    # Exact name and arguments. `pgrep -f` matches any command line that merely
    # contains the text -- in the silent-defaults run it listed a waiting shell
    # whose script mentioned `sleep 3593` (macOS pgrep only hides its own
    # ancestors), which looked like a leftover.
    out = subprocess.run(["ps", "-axo", "pid=,comm=,args="], capture_output=True, text=True, errors="replace").stdout
    found = []
    for line in out.splitlines():
        parts = line.split(None, 2)
        if len(parts) == 3 and parts[1] in ("sleep", "/bin/sleep") and parts[2].split() in (["sleep", SLEEP], ["/bin/sleep", SLEEP]):
            found.append(int(parts[0]))
    return found


def main():
    out = Path(sys.argv[1]).resolve()
    out.mkdir(parents=True)
    work = out / "work"
    work.mkdir()
    (out / "outside").mkdir()
    config = h.write_runtime_config(out, work)
    session = str(uuid.uuid4())
    wire = h.b64url({"protocol": 6, "workspace": "demo", "agent": h.IDENTITY, "session": session})
    env = {"PATH": os.environ["PATH"], "HOME": str(out / "runtime" / "home"),
           "XDG_STATE_HOME": str(out / "runtime" / "state"), "CCNM_CONFIG": str(config), "CCNM_LOG": "info"}
    assert not sleeps(), "a sleep of the same length is already running"
    stderr = open(out / "exec-serve.stderr", "wb")
    child = subprocess.Popen([h.CCNM, "internal", "exec-serve", "--payload", wire], env=env,
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=stderr)
    t0 = time.time()
    received = []

    def read():
        for line in child.stdout:
            received.append({"t": round(time.time() - t0, 3), "msg": h.parse(line)})

    reader = threading.Thread(target=read, daemon=True)
    reader.start()

    def send(message):
        child.stdin.write((json.dumps(message) + "\n").encode())
        child.stdin.flush()

    send({"id": 1, "method": "initialize", "params": {"clientName": "codex-environment", "resumeSessionId": None}})
    send({"method": "initialized", "params": {}})
    start = json.loads(CAPTURED.read_text()
                       .replace("{ROOT}", str(work))
                       .replace("{OUTSIDE}", str(out / "outside"))
                       .replace("{SERVER_HOME}", str(out / "runtime" / "home")))
    start["id"] = 2
    start["params"]["processId"] = "p2"
    start["params"]["argv"] = ["/bin/sh", "-c", f"exec sleep {SLEEP}"]
    # The capture's threadId is the placeholder `<thread>`, which the real
    # executor rejects as not a UUID (-32602); ccnm's own real-executor test
    # drops `metadata` the same way.
    start["params"].pop("metadata", None)
    send(start)
    last_byte = round(time.time() - t0, 3)
    time.sleep(5)
    running_before = sleeps()
    # Silent from here on: stdin stays open, nothing is written, nothing answered.
    rc = child.wait(timeout=20 * 60)
    ended = round(time.time() - t0, 3)
    reader.join(timeout=5)
    time.sleep(1)
    stderr.close()

    guards = {}
    guard_dir = out / "runtime" / "state" / "ccnm" / "write-guards"
    for p in sorted(guard_dir.glob("*.lock")):
        guards[p.name] = p.read_text()
    pings = [r["t"] for r in received if r["msg"].get("method") == "ccnm/liveness"]
    summary = {
        "exit_code": rc,
        "last_client_byte_s": last_byte,
        "ended_s": ended,
        "silence_until_end_s": round(ended - last_byte, 1),
        "handshake_ok": any(r["msg"].get("id") == 1 and "result" in r["msg"] for r in received),
        "process_start_reply": next((r["msg"] for r in received if r["msg"].get("id") == 2), None),
        "sleep_running_while_silent": running_before,
        "sleep_running_after": sleeps(),
        "pings": len(pings),
        "first_ping_s": pings[0] if pings else None,
        "ping_gaps_s": sorted({round(b - a) for a, b in zip(pings, pings[1:])}),
        "ping_ids": [r["msg"]["id"] for r in received if r["msg"].get("method") == "ccnm/liveness"][:3],
        "write_guards": guards,
        "exec_server_homes_left": sorted(p.name for p in (out / "runtime" / "state" / "ccnm" / "exec-server").glob("*")),
        "stderr_tail": (out / "exec-serve.stderr").read_text(errors="replace").splitlines()[-6:],
    }
    (out / "received.jsonl").write_text("".join(json.dumps(r) + "\n" for r in received))
    (out / "summary.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False))
    print(json.dumps(summary, indent=1, ensure_ascii=False))


if __name__ == "__main__":
    main()
