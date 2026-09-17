#!/usr/bin/env python3
"""ccnm P29.3: requests still in flight when the session ends.

usage: inflight.py <scenario> [rounds]

  client-leaves   `process/read` with a long waitMs on a silent command is in
                  flight when the client closes its end (20 rounds)
  helper-close    a sandboxed `fs/writeFile` to a FIFO in the workspace, so
                  exec-server's fs helper blocks; the client closes (20 rounds)
  helper-crash    the same helper, but exec-server itself is SIGKILLed; what
                  outlives it, and is the write guard released anyway (20 rounds)
  terminate       `process/terminate` on a running tree: children in the
                  command's process group, and one that left with setsid (5 rounds)

Each round records the time from the trigger to the supervisor's exit, the
write guard, marked processes, and the specific leftover being tested for.
"""
import os
import signal
import subprocess
import sys
import time

import common as c

SLEEP = "3587"  # an unusual length, so exact process-table checks find only ours


def sleeps(length=SLEEP):
    found = []
    for pid, _, _, comm, args in c.processes():
        words = args.split()
        if os.path.basename(comm) == "sleep" and words[1:] == [length]:
            found.append(pid)
    return found


def alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


def client_leaves(n):
    rt = c.Runtime(f"inflight-client-leaves-{n}")
    s = c.Session(rt)
    s.handshake()
    started = s.call(rt.start(2, f"exec sleep {SLEEP}"), timeout=15)
    assert started and "result" in started, started
    s.send(c.request(3, "process/read", processId="p2", afterSeq=0, maxBytes=65536, waitMs=600000))
    time.sleep(1.0)
    pending = 3 not in s.replies
    before = sleeps()
    t = time.time()
    rc = s.close(timeout=120)
    took = round(time.time() - t, 2)
    time.sleep(0.3)
    return {"round": n, "read_pending_at_close": pending, "sleep_running_before": len(before),
            "exit_code": rc, "close_to_exit_s": took, "guard_released": rt.guard_released(),
            "sleep_left": sleeps(), "marked_left": c.marked(), "homes_left": rt.homes_left()}


def helper_round(n, crash):
    name = "helper-crash" if crash else "helper-close"
    rt = c.Runtime(f"inflight-{name}-{n}")
    s = c.Session(rt)
    s.handshake()
    fifo = rt.work / "fifo"
    os.mkfifo(fifo)
    s.send(rt.write_file(2, fifo, b"written-after-release\n"))
    helper = None
    deadline = time.time() + 10
    while helper is None and time.time() < deadline:
        helper = next((p["pid"] for p in c.descendants(s.proc.pid)
                       if "--codex-run-as-fs-helper" in p["args"]), None)
        time.sleep(0.05)
    assert helper, "no fs helper appeared"
    executor = s.executor_pid()
    groups = subprocess.run(["ps", "-o", "pgid=", "-p", f"{executor},{helper}"], capture_output=True,
                            text=True).stdout.split()
    helper_marked = helper in c.marked()
    t = time.time()
    if crash:
        os.kill(executor, signal.SIGKILL)
        rc = s.proc.wait(timeout=120)
    else:
        rc = s.close(timeout=120)
    took = round(time.time() - t, 2)
    time.sleep(0.5)
    released = rt.guard_released()
    helper_alive = alive(helper)
    delivered = None
    if helper_alive:
        # Open the reading end: if the orphan still writes, the bytes arrive
        # here, after the guard said the session was over.
        fd = os.open(fifo, os.O_RDONLY | os.O_NONBLOCK)
        time.sleep(0.5)
        try:
            delivered = os.read(fd, 100).decode(errors="replace")
        except BlockingIOError:
            delivered = ""
        os.close(fd)
        time.sleep(0.3)
        if alive(helper):
            os.kill(helper, signal.SIGKILL)
    return {"round": n, "helper_pid_found": True, "helper_marked": helper_marked,
            "same_process_group": len(set(groups)) == 1, "exit_code": rc, "trigger_to_exit_s": took,
            "guard_released": released, "helper_alive_after_release": helper_alive,
            "helper_wrote_after_release": delivered, "marked_left": c.marked(),
            "reply_seen": 2 in s.replies}


def terminate(n):
    rt = c.Runtime(f"inflight-terminate-{n}")
    s = c.Session(rt)
    s.handshake()
    # A child and a grandchild in the command's group, and one process that
    # leaves with setsid (perl's POSIX::setsid, since macOS has no setsid(1)).
    script = (f"sleep {SLEEP} & (sleep {SLEEP}; true) & "
              f"perl -MPOSIX -e 'fork and exit; POSIX::setsid(); exec \"sleep\", \"{SLEEP}\"' ; wait")
    started = s.call(rt.start(2, script), timeout=15)
    assert started and "result" in started, started
    deadline = time.time() + 10
    while len(sleeps()) < 3 and time.time() < deadline:
        time.sleep(0.05)
    before = sleeps()
    groups = {}
    for pid in before:
        out = subprocess.run(["ps", "-o", "pgid=,sess=", "-p", str(pid)], capture_output=True, text=True).stdout
        groups[pid] = out.split()[0] if out.split() else None
    t = time.time()
    reply = s.call(c.request(3, "process/terminate", processId="p2"), timeout=15)
    gone_after = None
    while time.time() - t < 10:
        left = sleeps()
        if len(left) <= 1:
            gone_after = round(time.time() - t, 2)
            break
        time.sleep(0.05)
    exited = s.wait_note("process/exited", "p2", timeout=10)
    closed = s.wait_note("process/closed", "p2", timeout=10)
    escaped_during = sleeps()
    guard_during = rt.guards()
    t2 = time.time()
    rc = s.close(timeout=120)
    took = round(time.time() - t2, 2)
    time.sleep(0.3)
    return {"round": n, "sleeps_before": len(before), "distinct_groups_before": len(set(groups.values())),
            "terminate_reply": reply.get("result") if reply else None,
            "group_gone_within_s": gone_after, "exited_note": bool(exited),
            "exit_code_note": exited["params"].get("exitCode") if exited else None, "closed_note": bool(closed),
            "escaped_still_running_in_session": len(escaped_during), "guard_during": list(guard_during.values()),
            "session_exit_code": rc, "close_to_exit_s": took, "guard_released": rt.guard_released(),
            "sleep_left": sleeps(), "marked_left": c.marked()}


def main():
    scenario = sys.argv[1]
    rounds = int(sys.argv[2]) if len(sys.argv) > 2 else (5 if scenario == "terminate" else 20)
    assert not sleeps(), "a sleep of the same length is already running"
    fn = {"client-leaves": client_leaves, "helper-close": lambda n: helper_round(n, False),
          "helper-crash": lambda n: helper_round(n, True), "terminate": terminate}[scenario]
    results = []
    for n in range(1, rounds + 1):
        results.append(fn(n))
        print(results[-1], flush=True)
    c.write_summary(f"inflight-{scenario}", {"scenario": scenario, "rounds": rounds, "results": results})


if __name__ == "__main__":
    main()
