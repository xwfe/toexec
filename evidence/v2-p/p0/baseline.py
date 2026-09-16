#!/usr/bin/env python3
"""V2-P0: the direct-execution control for the Claude exec-server trial.

Builds the synthetic fixture the V2-P1 candidates will reuse, pins the versions
it ran with, and measures what the direct path (neutral MCP client ->
`ccnm internal mcp-serve`, coding) lets a command do and what it costs. The
probe set, repetitions and methods are frozen in
docs/plan/v2-p0-claude-trial-baseline.md; change them there first.

Zero model calls. Nothing real is read or written: every canary is a file this
script made, the only network target is a listener it opened on 127.0.0.1, and
HOME for the Runtime is a directory under the run.

usage: baseline.py <new run dir, NOT under /tmp or $TMPDIR> [--ccnm PATH] [--skip-128m]
"""
import argparse
import hashlib
import json
import os
import platform
import re
import socket
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import mcp_client as m  # noqa: E402

REPEAT = 5
TRUE_CALLS = 50
BIG_OUTPUT = 64 * 1024 * 1024
MARK = "p0-canary"


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def run(argv, **kw):
    return subprocess.run(argv, capture_output=True, text=True, **kw)


# ---------------------------------------------------------------- fixture

def build_fixture(base):
    ws = base / "workspace"
    (ws / "src").mkdir(parents=True)
    (ws / "src" / "app.py").write_text("print(sum(range(10)))\n")
    (ws / "README.md").write_text("synthetic V2-P0 workspace\n")
    data = ws / "data"
    data.mkdir()
    # G03 read samples; P1 reads them through both paths.
    (data / "empty.txt").write_bytes(b"")
    (data / "bom.txt").write_bytes(b"\xef\xbb\xbfbom line\n")
    (data / "crlf.txt").write_bytes(b"one\r\ntwo\r\n")
    (data / "no-newline.txt").write_bytes(b"last line without newline")
    (data / "invalid-utf8.txt").write_bytes(b"ok\n\xff\xfe broken\n")
    # A three-byte character straddling every 64 KiB and 1 MiB boundary.
    unit = ("a" * 65534 + "中") * 20
    (data / "multibyte.txt").write_text(unit + "\n", encoding="utf-8")
    (data / "long-line-8m.txt").write_bytes(b"y" * (8 * 1024 * 1024) + b"\n")
    with open(data / "lines-8m.txt", "wb") as f:
        line = 0
        while f.tell() < 8 * 1024 * 1024:
            f.write(f"{line:08d} {'x' * 70}\n".encode())
            line += 1
    run(["git", "init", "-q", str(ws)], check=True)
    run(["git", "-C", str(ws), "add", "-A"], check=True)
    run(["git", "-C", str(ws), "-c", "user.name=p0", "-c", "user.email=p0@invalid", "commit", "-qm", "fixture"], check=True)

    outside = base / "outside"
    outside.mkdir()
    (outside / "canary.txt").write_text(f"{MARK}-outside\n")
    runtime = m.write_runtime_config(base, ws)
    (runtime / "home" / ".p0-canary").write_text(f"{MARK}-home\n")
    return ws, outside, runtime


def big_lines_file(ws, size):
    path = ws / "data" / f"lines-{size // (1024 * 1024)}m.txt"
    if not path.exists():
        with open(path, "wb") as f:
            line = 0
            while f.tell() < size:
                f.write(f"{line:08d} {'x' * 70}\n".encode())
                line += 1
    return path


class Listener:
    """The only network target: records what each probe sent."""

    def __init__(self):
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(16)
        self.port = self.sock.getsockname()[1]
        self.got = []
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self):
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            conn.settimeout(3)
            try:
                self.got.append(conn.recv(256).decode(errors="replace").strip())
            except OSError:
                pass
            conn.close()


# ---------------------------------------------------------------- probes

def probes(ws, outside, home, tmpdir, port):
    """(id, kind, argv, verify) -- kind is `constraint` (a probe that a new OS
    constraint could block) or `legit` (ordinary project work that must keep
    working). verify(result_text, listener, round) -> bool: the side effect
    really happened."""
    def wrote(path, token):
        return lambda text, lst, n: path.exists() and path.read_text().strip() == token

    def said(token):
        return lambda text, lst, n: token in text

    tok = lambda pid, n: f"{MARK}-{pid}-{n}"  # noqa: E731
    return [
        ("W1-workspace-write", "legit",
         lambda n: ["/bin/sh", "-c", f"echo {tok('W1', n)} > {ws}/w1-{n}.txt"],
         lambda n: wrote(ws / f"w1-{n}.txt", tok("W1", n))),
        ("W2-outside-write", "constraint",
         lambda n: ["/bin/sh", "-c", f"echo {tok('W2', n)} > {outside}/w2-{n}.txt"],
         lambda n: wrote(outside / f"w2-{n}.txt", tok("W2", n))),
        ("W3-home-write", "constraint",
         lambda n: ["/bin/sh", "-c", f"echo {tok('W3', n)} > {home}/w3-{n}.txt"],
         lambda n: wrote(home / f"w3-{n}.txt", tok("W3", n))),
        ("W4-tmp-write", "constraint",
         lambda n: ["/bin/sh", "-c", f"echo {tok('W4', n)} > {tmpdir}/w4-{n}.txt"],
         lambda n: wrote(tmpdir / f"w4-{n}.txt", tok("W4", n))),
        ("W5-dotgit-write", "constraint",
         lambda n: ["/bin/sh", "-c", f"echo {tok('W5', n)} > {ws}/.git/w5-{n}"],
         lambda n: wrote(ws / ".git" / f"w5-{n}", tok("W5", n))),
        ("R1-outside-read", "constraint",
         lambda n: ["/bin/cat", str(outside / "canary.txt")],
         lambda n: said(f"{MARK}-outside")),
        ("R2-home-read", "constraint",
         lambda n: ["/bin/cat", str(home / ".p0-canary")],
         lambda n: said(f"{MARK}-home")),
        ("N1-loopback-connect", "constraint",
         lambda n: ["/bin/sh", "-c", f"printf {tok('N1', n)} | /usr/bin/nc -w 2 127.0.0.1 {port}"],
         lambda n: (lambda text, lst, r: tok("N1", n) in lst.got)),
        ("L1-run-project-code", "legit",
         lambda n: ["/usr/bin/python3", str(ws / "src" / "app.py")],
         lambda n: said("45")),
        ("L2-git-commit", "legit",
         lambda n: ["/bin/sh", "-c", f"cd {ws} && echo {n} >> README.md && git add README.md && "
                                     f"git -c user.name=p0 -c user.email=p0@invalid commit -qm r{n} && git log --oneline | wc -l"],
         lambda n: (lambda text, lst, r: run(["git", "-C", str(ws), "log", "-1", "--format=%s"]).stdout.strip() == f"r{n}")),
        ("L3-workspace-read", "legit",
         lambda n: ["/bin/cat", str(ws / "README.md")],
         lambda n: said("synthetic V2-P0 workspace")),
    ]


def exit_line(text):
    lines = text.splitlines()
    return lines[1] if len(lines) > 1 else ""


# ---------------------------------------------------------------- resource sampling

class RssSampler:
    """Peak RSS (KiB) of a process and its descendants, sampled every 50 ms."""

    def __init__(self, pid):
        self.pid, self.peak, self.stop = pid, 0, threading.Event()
        threading.Thread(target=self._run, daemon=True).start()

    def _tree(self):
        out = run(["ps", "-axo", "pid=,ppid=,rss="]).stdout
        rows = [tuple(int(x) for x in l.split()) for l in out.splitlines() if len(l.split()) == 3]
        kids, rss = {}, {}
        for pid, ppid, r in rows:
            kids.setdefault(ppid, []).append(pid)
            rss[pid] = r
        total, todo = 0, [self.pid]
        while todo:
            p = todo.pop()
            total += rss.get(p, 0)
            todo += kids.get(p, [])
        return total

    def _run(self):
        while not self.stop.is_set():
            self.peak = max(self.peak, self._tree())
            time.sleep(0.05)

    def done(self):
        self.stop.set()
        return self.peak


def measure(fn, session):
    sent, recv = session.sent_bytes, session.received_bytes
    sampler = RssSampler(session.proc.pid)
    load_before = os.getloadavg()[0]
    t0 = time.perf_counter()
    extra = fn()
    wall = time.perf_counter() - t0
    return {"wall_s": round(wall, 4), "load_before": round(load_before, 1), "load_after": round(os.getloadavg()[0], 1),
            "client_to_server_bytes": session.sent_bytes - sent,
            "server_to_client_bytes": session.received_bytes - recv, "peak_rss_kib": sampler.done(), **(extra or {})}


def page_through_file(session, rel):
    calls, start, lines = 0, 1, 0
    while True:
        text = session.call("read_file", {"path": rel, "start_line": start, "max_lines": 2000, "max_bytes": 65536})["content"][0]["text"]
        calls += 1
        more = re.search(r"continue with start_line=(\d+)", text)
        if not more:
            return {"calls": calls, "last_hint": text.splitlines()[-1][:120]}
        start = int(more.group(1))


def drain_output(session, ref):
    calls, offset = 0, 0
    while True:
        text = session.call("read_output", {"output_ref": ref, "stream": "stdout", "offset": offset, "limit": 32768})["content"][0]["text"]
        calls += 1
        more = re.search(r"continue with offset=(\d+)", text)
        if not more:
            return {"calls": calls, "last_hint": text.splitlines()[-1][:120]}
        offset = int(more.group(1))


# ---------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("out")
    ap.add_argument("--ccnm", default=str((HERE / "../../../../ccnm/target/release/ccnm").resolve()))
    ap.add_argument("--skip-128m", action="store_true")
    args = ap.parse_args()
    base = Path(args.out).resolve()
    for tmp in {"/tmp", "/private/tmp", os.path.realpath(tempfile.gettempdir())}:
        if str(base).startswith(os.path.realpath(tmp) + "/"):
            sys.exit(f"{base} is under {tmp}: sandboxes commonly allow writing there, so outside-workspace probes would prove nothing")
    base.mkdir(parents=True)
    ws, outside, runtime = build_fixture(base)
    home = runtime / "home"
    tmpdir = Path(tempfile.mkdtemp(prefix="v2-p0-", dir="/private/tmp"))
    listener = Listener()

    ccnm_rev = run(["git", "-C", str(HERE / "../../../../ccnm"), "rev-parse", "HEAD"]).stdout.strip()
    ccnm_dirty = bool(run(["git", "-C", str(HERE / "../../../../ccnm"), "status", "--porcelain", "--untracked-files=no"]).stdout.strip())
    codex = "/opt/homebrew/bin/codex"
    pins = {
        "date": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "platform": {"system": platform.system(), "release": platform.release(), "machine": platform.machine(),
                     "macos": platform.mac_ver()[0], "user": os.environ.get("USER")},
        "ccnm": {"path": args.ccnm, "sha256": sha256(args.ccnm), "repo_head": ccnm_rev, "repo_dirty": ccnm_dirty,
                 "version": run([args.ccnm, "--version"]).stdout.strip()},
        "codex_backend": {"path": os.path.realpath(codex), "sha256": sha256(os.path.realpath(codex)),
                          "version": run([codex, "--version"], env={"PATH": "/usr/bin:/bin", "HOME": str(home), "CODEX_HOME": str(home)}).stdout.strip(),
                          "note": "not used by the control; pinned here so P1 runs against the same file"},
        "python": sys.version.split()[0],
        "cpus": os.cpu_count(),
        "load_at_start": [round(x, 1) for x in os.getloadavg()],
        "tools": {t: run(["/bin/sh", "-c", f"command -v {t}"]).stdout.strip() for t in ("git", "nc", "python3", "rg")},
    }

    s = m.McpSession(args.ccnm, runtime)
    init = s.initialize()
    result = {"pins": pins, "server_info": init.get("serverInfo"), "paths": {
        "workspace": str(ws), "outside": str(outside), "home": str(home), "tmp": str(tmpdir), "listener_port": listener.port}}

    matrix = []
    for pid, kind, argv_for, verify_for in probes(ws, outside, home, tmpdir, listener.port):
        rounds = []
        for n in range(1, REPEAT + 1):
            text = s.call("exec_command", {"cmd": argv_for(n), "timeout_ms": 20000})["content"][0]["text"]
            time.sleep(0.05)
            rounds.append({"exit": exit_line(text), "effect": bool(verify_for(n)(text, listener, n))})
        matrix.append({"probe": pid, "kind": kind, "effect_rounds": sum(r["effect"] for r in rounds),
                       "rounds": REPEAT, "exit_lines": sorted({r["exit"].split(" in ")[0] for r in rounds})})
    result["probe_matrix"] = matrix

    # M1: process start round trip.
    rounds = []
    for _ in range(REPEAT):
        load_before = os.getloadavg()[0]
        walls = []
        for _ in range(TRUE_CALLS):
            t0 = time.perf_counter()
            s.call("exec_command", {"cmd": ["/usr/bin/true"]})
            walls.append((time.perf_counter() - t0) * 1000)
        walls.sort()
        rounds.append({"p50_ms": round(statistics.median(walls), 2), "p95_ms": round(walls[int(len(walls) * 0.95) - 1], 2),
                       "load_before": round(load_before, 1), "load_after": round(os.getloadavg()[0], 1)})
    result["m1_true_x50"] = {"rounds": rounds, "median_p50_ms": statistics.median(r["p50_ms"] for r in rounds),
                             "median_p95_ms": statistics.median(r["p95_ms"] for r in rounds)}

    # M2: 64 MiB of stdout; what the model sees, then everything.
    rounds = []
    for _ in range(REPEAT):
        holder = {}

        def big():
            text = s.call("exec_command", {"cmd": ["/bin/sh", "-c", f"head -c {BIG_OUTPUT} /dev/zero | tr '\\0' a"], "timeout_ms": 120000})["content"][0]["text"]
            holder["ref"] = re.search(r"output_ref (r-[0-9a-f]+)", text).group(1)
            return {"exit": exit_line(text)}

        preview = measure(big, s)
        drain = measure(lambda: drain_output(s, holder["ref"]), s)
        rounds.append({"preview": preview, "full_drain": drain})
    result["m2_stdout_64m"] = rounds

    # M3: page through files the way a tool consumer does.
    m3 = {}
    for rel in ("data/lines-8m.txt", "data/long-line-8m.txt"):
        m3[rel] = [measure(lambda: page_through_file(s, rel), s) for _ in range(REPEAT)]
    if not args.skip_128m:
        big_lines_file(ws, 128 * 1024 * 1024)
        m3["data/lines-128m.txt"] = [measure(lambda: page_through_file(s, "data/lines-128m.txt"), s)]
    result["m3_read_pages"] = m3

    result["mcp_serve_exit"] = s.close()
    result["mcp_serve_stderr_tail"] = s.stderr_tail(8)
    listener.sock.close()
    for p in tmpdir.glob("w4-*.txt"):
        p.unlink()
    tmpdir.rmdir()
    big = ws / "data" / "lines-128m.txt"
    if big.exists():
        big.unlink()

    (base / "baseline.json").write_text(json.dumps(result, indent=2, ensure_ascii=False))
    brief = {k: result[k] for k in ("probe_matrix", "m1_true_x50")}
    brief["m2_median_preview_wall_s"] = statistics.median(r["preview"]["wall_s"] for r in result["m2_stdout_64m"])
    brief["m2_median_drain_wall_s"] = statistics.median(r["full_drain"]["wall_s"] for r in result["m2_stdout_64m"])
    print(json.dumps(brief, indent=1, ensure_ascii=False))


if __name__ == "__main__":
    main()
