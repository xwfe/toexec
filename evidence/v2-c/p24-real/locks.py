#!/usr/bin/env python3
"""ccnm P24.2: the exec-server chain, the managed MCP entry and the external MCP
coding entry compete for one workspace write guard on hpsrv. No model turns.

Neutral matrix (each round): every entry takes a turn holding the guard; while it
holds, a second session of every entry (its own kind included) must be refused at
start with "write guard is busy"; after the holder closes, the guard reads
released and a new session can take it.

Real Codex (once each, no prompt, so no model request): an idle Codex session on
the chain holds the guard and the three neutral entries are refused; and while a
neutral exec-serve holds it, `ccnm run p24` is refused before any session exists.

usage: locks.py <rounds> <out json>
"""

import json
import os
import subprocess
import sys
import time

import native_client as nc

CCNM_LOCAL = os.path.expanduser("~/.local/state/ccnm-p24/bin/ccnm")
AGENT_ENV = dict(os.environ, CCNM_CONFIG=os.path.expanduser("~/.config/ccnm/p24-agent.toml"),
                 XDG_STATE_HOME=os.path.expanduser("~/.local/state/ccnm-p24"))
TMUX = "/opt/homebrew/bin/tmux"
results = []


def record(name, ok, detail=""):
    results.append({"check": name, "ok": bool(ok), "detail": detail})
    print(("PASS " if ok else "FAIL ") + name + ("" if ok else f"  -- {json.dumps(detail, default=str)[:600]}"))


def open_entry(kind):
    if kind == "native":
        s = nc.Session("exec-serve")
        return s, s.handshake(timeout=40)
    if kind == "managed":
        s = nc.Session("mcp-serve")
        return s, s.mcp_handshake(timeout=40)
    s = nc.Session(argv=[CCNM_LOCAL, "--lang", "en", "mcp", "bridge", "p24", "--node", "hpsrv", "--mode", "coding"],
                   env=AGENT_ENV)
    return s, s.mcp_handshake(timeout=40)


def opened(reply):
    return bool(reply) and "result" in reply


def refused_busy(s, reply):
    if opened(reply):
        return False, "opened"
    s.proc.wait(timeout=30)
    text = s.stderr.decode(errors="replace")
    return "write guard is busy" in text, text.strip().splitlines()[:2]


def guard_held_by(session=None):
    state = nc.guard_state()
    if session:
        return state.startswith(f"held {session} p24"), state
    return state.startswith("held ") and state.endswith(" p24"), state


def neutral_round(n):
    kinds = ["native", "managed", "external"]
    for holder_kind in kinds:
        holder, reply = open_entry(holder_kind)
        record(f"round {n}: {holder_kind} takes the guard", opened(reply), [reply, holder.stderr.decode(errors="replace")[-300:]])
        ok, state = guard_held_by(holder.session if holder_kind != "external" else None)
        record(f"round {n}: guard reads held while {holder_kind} is open", ok, state)
        for contender_kind in kinds:
            contender, reply = open_entry(contender_kind)
            ok, detail = refused_busy(contender, reply)
            record(f"round {n}: {contender_kind} refused busy while {holder_kind} holds", ok, detail)
            if opened(reply):
                contender.close()
        ok, state = guard_held_by(holder.session if holder_kind != "external" else None)
        record(f"round {n}: {holder_kind} still holds after the refusals", ok, state)
        rc, _ = holder.close()
        time.sleep(1)
        state = nc.guard_state()
        record(f"round {n}: released after {holder_kind} closes", state == "released", [rc, state])


def tmux_has(name):
    return subprocess.run([TMUX, "-L", "ccnm", "has-session", "-t", name], capture_output=True).returncode == 0


def codex_checks():
    # Codex as the holder: an idle session, no prompt.
    start = subprocess.run([CCNM_LOCAL, "--lang", "en", "run", "p24", "--detached"], env=AGENT_ENV,
                           capture_output=True, text=True, timeout=120)
    session = next((l.split()[1] for l in start.stdout.splitlines() + start.stderr.splitlines() if l.startswith("id ")), None)
    record("codex: idle session starts", start.returncode == 0 and session, [start.returncode, start.stderr[-300:]])
    deadline = time.time() + 60
    ok, state = False, ""
    while time.time() < deadline and not ok:
        ok, state = guard_held_by(session)
        time.sleep(2)
    record("codex: its exec-server connection holds the guard before any prompt", ok, state)
    for kind in ["native", "managed", "external"]:
        contender, reply = open_entry(kind)
        ok2, detail = refused_busy(contender, reply)
        record(f"codex holds: {kind} refused busy", ok2, detail)
    subprocess.run([TMUX, "-L", "ccnm", "send-keys", "-t", "ccnm-p24", "/exit"], capture_output=True)
    time.sleep(1)
    subprocess.run([TMUX, "-L", "ccnm", "send-keys", "-t", "ccnm-p24", "Enter"], capture_output=True)
    deadline = time.time() + 60
    while time.time() < deadline and tmux_has("ccnm-p24"):
        time.sleep(1)
    time.sleep(2)
    state = nc.guard_state()
    record("codex: /exit releases the guard", state == "released", state)

    # Codex as the contender: refused by the preflight, nothing created.
    holder, reply = open_entry("native")
    record("codex contender: neutral exec-serve holds", opened(reply), reply)
    sessions_dir = os.path.expanduser("~/.local/state/ccnm-p24/ccnm/sessions")
    before = set(os.listdir(sessions_dir))
    start = subprocess.run([CCNM_LOCAL, "--lang", "en", "run", "p24", "--detached"], env=AGENT_ENV,
                           capture_output=True, text=True, timeout=120)
    after = set(os.listdir(sessions_dir))
    record("codex contender: ccnm run refused with the busy guard",
           start.returncode != 0 and "busy" in (start.stderr + start.stdout), [start.returncode, start.stderr.strip()[:400]])
    record("codex contender: no session record and no tmux session created", after == before and not tmux_has("ccnm-p24"),
           sorted(after - before))
    holder.close()
    time.sleep(1)
    record("codex contender: released after the neutral holder closes", nc.guard_state() == "released", nc.guard_state())


def main():
    rounds, out = int(sys.argv[1]), sys.argv[2]
    for n in range(1, rounds + 1):
        neutral_round(n)
    codex_checks()
    left = nc.marked_processes()
    record("no process with a session marker left", not left, left)
    summary = {"rounds": rounds, "passed": sum(r["ok"] for r in results), "failed": sum(not r["ok"] for r in results),
               "checks": results}
    open(out, "w").write(json.dumps(summary, indent=2, ensure_ascii=False, default=str))
    print(json.dumps({k: summary[k] for k in ("rounds", "passed", "failed")}))
    sys.exit(1 if summary["failed"] else 0)


if __name__ == "__main__":
    main()
