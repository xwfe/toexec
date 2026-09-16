#!/usr/bin/env python3
"""ccnm P26.1: does Codex 0.154.0 answer liveness requests from the exec-server side?

Real Codex TUI (fake model, no-egress Seatbelt, fresh HOME, per-session
CODEX_HOME) -> liveness_relay.py -> the P23 harness's real `ccnm internal
exec-serve` -> real `codex exec-server`. Zero model spend.

Conversation: a first turn runs a command; then Codex sits idle for IDLE seconds
while pings keep coming; a second prompt runs a slow command with pings arriving
mid-command; a third turn finishes. Every PING seconds a ping goes to Codex.

usage: probe.py <new out dir> [ping seconds, default 5] [idle seconds, default 40]
"""
import json
import os
import shlex
import subprocess
import sys
import time
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "p23-stdio"))
sys.path.insert(0, str(HERE.parent / "native-surface"))
import harness as h  # noqa: E402
from probe import SANDBOX_PROFILE, Log, MockModel  # noqa: E402

TMUX = "/opt/homebrew/bin/tmux"


def pane(sock):
    return subprocess.run([TMUX, "-L", sock, "capture-pane", "-p", "-t", "s", "-S", "-300"],
                          capture_output=True, text=True).stdout


def prompt(sock, text):
    # Text and Enter in one send-keys arrive as a paste, and the TUI turns the
    # Enter into a newline in the composer instead of submitting (seen in r1).
    subprocess.run([TMUX, "-L", sock, "send-keys", "-t", "s", text], check=True)
    time.sleep(1)
    subprocess.run([TMUX, "-L", sock, "send-keys", "-t", "s", "Enter"], check=True)


def wait_step(model, step, timeout=90):
    deadline = time.time() + timeout
    while model.step < step and time.time() < deadline:
        time.sleep(0.5)
    return model.step >= step


def main():
    out = Path(sys.argv[1]).resolve()
    every = float(sys.argv[2]) if len(sys.argv) > 2 else 5.0
    idle = float(sys.argv[3]) if len(sys.argv) > 3 else 40.0
    out.mkdir(parents=True)
    work = out / "work"
    work.mkdir()
    (work / "AGENTS.md").write_text("P26-AGENTS-MARKER\n")
    (out / "userhome").mkdir()
    log = Log(out / "harness.jsonl")
    config = h.write_runtime_config(out, work)
    relay = h.RuntimeRelay(out, log, config, str(uuid.uuid4()))
    home = h.write_session_home(out, work, h.PYTHON,
                                [str(HERE / "liveness_relay.py"), str(out), str(relay.port), str(every)])
    plan = [("exec", "echo before-idle"), ("say", "turn one done"),
            ("exec", "sleep 12; echo after-slow"), ("say", "turn two done"),
            ("exec", "echo last"), ("say", "turn three done")]
    model = MockModel(out, plan, log)

    profile = out / "no-egress.sb"
    profile.write_text(SANDBOX_PROFILE)
    env = {"HOME": str(out / "userhome"), "CODEX_HOME": str(home), "PATH": os.environ["PATH"], "TERM": "xterm-256color"}
    inner = ["env", "-i"] + [f"{k}={v}" for k, v in env.items()]
    inner += ["sandbox-exec", "-f", str(profile), h.CODEX] + h.codex_args(model.port, work) + ["--", "run turn one"]
    sock = f"p26-{os.getpid()}"
    (out / "local-cwd").mkdir()
    subprocess.run([TMUX, "-L", sock, "new-session", "-d", "-s", "s", "-x", "200", "-y", "60", "-c", str(out / "local-cwd"),
                    shlex.join(inner) + "; echo CODEX_EXITED=$?; sleep 600"], check=True)
    timeline = {}
    try:
        timeline["turn_one"] = wait_step(model, 2)
        time.sleep(3)
        t_idle = time.time()
        time.sleep(idle)
        timeline["idle_seconds"] = round(time.time() - t_idle, 1)
        prompt(sock, "run turn two")
        timeline["turn_two"] = wait_step(model, 4, timeout=120)
        time.sleep(3)
        prompt(sock, "run turn three")
        timeline["turn_three"] = wait_step(model, 6)
        time.sleep(6)
    finally:
        final_pane = pane(sock)
        (out / "tui-pane.txt").write_text(final_pane)
        subprocess.run([TMUX, "-L", sock, "kill-server"], capture_output=True)
        exec_serve_rcs = relay.close()

    rows = []
    for f in sorted(out.glob("liveness-relay-*.jsonl")):
        rows += [json.loads(l) for l in f.read_text().splitlines()]
    pings = [r["id"] for r in rows if r.get("dir") == "ping"]
    replies = {r["id"]: r for r in rows if r.get("dir") == "codex-reply"}
    outputs = h.model_outputs(out)
    summary = {
        "ping_seconds": every, **timeline,
        "pings_sent": len(pings),
        "pings_answered": sum(1 for p in pings if p in replies),
        "reply_error_codes": sorted({(r.get("error") or {}).get("code") for r in replies.values()}),
        "reply_messages": sorted({(r.get("error") or {}).get("message") for r in replies.values()}),
        "unanswered": [p for p in pings if p not in replies],
        "transport_spawns": len([r for r in rows if r.get("event") == "connected"]),
        "relay_events": [r for r in rows if "event" in r],
        "runtime_connections": relay.connections,
        "exec_serve_exit_codes": exec_serve_rcs,
        "commands_ok": [("exit_code\\\":0" in (o or "")) or ('"exit_code":0' in (o or "")) for o in outputs],
        "outputs_head": [(o or "")[:220] for o in outputs],
        "disconnect_reported": any("disconnected" in (o or "") for o in outputs),
        # The run directory is named p26-liveness, so look for what Codex would print.
        "pane_mentions_ping": any(k in final_pane for k in ("ccnm/liveness", "-32601", "does not implement")),
        "pane_tail": final_pane.rstrip().splitlines()[-14:],
        "work_files": sorted(p.name for p in work.iterdir()),
    }
    (out / "summary.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False, default=str))
    print(json.dumps({k: v for k, v in summary.items() if k not in ("relay_events", "outputs_head")}, indent=1, ensure_ascii=False, default=str))
    for o in summary["outputs_head"]:
        print("OUT:", o)


if __name__ == "__main__":
    main()
