#!/usr/bin/env python3
"""ccnm P29.4: resource limits on the exec-server chain.

usage: resources.py <scenario> [rounds]

  output      a command writes 200 MiB to stdout; the client reads it all,
              except for a 60 s stretch in the middle where it stops reading.
              Bytes received, time, peak RSS of exec-serve and exec-server,
              and whether the command stood still while nobody read (5 rounds)
  readfile    `fs/readFile` of a 200 MiB file, with and without the sandbox;
              and a sparse file one byte over exec-server's 512 MiB limit (5 rounds)
  writelimit  `fs/writeFile` whose line is just under and just over ccnm's
              32 MiB client-message limit (1 round each; P22 already tests the
              over case offline)
  diskfull    the workspace on a 16 MiB disk image mounted by this user:
              a write that does not fit, a small write after it, a command
              filling the disk, and the guard at the end (5 rounds)
  expired     a fast command read back after it exited, and again after
              exec-server's 30 s retention; `fs/readBlock` after `fs/close` (5 rounds)
"""
import base64
import os
import shutil
import subprocess
import sys
import time

import common as c

MIB = 1024 * 1024


def output(n):
    rt = c.Runtime(f"resources-output-{n}")
    got = {"bytes": 0, "chunks": 0, "last_seq": 0, "seq_gaps": [], "streams": {}}

    def on_output(params):
        size = len(base64.b64decode(params["chunk"]))
        got["bytes"] += size
        got["chunks"] += 1
        got["streams"][params["stream"]] = got["streams"].get(params["stream"], 0) + size
        if params["seq"] != got["last_seq"] + 1 and len(got["seq_gaps"]) < 5:
            got["seq_gaps"].append({"after": got["last_seq"], "got": params["seq"], "at_mib": round(got["bytes"] / MIB, 1)})
        got["last_seq"] = params["seq"]

    s = c.Session(rt, on_output=on_output)
    s.handshake()
    sampler = c.RssSampler(s)
    progress = rt.work / "progress"
    script = ('i=0; while [ $i -lt 200 ]; do head -c 1048576 /dev/zero; i=$((i+1)); '
              'echo $i > progress; done')
    t = time.time()
    reply = s.call(rt.start(2, script, process_id="out"), timeout=15)
    assert reply and "result" in reply, reply
    while got["bytes"] < 50 * MIB and time.time() - t < 120:
        time.sleep(0.01)
    s.reading.clear()
    paused_at = time.time()
    time.sleep(2)  # let whatever was already in flight land
    p0 = progress.read_text().strip()
    sup_rss_0 = next((r for pid, _, r, _, _ in c.processes() if pid == s.proc.pid), 0)
    time.sleep(58)
    p1 = progress.read_text().strip()
    sup_rss_1 = next((r for pid, _, r, _, _ in c.processes() if pid == s.proc.pid), 0)
    bytes_at_pause = got["bytes"]
    s.reading.set()
    paused_s = round(time.time() - paused_at, 1)
    closed = s.wait_note("process/closed", "out", timeout=600)
    elapsed = round(time.time() - t - paused_s, 1)
    rss = sampler.finish()
    rc = s.close(timeout=60)
    return {"round": n, "bytes_received": got["bytes"], "expected": 200 * MIB, "chunks": got["chunks"],
            "seq_gaps": got["seq_gaps"], "streams": got["streams"],
            "exited_seq": s.wait_note("process/exited", "out", timeout=5)["params"]["seq"],
            "closed_note": bool(closed), "seconds_excluding_pause": elapsed,
            "mib_per_s": round(got["bytes"] / MIB / elapsed, 1) if elapsed else None,
            "pause_s": paused_s, "bytes_at_pause_mib": round(bytes_at_pause / MIB, 1),
            "command_progress_during_pause": [p0, p1],
            "supervisor_rss_kib_during_pause": [sup_rss_0, sup_rss_1], **rss,
            "exit_code": rc, "guard_released": rt.guard_released(), "marked_left": c.marked()}


def readfile(n):
    rt = c.Runtime(f"resources-readfile-{n}")
    big = rt.work / "big.bin"
    with open(big, "wb") as f:
        block = os.urandom(MIB)
        for _ in range(200):
            f.write(block)
    over = rt.work / "over.bin"
    with open(over, "wb") as f:
        f.truncate(512 * MIB + 1)  # sparse: no disk used
    out = {"round": n}
    for sandboxed in (True, False):
        s = c.Session(rt)
        s.handshake()
        sampler = c.RssSampler(s, interval=0.05)
        t = time.time()
        reply = s.call(rt.read_file(2, big, sandboxed=sandboxed), timeout=300)
        took = round(time.time() - t, 2)
        size = len(base64.b64decode(reply["result"]["dataBase64"])) if reply and "result" in reply else None
        reply = None
        s.replies.clear()
        rss = sampler.finish()
        over_reply = s.call(rt.read_file(3, over, sandboxed=sandboxed), timeout=60)
        rc = s.close(timeout=60)
        key = "sandboxed" if sandboxed else "unsandboxed"
        out[key] = {"size": size, "expected": 200 * MIB, "seconds": took, **rss,
                    "over_limit_reply": over_reply.get("error") if over_reply else None,
                    "exit_code": rc, "guard_released": rt.guard_released()}
    big.unlink()
    over.unlink()
    out["marked_left"] = c.marked()
    return out


def writelimit(n):
    rt = c.Runtime(f"resources-writelimit-{n}")
    out = {"round": n}
    for label, raw in (("under", 23 * MIB), ("over", 25 * MIB)):
        s = c.Session(rt)
        s.handshake()
        target = rt.work / f"{label}.bin"
        msg = rt.write_file(2, target, b"w" * raw)
        import json
        line_bytes = len(json.dumps(msg)) + 1
        t = time.time()
        reply = s.call(msg, timeout=120)
        took = round(time.time() - t, 2)
        rc = s.close(timeout=60) if s.proc.poll() is None else s.proc.returncode
        out[label] = {"raw_mib": raw // MIB, "line_mib": round(line_bytes / MIB, 2), "seconds": took,
                      "reply": (reply.get("result") if reply and "result" in reply else reply),
                      "file_size": target.stat().st_size if target.exists() else None, "exit_code": rc,
                      "stderr_tail": [l[-160:] for l in s.stderr().splitlines()[-2:]],
                      "guard_released": rt.guard_released()}
    out["marked_left"] = c.marked()
    return out


def diskfull(n):
    base = c.WORK / f"resources-diskfull-{n}-image"
    if base.exists():
        subprocess.run(["hdiutil", "detach", str(base / "vol"), "-force"], capture_output=True)
        shutil.rmtree(base)
    base.mkdir(parents=True)
    image = base / "small.dmg"
    subprocess.run(["hdiutil", "create", "-size", "16m", "-fs", "HFS+", "-volname", f"p29full{n}", str(image)],
                   check=True, capture_output=True)
    mount = base / "vol"
    mount.mkdir()
    subprocess.run(["hdiutil", "attach", str(image), "-mountpoint", str(mount), "-nobrowse"], check=True,
                   capture_output=True)
    try:
        root = mount / "work"
        root.mkdir()
        rt = c.Runtime(f"resources-diskfull-{n}", root=root)
        s = c.Session(rt)
        s.handshake()
        free_before = shutil.disk_usage(mount).free
        big = s.call(rt.write_file(2, root / "big.bin", b"b" * (20 * MIB)), timeout=60)
        partial = (root / "big.bin").stat().st_size if (root / "big.bin").exists() else None
        free_after_big = shutil.disk_usage(mount).free
        small = s.call(rt.write_file(3, root / "small.txt", b"small\n"), timeout=60)
        small_on_disk = (root / "small.txt").read_bytes() == b"small\n" if (root / "small.txt").exists() else False
        s.call(c.request(4, "fs/remove", path=rt.uri(root / "big.bin"), recursive=False, force=True,
                         sandbox=rt.sandbox()), timeout=60)
        fill = s.call(rt.start(5, "head -c 20000000 /dev/zero > fill.bin; echo rc=$?"), timeout=15)
        exited = s.wait_note("process/exited", "p5", timeout=60)
        s.wait_note("process/closed", "p5", timeout=60)
        fill_out = bytes(s.outputs.get("p5", b"")).decode(errors="replace")
        after = s.call(rt.write_file(6, root / "after.txt", b"x" * 1024), timeout=60)
        rc = s.close(timeout=60)
        return {"round": n, "free_before_mib": round(free_before / MIB, 1),
                "big_write_reply": big.get("error") or big.get("result"), "partial_file_bytes": partial,
                "free_after_big_mib": round(free_after_big / MIB, 1),
                "small_write_reply": small.get("error") or small.get("result"), "small_on_disk": small_on_disk,
                "fill_started": bool(fill and "result" in fill), "fill_exit": exited["params"] if exited else None,
                "fill_output": fill_out.strip()[-200:],
                "write_after_fill_reply": after.get("error") or after.get("result"),
                "exit_code": rc, "guard_released": rt.guard_released(), "marked_left": c.marked()}
    finally:
        subprocess.run(["hdiutil", "detach", str(mount), "-force"], capture_output=True)
        shutil.rmtree(base, ignore_errors=True)


def expired(n):
    rt = c.Runtime(f"resources-expired-{n}")
    s = c.Session(rt)
    s.handshake()
    s.call(rt.start(2, "printf 'héllo 多字节\\n'", process_id="fast"), timeout=15)
    closed = s.wait_note("process/closed", "fast", timeout=30)
    soon = s.call(c.request(3, "process/read", processId="fast", afterSeq=0, maxBytes=65536, waitMs=0), timeout=15)
    soon_text = b"".join(base64.b64decode(ch["chunk"]) for ch in soon["result"]["chunks"]).decode() \
        if soon and "result" in soon else None
    time.sleep(31)
    late = s.call(c.request(4, "process/read", processId="fast", afterSeq=0, maxBytes=65536, waitMs=0), timeout=15)
    late_terminate = s.call(c.request(5, "process/terminate", processId="fast"), timeout=15)
    (rt.work / "blocks.txt").write_bytes(b"0123456789" * 100)
    opened = s.call(c.request(6, "fs/open", handleId="h1", path=rt.uri(rt.work / "blocks.txt"), sandbox=None),
                    timeout=15)
    block = s.call(c.request(7, "fs/readBlock", handleId="h1", offset=10, len=10), timeout=15)
    closed_h = s.call(c.request(8, "fs/close", handleId="h1"), timeout=15)
    after_close = s.call(c.request(9, "fs/readBlock", handleId="h1", offset=0, len=10), timeout=15)
    rc = s.close(timeout=60)
    return {"round": n, "closed_note": bool(closed), "read_soon_text": soon_text,
            "read_after_retention": late.get("error") or late.get("result"),
            "terminate_after_retention": late_terminate.get("error") or late_terminate.get("result"),
            "open": opened.get("error") or opened.get("result"),
            "block": base64.b64decode(block["result"]["chunk"]).decode() if block and "result" in block else block,
            "close": closed_h.get("error") or closed_h.get("result"),
            "block_after_close": after_close.get("error") or after_close.get("result"),
            "exit_code": rc, "guard_released": rt.guard_released(), "marked_left": c.marked()}


def main():
    scenario = sys.argv[1]
    default = {"writelimit": 1}.get(scenario, 5)
    rounds = int(sys.argv[2]) if len(sys.argv) > 2 else default
    fn = {"output": output, "readfile": readfile, "writelimit": writelimit, "diskfull": diskfull,
          "expired": expired}[scenario]
    results = []
    for n in range(1, rounds + 1):
        results.append(fn(n))
        print(results[-1], flush=True)
    c.write_summary(f"resources-{scenario}", {"scenario": scenario, "rounds": rounds, "results": results})


if __name__ == "__main__":
    main()
