#!/usr/bin/env python3
"""ccnm P29.2: many requests on one connection at once.

usage: concurrency.py <scenario> [rounds]

  pipelined   one burst of requests sent without waiting for any answer --
              sandboxed writes, reads, process starts, and requests ccnm must
              refuse (a write outside the workspace, an escalated command,
              http/request) -- while another command floods its output.
              Every line back must be whole JSON, every id answered exactly
              once, refused ones answered by ccnm with nothing on disk.
  same-path   two `fs/writeFile` to one path back to back, long and short
              content, order alternating. Is the file one of the two whole
              contents afterwards? (exec-server runs them concurrently)
"""
import sys
import time

import common as c

BURST = 40  # of each kind, so 7 * BURST requests per round
FLOOD_BYTES = 16 * 1024 * 1024


def pipelined(n):
    rt = c.Runtime(f"concurrency-pipelined-{n}")
    flood = {"bytes": 0}
    other_output = {}

    def on_output(params):
        import base64
        size = len(base64.b64decode(params["chunk"]))
        if params["processId"] == "flood":
            flood["bytes"] += size
        else:
            other_output[params["processId"]] = other_output.get(params["processId"], 0) + size

    s = c.Session(rt, on_output=on_output)
    s.handshake()
    (rt.work / "r.txt").write_bytes(b"read me\n" * 1000)
    flood_start = rt.start("flood", f"head -c {FLOOD_BYTES} /dev/zero", process_id="flood")
    s.send(flood_start)
    expect = {}  # id -> "forward" | "refuse"
    batch = []
    for i in range(BURST):
        batch.append(rt.write_file(f"w{i}", rt.work / f"w{i}.txt", f"write {i}\n".encode() * 50))
        expect[f"w{i}"] = "forward"
        batch.append(rt.read_file(f"r{i}", rt.work / "r.txt"))
        expect[f"r{i}"] = "forward"
        batch.append(rt.start(f"s{i}", f"echo {i} > s{i}.txt", process_id=f"s{i}"))
        expect[f"s{i}"] = "forward"
        batch.append(c.request(f"m{i}", "fs/getMetadata", path=rt.uri(rt.work / "r.txt"), sandbox=None))
        expect[f"m{i}"] = "forward"
        batch.append(rt.write_file(f"x{i}", rt.outside / f"x{i}.txt", b"outside\n"))
        expect[f"x{i}"] = "refuse"
        batch.append(rt.escalated(f"e{i}", f"echo escalated > {rt.outside}/e{i}.txt"))
        expect[f"e{i}"] = "refuse"
        batch.append({"id": f"h{i}", "method": "http/request",
                      "params": {"method": "GET", "url": "http://127.0.0.1:9/"}})
        expect[f"h{i}"] = "refuse"
    # Interleave the kinds so refusals and forwards alternate on the wire.
    for msg in batch:
        s.send(msg)
    deadline = time.time() + 120
    while time.time() < deadline:
        if all(k in s.replies for k in expect) and "flood" in s.replies:
            break
        time.sleep(0.05)
    s.wait_note("process/closed", "flood", timeout=120)
    for i in range(BURST):
        s.wait_note("process/closed", f"s{i}", timeout=30)
    rc = s.close(timeout=120)

    missing = [k for k in expect if k not in s.replies]
    duplicated = [k for k, v in s.replies.items() if len(v) != 1]
    wrong = []
    for k, kind in expect.items():
        reply = (s.replies.get(k) or [None])[0]
        if reply is None:
            continue
        if kind == "forward" and "result" not in reply:
            wrong.append({k: reply})
        if kind == "refuse" and not (reply.get("error", {}).get("code") == -32600
                                     and reply["error"]["message"].startswith("ccnm refused")):
            wrong.append({k: reply})
    written_ok = sum((rt.work / f"w{i}.txt").read_bytes() == f"write {i}\n".encode() * 50
                     for i in range(BURST) if (rt.work / f"w{i}.txt").exists())
    started_ok = sum((rt.work / f"s{i}.txt").exists() for i in range(BURST))
    outside = sorted(p.name for p in rt.outside.iterdir())
    return {"round": n, "requests": len(expect), "lines": s.lines, "bad_lines": len(s.bad_lines),
            "missing": missing, "duplicated": duplicated, "wrong": wrong[:5], "wrong_count": len(wrong),
            "writes_on_disk": written_ok, "starts_on_disk": started_ok, "outside_files": outside,
            "flood_bytes": flood["bytes"], "flood_expected": FLOOD_BYTES, "exit_code": rc,
            "guard_released": rt.guard_released(), "marked_left": c.marked()}


def same_path(n):
    rt = c.Runtime(f"concurrency-same-path-{n}")
    s = c.Session(rt)
    s.handshake()
    target = rt.work / "same.txt"
    long_data = b"L" * (8 * 1024 * 1024)
    short_data = b"S" * (64 * 1024)
    first, second = (long_data, short_data) if n % 2 else (short_data, long_data)
    s.send(rt.write_file("a", target, first))
    s.send(rt.write_file("b", target, second))
    ra, rb = s.wait("a", 60), s.wait("b", 60)
    final = target.read_bytes() if target.exists() else None
    rc = s.close(timeout=60)
    if final == long_data:
        outcome = "long"
    elif final == short_data:
        outcome = "short"
    elif final is None:
        outcome = "missing"
    else:
        outcome = "torn"
    detail = None
    if outcome == "torn":
        detail = {"size": len(final), "S": final.count(b"S"), "L": final.count(b"L"),
                  "zero": final.count(b"\0"), "head": final[:8].decode(errors="replace"),
                  "tail": final[-8:].decode(errors="replace")}
    return {"round": n, "first": "long" if first is long_data else "short",
            "replies_ok": bool(ra and "result" in ra and rb and "result" in rb),
            "reply_order": [k for k in s.replies if k in ("a", "b")],
            "outcome": outcome, "torn": detail, "exit_code": rc, "guard_released": rt.guard_released()}


def main():
    scenario = sys.argv[1]
    rounds = int(sys.argv[2]) if len(sys.argv) > 2 else 20
    fn = {"pipelined": pipelined, "same-path": same_path}[scenario]
    results = []
    for n in range(1, rounds + 1):
        results.append(fn(n))
        print(results[-1], flush=True)
    c.write_summary(f"concurrency-{scenario}", {"scenario": scenario, "rounds": rounds, "results": results})


if __name__ == "__main__":
    main()
