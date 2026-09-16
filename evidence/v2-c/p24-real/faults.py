#!/usr/bin/env python3
"""ccnm P24.3: fault injection on the exec-server chain, real ssh to hpsrv, no model.

Every iteration opens one exec-serve session with the neutral client, starts a
long sandboxed command, injects one fault and then watches hpsrv: what the
write-guard marker says, how long until it is released, and whether any process
carrying a session marker is left.

  agent-ssh      SIGKILL the ssh on this Mac                       (FIN reaches hpsrv)
  runtime-sshd   SIGKILL ccrun's sshd-session for this connection  (exec-serve loses its pipes)
  executor       SIGKILL `codex exec-server`                       (supervisor must sweep, then release)
  setsid         the command leaves a setsid child, then the client disconnects
  freeze         SIGSTOP this Mac's ssh for 60 s, then SIGCONT     (session must survive, guard stays)
  blackhole      root on hpsrv drops this one TCP connection both ways for 60 s (nftables,
                 removed by a systemd timer); the ssh here is killed inside that window, so its
                 FIN never arrives -- an Agent that vanished off the network. Then watch.

usage: faults.py <kind> <iterations> <out json>
"""

import json
import os
import signal
import subprocess
import sys
import time

import native_client as nc

results = []


def remote_root(script, timeout=60):
    out = subprocess.run(["/usr/bin/ssh", "-o", "BatchMode=yes", "-T", "hpsrv", "bash", "-s"], input=script,
                         capture_output=True, text=True, timeout=timeout)
    return out.stdout + out.stderr


def supervisor_pids(s):
    """(exec-serve pid, its sshd-session parent, codex exec-server child) on hpsrv for this session."""
    wire = nc.native_wire(s.session)
    text = nc.remote("ps -u ccrun -o pid=,ppid=,args=")
    serve = sshd = executor = None
    rows = [line.split(None, 2) for line in text.splitlines() if line.strip()]
    for pid, ppid, args in rows:
        if "internal exec-serve" in args and wire in args:
            serve, sshd = int(pid), int(ppid)
    for pid, ppid, args in rows:
        if serve and int(ppid) == serve and "exec-server" in args:
            executor = int(pid)
    return serve, sshd, executor


def wait_guard(predicate, timeout):
    start = time.time()
    state = nc.guard_state()
    while not predicate(state) and time.time() - start < timeout:
        time.sleep(0.5)
        state = nc.guard_state()
    return state, round(time.time() - start, 1)


def open_with_command(script="sleep 300"):
    s = nc.Session("exec-serve")
    init = s.handshake(timeout=40)
    assert init and "result" in init, (init, s.stderr.decode(errors="replace"))
    reply = s.call(nc.start(1, script))
    assert reply and "result" in reply, reply
    time.sleep(1)
    return s


def finish(entry, s, timeout=45):
    released, secs = wait_guard(lambda st: st == "released", timeout)
    entry["guard_after"] = released
    entry["seconds_to_release"] = secs
    entry["marked_left"] = nc.marked_processes()
    entry["client_ended"] = not s.alive()
    stderr = s.stderr.decode(errors="replace")
    entry["supervisor_swept"] = "left processes; killing them" in stderr
    entry["stderr_tail"] = stderr.strip().splitlines()[-2:]
    entry["ok"] = released == "released" and not entry["marked_left"] and entry.get("ok", True)
    if s.alive():
        s.close(timeout=10)


def one(kind, i):
    entry = {"kind": kind, "iteration": i}
    if kind == "setsid":
        s = open_with_command("setsid sh -c 'sleep 600' </dev/null >/dev/null 2>&1 & echo started")
        s.wait_exited("p1", 10)
    else:
        s = open_with_command()
    entry["session"] = s.session
    held = nc.guard_state()
    entry["guard_before"] = held
    entry["marked_before"] = len(nc.marked_processes())
    serve, sshd, executor = supervisor_pids(s)
    entry["pids"] = {"exec_serve": serve, "sshd_session": sshd, "executor": executor}
    ok = held.startswith(f"held {s.session} ") and serve and sshd and executor

    if kind == "agent-ssh":
        s.kill_local()
    elif kind == "runtime-sshd":
        nc.remote(f"kill -KILL {sshd}")
    elif kind == "executor":
        nc.remote(f"kill -KILL {executor}")
    elif kind == "supervisor":
        ok = ok and supervisor_killed(entry, s, serve)
    elif kind == "setsid":
        entry["setsid_child_seen"] = entry["marked_before"] >= 3
        s.proc.stdin.close()
    elif kind == "freeze":
        s.kill_local(signal.SIGSTOP)
        time.sleep(60)
        during = nc.guard_state()
        entry["guard_during_freeze"] = during
        entry["marked_during_freeze"] = len(nc.marked_processes())
        s.kill_local(signal.SIGCONT)
        time.sleep(2)
        reply, code, text = s.run(2, "echo alive-after-freeze", timeout=30)
        entry["after_freeze"] = {"exit": code, "output": text.strip()}
        ok = ok and during.startswith(f"held {s.session} ") and code == 0 and "alive-after-freeze" in text
        s.close()
    elif kind == "blackhole":
        ok = ok and blackhole(entry, s, sshd)
    entry["ok"] = bool(ok)
    finish(entry, s, timeout=45 if kind != "blackhole" else 60)
    results.append(entry)
    flag = "PASS" if entry["ok"] else "FAIL"
    print(f"{flag} {kind} #{i}: before={entry['guard_before'][:5]} after={entry['guard_after']} "
          f"in {entry['seconds_to_release']}s marked_left={len(entry['marked_left'])} swept={entry['supervisor_swept']}"
          + (f" held_after_blackhole={entry.get('held_after_blackhole_s')}" if kind == "blackhole" else ""))


def supervisor_killed(entry, s, serve):
    """SIGKILL `ccnm internal exec-serve` itself. Nothing may be released on its
    behalf: the guard must stay held, the next open must be refused as an
    interrupted holder, and only the operator's recovery -- prove the old
    processes gone, then remove that one marker (docs/operations.md) -- frees it."""
    nc.remote(f"kill -KILL {serve}")
    start = time.time()
    left = nc.marked_processes()
    while left and time.time() - start < 15:
        time.sleep(0.5)
        left = nc.marked_processes()
    entry["seconds_until_executor_processes_gone"] = round(time.time() - start, 1)
    entry["marked_after_supervisor_kill"] = left
    state = nc.guard_state()
    entry["guard_after_kill"] = state
    kept = state.startswith(f"held {s.session} ")
    probe = nc.Session("exec-serve")
    reply = probe.handshake(timeout=40)
    probe.proc.wait(timeout=30)
    said = probe.stderr.decode(errors="replace")
    entry["next_open"] = said.strip().splitlines()[:2]
    refused = not (reply and "result" in reply) and "left held by an interrupted process" in said
    entry["next_open_refused_as_interrupted"] = refused
    if left:
        return False
    nc.remote(f"grep -l '^held {s.session} p24' ~/.local/state/ccnm/write-guards/*.lock | xargs -r rm -f")
    again = nc.Session("exec-serve")
    reply = again.handshake(timeout=40)
    entry["open_after_recovery"] = bool(reply and "result" in reply)
    again.close()
    return kept and refused and entry["open_after_recovery"]


NFT = r"""set -euo pipefail
table=ccnm_p24_bh
nft list table inet $table >/dev/null 2>&1 && nft delete table inet $table
# The removal is scheduled before anything is added, so a failure halfway
# through can never leave a rule behind.
systemd-run --quiet --unit=ccnm-p24-bh-UNIT --on-active=60 /usr/sbin/nft delete table inet $table
nft add table inet $table
nft add chain inet $table input '{ type filter hook input priority -10; policy accept; }'
nft add chain inet $table output '{ type filter hook output priority -10; policy accept; }'
nft add rule inet $table input ip saddr PEER tcp sport PORT tcp dport 22 drop
nft add rule inet $table output ip daddr PEER tcp dport PORT tcp sport 22 drop
echo inserted
"""


def blackhole(entry, s, sshd_child):
    # The socket belongs to the privileged sshd-session, the parent of ccrun's.
    parent = remote_root(f"ps -o ppid= -p {sshd_child}").strip()
    ss = remote_root(f"ss -tnpH state established '( sport = :22 )' | grep 'pid={parent},'")
    try:
        peer = ss.split()[3]
        ip, port = peer.rsplit(":", 1)
    except (IndexError, ValueError):
        entry["blackhole_error"] = ss
        return False
    unit = f"{int(time.time())}"
    script = NFT.replace("PEER", ip).replace("PORT", port).replace("UNIT", unit)
    out = remote_root(script)
    entry["blackhole"] = {"peer_port": int(port), "inserted": "inserted" in out, "at": time.time()}
    if "inserted" not in out:
        entry["blackhole_error"] = out
        return False
    time.sleep(20)
    s.kill_local()            # its FIN and RST are dropped on hpsrv
    t_kill = time.time()
    while time.time() - entry["blackhole"]["at"] < 62:
        time.sleep(1)
    rule_gone = "no such" in remote_root("nft list table inet ccnm_p24_bh 2>&1 || true").lower() or \
        "does not exist" in remote_root("nft list table inet ccnm_p24_bh 2>&1 || true").lower()
    entry["rule_removed_by_timer"] = rule_gone
    # Watch for three minutes after the rule is gone: does the Runtime notice?
    watch_until = time.time() + 180
    state = nc.guard_state()
    while time.time() < watch_until and state != "released":
        time.sleep(5)
        state = nc.guard_state()
    entry["held_after_blackhole_s"] = round(time.time() - t_kill, 1) if state != "released" else None
    entry["released_on_its_own_within_watch"] = state == "released"
    if state != "released":
        # Recovery as the operations manual says: prove the old executor is gone
        # by ending the orphaned connection on the Runtime side.
        entry["recovery"] = "killed the orphaned sshd-session on hpsrv"
        nc.remote(f"kill -TERM {sshd_child}")
    return rule_gone


def main():
    kind, n, out = sys.argv[1], int(sys.argv[2]), sys.argv[3]
    for i in range(1, n + 1):
        try:
            one(kind, i)
        except Exception as exc:  # record, keep going: one broken iteration is a result too
            results.append({"kind": kind, "iteration": i, "ok": False, "error": repr(exc)})
            print(f"FAIL {kind} #{i}: {exc!r}")
            time.sleep(3)
    summary = {"kind": kind, "iterations": n, "passed": sum(bool(r.get("ok")) for r in results),
               "failed": sum(not r.get("ok") for r in results), "results": results}
    existing = json.load(open(out)) if os.path.exists(out) else {}
    existing[kind] = summary
    open(out, "w").write(json.dumps(existing, indent=2, ensure_ascii=False, default=str))
    print(json.dumps({k: summary[k] for k in ("kind", "iterations", "passed", "failed")}))


if __name__ == "__main__":
    main()
