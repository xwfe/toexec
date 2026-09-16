#!/usr/bin/env python3
"""ccnm P26.3, real binaries at default timings: a Codex that freezes.

The P23 chain with nothing injected: real Codex TUI (fake model, no-egress
Seatbelt) -> P23 relay_client.py -> real `ccnm internal exec-serve` (default
30 s / 10 min) -> real `codex exec-server`. Turn one leaves a long sleep
running; then every process of the TUI's tree gets SIGSTOP, which is what a
laptop going to sleep looks like from the Runtime: the connection stays open
and nothing answers. Records when exec-serve gives up, what it leaves, and
after SIGCONT what the person at the TUI sees on their next turn.

With a freeze length (seconds), SIGCONT comes after that long instead: a
Codex that is back within the limit must find its session still there.

usage: frozen_codex.py <new out dir> [freeze seconds]
"""
import json
import os
import shlex
import signal
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
SLEEP = "3594"


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


def pane(sock):
    return subprocess.run([TMUX, "-L", sock, "capture-pane", "-p", "-t", "s", "-S", "-200"],
                          capture_output=True, text=True).stdout


def prompt(sock, text):
    subprocess.run([TMUX, "-L", sock, "send-keys", "-t", "s", text], check=True)
    time.sleep(1)
    subprocess.run([TMUX, "-L", sock, "send-keys", "-t", "s", "Enter"], check=True)


def wait_step(model, step, timeout):
    deadline = time.time() + timeout
    while model.step < step and time.time() < deadline:
        time.sleep(0.5)
    return model.step >= step


def main():
    out = Path(sys.argv[1]).resolve()
    freeze_for = float(sys.argv[2]) if len(sys.argv) > 2 else None
    out.mkdir(parents=True)
    work = out / "work"
    work.mkdir()
    (out / "userhome").mkdir()
    log = Log(out / "harness.jsonl")
    config = h.write_runtime_config(out, work)
    relay = h.RuntimeRelay(out, log, config, str(uuid.uuid4()))
    home = h.write_session_home(out, work, h.PYTHON,
                                [str(HERE.parent / "p23-stdio" / "relay_client.py"), str(out), "basic", str(relay.port)])
    plan = [("exec", f"sleep {SLEEP}"), ("say", "turn one done"),
            ("exec", "echo after-resume"), ("say", "turn two done")]
    model = MockModel(out, plan, log)
    assert not sleeps(), "a sleep of the same length is already running"

    profile = out / "no-egress.sb"
    profile.write_text(SANDBOX_PROFILE)
    env = {"HOME": str(out / "userhome"), "CODEX_HOME": str(home), "PATH": os.environ["PATH"], "TERM": "xterm-256color"}
    inner = ["env", "-i"] + [f"{k}={v}" for k, v in env.items()]
    inner += ["sandbox-exec", "-f", str(profile), h.CODEX] + h.codex_args(model.port, work) + ["--", "run turn one"]
    sock = f"p26f-{os.getpid()}"
    (out / "local-cwd").mkdir()
    subprocess.run([TMUX, "-L", sock, "new-session", "-d", "-s", "s", "-x", "200", "-y", "60", "-c", str(out / "local-cwd"),
                    shlex.join(inner) + "; echo CODEX_EXITED=$?; sleep 1200"], check=True)
    pane_pid = int(subprocess.run([TMUX, "-L", sock, "list-panes", "-t", "s", "-F", "#{pane_pid}"],
                                  capture_output=True, text=True).stdout.strip())
    s = {}
    stopped = []
    try:
        s["turn_one"] = wait_step(model, 2, 90)
        time.sleep(5)
        s["sleep_running_before_freeze"] = sleeps()
        (out / "pane-before-freeze.txt").write_text(pane(sock))
        stopped = h.process_tree(pane_pid)
        for pid in stopped:
            os.kill(pid, signal.SIGSTOP)
        frozen_at = time.time()
        log(side="harness", event="sigstop", pids=stopped)
        s["frozen_pids"] = len(stopped)

        exec_serve = relay.children[0]
        limit = freeze_for if freeze_for is not None else 14 * 60
        while exec_serve.poll() is None and time.time() - frozen_at < limit:
            time.sleep(1)
        s["freeze_seconds"] = freeze_for
        s["exec_serve_exit_code"] = exec_serve.poll()
        s["exec_serve_ended_after_freeze_s"] = round(time.time() - frozen_at, 1)
        time.sleep(2)
        s["sleep_running_after_give_up"] = sleeps()
        guard_dir = out / "runtime" / "state" / "ccnm" / "write-guards"
        s["write_guards"] = {p.name: p.read_text() for p in sorted(guard_dir.glob("*.lock"))}

        for pid in stopped:
            try:
                os.kill(pid, signal.SIGCONT)
            except ProcessLookupError:
                pass
        stopped = []
        log(side="harness", event="sigcont")
        time.sleep(8)
        (out / "pane-after-resume.txt").write_text(pane(sock))
        prompt(sock, "run turn two")
        s["turn_two_model_steps"] = wait_step(model, 4, 90)
        time.sleep(6)
        final = pane(sock)
        (out / "pane-final.txt").write_text(final)
        s["pane_final_tail"] = [l for l in final.rstrip().splitlines() if l.strip()][-16:]
        s["runtime_connections"] = relay.connections
    finally:
        for pid in stopped:
            try:
                os.kill(pid, signal.SIGCONT)
            except ProcessLookupError:
                pass
        subprocess.run([TMUX, "-L", sock, "kill-server"], capture_output=True)
        s["exec_serve_exit_codes"] = relay.close()

    rows = [json.loads(l) for l in (out / "harness.jsonl").read_text().splitlines()]
    c2s = [r["t"] for r in rows if r.get("side") == "runtime-relay" and r.get("dir") == "c2s" and "msg" in r]
    pings = [r["t"] for r in rows if r.get("side") == "runtime-relay" and r.get("dir") == "s2c"
             and (r.get("msg") or {}).get("method") == "ccnm/liveness"]
    freeze = next(r["t"] for r in rows if r.get("event") == "sigstop")
    s["last_client_message_before_freeze_s"] = round(freeze - max(t for t in c2s if t <= freeze), 1)
    s["pings_while_frozen"] = len([t for t in pings if t >= freeze])
    s["ping_gaps_s"] = sorted({round(b - a) for a, b in zip(pings, pings[1:])})
    s["model_outputs_head"] = [(o or "")[:300] for o in h.model_outputs(out)]
    stderr = []
    for p in sorted(out.glob("exec-serve-*.stderr")):
        stderr += p.read_text(errors="replace").splitlines()[-5:]
    s["exec_serve_stderr_tail"] = stderr
    (out / "summary.json").write_text(json.dumps(s, indent=2, ensure_ascii=False, default=str))
    print(json.dumps(s, indent=1, ensure_ascii=False, default=str))


if __name__ == "__main__":
    main()
