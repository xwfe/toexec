#!/usr/bin/env python3
"""ccnm P24.3 real-Codex spot checks (one model prompt each).

A real Codex session on the chain is asked to run one slow command. While that
command is running on hpsrv, one fault is injected:

  transport   SIGKILL the ssh that Codex's `ccnm internal exec-transport` became (this Mac)
  executor    SIGKILL `codex exec-server` on hpsrv

Then: does Codex report failure without starting a second transport or re-running
the command, does hpsrv release the guard with nothing left, and does the command's
completion marker ever appear anywhere.

usage: spotcheck.py <transport|executor> <out json>
"""

import json
import os
import re
import subprocess
import sys
import time

import native_client as nc
from locks import AGENT_ENV, CCNM_LOCAL, TMUX

MARK = {"transport": "finished-p24-transport", "executor": "finished-p24-executor"}


def pane():
    return subprocess.run([TMUX, "-L", "ccnm", "capture-pane", "-p", "-t", "ccnm-p24", "-S", "-200"],
                          capture_output=True, text=True).stdout


def keys(*k):
    for key in k:
        subprocess.run([TMUX, "-L", "ccnm", "send-keys", "-t", "ccnm-p24", key], capture_output=True)
        time.sleep(0.5)


def local_transports():
    ps = subprocess.run(["ps", "-axo", "pid=,ppid=,command="], capture_output=True, text=True).stdout
    rows = [l.split(None, 2) for l in ps.splitlines() if l.strip()]
    codex = [int(p) for p, pp, c in rows if "codex --no-alt-screen" in c and "sandbox-exec" not in c]
    return [int(p) for p, pp, c in rows if int(pp) in codex and "internal exec-serve" in c and "/usr/bin/ssh" in c]


def dismiss_dialogs(text):
    if "Keep current model" in text and "Press enter to confirm" in text:
        keys("Down", "Enter")
        return "kept current model"
    return None


def main():
    kind, out = sys.argv[1], sys.argv[2]
    mark = MARK[kind]
    entry = {"kind": kind, "marker": mark}
    prompt = f"Run exactly this shell command once and nothing else: sleep 45; echo {mark} . Then report its output in one sentence."
    start = subprocess.run([CCNM_LOCAL, "--lang", "en", "run", "p24", "--detached", prompt], env=AGENT_ENV,
                           capture_output=True, text=True, timeout=120)
    entry["session"] = next((l.split()[1] for l in start.stdout.splitlines() + start.stderr.splitlines() if l.startswith("id ")), None)
    entry["started"] = start.returncode == 0
    # Wait for the command to be running on hpsrv.
    deadline, running = time.time() + 120, ""
    while time.time() < deadline:
        running = nc.remote("pgrep -u ccrun -af 'sleep 45' | grep -v pgrep || true").strip()
        if running:
            break
        dismiss_dialogs(pane())
        time.sleep(1)
    entry["command_running_on_hpsrv"] = running
    entry["transports_before"] = local_transports()
    entry["guard_before"] = nc.guard_state()
    time.sleep(3)
    if kind == "transport":
        for pid in entry["transports_before"]:
            os.kill(pid, 9)
    else:
        pid = nc.remote("pgrep -u ccrun -f 'codex exec-server --listen stdio' | head -1").strip()
        entry["executor_pid"] = pid
        nc.remote(f"kill -KILL {pid}")
    t_fault = time.time()
    released, secs = None, None
    while time.time() - t_fault < 30:
        state = nc.guard_state()
        if state == "released":
            released, secs = state, round(time.time() - t_fault, 1)
            break
        time.sleep(0.5)
    entry["guard_after"] = nc.guard_state()
    entry["seconds_to_release"] = secs
    entry["sleep_left_on_hpsrv"] = nc.remote("pgrep -u ccrun -af 'sleep 45' | grep -v pgrep || true").strip()
    # Let the turn finish; watch for a second transport the whole time.
    seen_transports, dialogs = set(), []
    deadline = time.time() + 150
    while time.time() < deadline:
        seen_transports.update(local_transports())
        text = pane()
        d = dismiss_dialogs(text)
        if d:
            dialogs.append(d)
        if "Working (" not in text and "esc to interrupt" not in text and time.time() - t_fault > 15:
            break
        time.sleep(2)
    entry["transports_after_fault"] = sorted(seen_transports)
    entry["dialogs"] = dialogs
    time.sleep(3)
    entry["pane_tail"] = [l for l in pane().splitlines() if l.strip()][-25:]
    keys("/exit", "Enter")
    deadline = time.time() + 60
    while time.time() < deadline and subprocess.run([TMUX, "-L", "ccnm", "has-session", "-t", "ccnm-p24"],
                                                   capture_output=True).returncode == 0:
        time.sleep(1)
    time.sleep(2)
    session_dir = os.path.expanduser(f"~/.local/state/ccnm-p24/ccnm/sessions/{entry['session']}")
    try:
        entry["exit"] = json.load(open(os.path.join(session_dir, "exit")))
    except OSError:
        entry["exit"] = None
    rollout = subprocess.run(["find", os.path.join(session_dir, "codex-home", "sessions"), "-name", "*.jsonl"],
                             capture_output=True, text=True).stdout.split()
    text = open(rollout[0]).read() if rollout else ""
    calls = re.findall(r'"name":\s*"exec"', text)
    entry["exec_tool_calls"] = len(calls)
    entry["marker_in_any_tool_output"] = f"{mark}\\n" in text.replace(f"echo {mark}", "")
    entry["guard_final"] = nc.guard_state()
    entry["marked_left"] = nc.marked_processes()
    entry["ok"] = (entry["started"] and bool(running) and entry["guard_final"] == "released"
                   and not entry["marked_left"] and not entry["sleep_left_on_hpsrv"]
                   and len(entry["transports_after_fault"]) == 0 and not entry["marker_in_any_tool_output"])
    existing = json.load(open(out)) if os.path.exists(out) else {}
    existing[kind] = entry
    open(out, "w").write(json.dumps(existing, indent=2, ensure_ascii=False, default=str))
    print(json.dumps({k: entry[k] for k in ("kind", "ok", "command_running_on_hpsrv", "transports_before",
                                           "seconds_to_release", "sleep_left_on_hpsrv", "transports_after_fault",
                                           "exec_tool_calls", "marker_in_any_tool_output", "guard_final", "exit",
                                           "dialogs")}, ensure_ascii=False, default=str, indent=1))
    print("\n".join(entry["pane_tail"][-14:]))


if __name__ == "__main__":
    main()
