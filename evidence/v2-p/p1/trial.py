#!/usr/bin/env python3
"""V2-P1: run the frozen comparison of docs/plan/v2-p0-claude-trial-baseline.md.

D (direct: neutral MCP client -> ccnm mcp-serve), A-proc / A-read (neutral
exec-server client -> ccnm exec-serve -> codex exec-server with the captured
workspace-write sandbox), S (`codex sandbox` with the same permissions object,
no RPC), N (raw `codex exec-server`, sandbox null, no ccnm; timing only, and
only when A-proc has a new constraint), and L (the argv spawned locally with no
wrapper at all, the floor S and N are compared against).

Probes and measurements are interleaved path by path, round by round, and every
measurement block records the load average (the P0 amendment). Zero model calls.

usage: trial.py <new run dir, NOT under /tmp> [--skip-128m]
"""
import argparse
import json
import os
import re
import statistics
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "p0"))
import baseline as p0  # noqa: E402
import exec_client as x  # noqa: E402
import mcp_client as m  # noqa: E402

CCNM = str((HERE / "../../../../ccnm/target/release/ccnm").resolve())
CCNM_SHA_P0 = "97a93d0b9fe4cb50fa9505259c7bf002c2ea47c95abc5053385190bea8e76440"
CODEX = "/opt/homebrew/Caskroom/codex/0.154.0/bin/codex"
CODEX_SHA_P0 = "4f85982624b3898c8991cb80c0981b2aa71070e3537046c9a95950318a95afcc"
REPEAT = 5
TRUE_CALLS = 50
PAGE = 64 * 1024
CREDENTIAL_LIKE = re.compile(r"TOKEN|SECRET|PASSWORD|PASSWD|API_?KEY|AUTH|ANTHROPIC|CLAUDE|OPENAI|SSH_AUTH_SOCK|AWS_|GITHUB_", re.I)


def load():
    return round(os.getloadavg()[0], 1)


def pct(values, q):
    values = sorted(values)
    return round(values[max(0, int(len(values) * q) - 1)], 2)


class RawExec(x.ExecSession):
    """N: `codex exec-server --listen stdio` with no ccnm in front."""

    def __init__(self, root, home):  # noqa: super().__init__ is replaced on purpose
        import threading
        self.session = f"n-{uuid.uuid4().hex[:8]}"
        self.root = Path(root)
        codex_home = Path(home) / ".codex-n"
        codex_home.mkdir(exist_ok=True)
        self.stderr_path = codex_home / "exec-server.stderr"
        self.proc = subprocess.Popen([CODEX, "exec-server", "--listen", "stdio"],
                                     env={"PATH": os.environ["PATH"], "HOME": str(home), "CODEX_HOME": str(codex_home)},
                                     cwd=str(root), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=open(self.stderr_path, "wb"))
        self.sent_bytes = self.received_bytes = 0
        self.next_id = 1
        self.replies, self.cv = {}, threading.Condition()
        self.proc_state, self.eof = {}, False
        threading.Thread(target=self._read, daemon=True).start()


# ---------------------------------------------------------------- path adapters

class Paths:
    def __init__(self, base, skip_128m):
        self.base = base
        self.ws, self.outside, self.runtime = p0.build_fixture(base)
        self.home = self.runtime / "home"
        self.tmpdir = Path(tempfile.mkdtemp(prefix="v2-p1-", dir="/private/tmp"))
        self.listener = p0.Listener()
        (self.ws / "escape").symlink_to(self.outside)
        subprocess.run(["git", "-C", str(self.ws), "add", "-A"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(self.ws), "-c", "user.name=p1", "-c", "user.email=p1@invalid", "commit", "-qm", "symlink"],
                       check=True, capture_output=True)
        self.native_runtime = x.write_native_config(base, self.ws, self.home, CODEX)
        self.d = m.McpSession(CCNM, self.runtime)
        self.d.initialize()
        self.a = x.ExecSession(CCNM, self.native_runtime, self.home, self.ws)
        self.a.initialize()
        self.skip_128m = skip_128m

    def exec_d(self, argv, timeout=120):
        text = self.d.call("exec_command", {"cmd": argv, "timeout_ms": timeout * 1000})["content"][0]["text"]
        return text, p0.exit_line(text)

    def exec_a(self, argv, timeout=120):
        reply, code, data, _ = self.a.run(argv, timeout=timeout)
        if "error" in reply:
            return json.dumps(reply), f"refused {reply['error'].get('code')}"
        return data.decode(errors="replace"), f"exit {code}"

    def exec_s(self, argv, timeout=120):
        r = x.run_s(CODEX, self.ws, self.home, argv, timeout=timeout)
        return (r.stdout + r.stderr).decode(errors="replace"), f"exit {r.returncode}"


# ---------------------------------------------------------------- phases

def probes(P):
    runners = {"D": P.exec_d, "A": P.exec_a, "S": P.exec_s}
    rows = []
    for pid, kind, argv_for, verify_for in p0.probes(P.ws, P.outside, P.home, P.tmpdir, P.listener.port):
        row = {"probe": pid, "kind": kind}
        for path in runners:
            row[path] = {"effect_rounds": 0, "exits": set(), "first_block_evidence": None}
        for n in range(1, p0.REPEAT + 1):
            for path, runner in runners.items():
                key = f"{path}{n}"
                text, exit_line = runner(argv_for(key))
                time.sleep(0.05)
                ok = bool(verify_for(key)(text, P.listener, key))
                cell = row[path]
                cell["effect_rounds"] += ok
                cell["exits"].add(exit_line.split(" in ")[0])
                if not ok and cell["first_block_evidence"] is None:
                    lines = [l for l in text.splitlines() if re.search(r"not permitted|denied|refused|failed|error|fatal", l, re.I)]
                    cell["first_block_evidence"] = (lines[0] if lines else text.strip().splitlines()[-1] if text.strip() else "")[:240]
        for path in runners:
            row[path]["exits"] = sorted(row[path]["exits"])
        rows.append(row)
        print("probe", pid, {p: row[p]["effect_rounds"] for p in runners}, flush=True)
    return rows


def rpc_read_probes(P):
    """A-read against ccnm's read contract, next to D's read_file on the same paths."""
    cases = {
        "outside-absolute": str(P.outside / "canary.txt"),
        "parent-components": "../outside/canary.txt",
        "symlink-escape": "escape/canary.txt",
        "dotgit-config": ".git/config",
        "inside": "README.md",
    }
    rows = []
    for name, rel in cases.items():
        d_text = P.d.call("read_file", {"path": rel})
        d_text = d_text["content"][0]["text"]
        uri = f"file://{rel}" if rel.startswith("/") else f"file://{P.ws}/{rel}"
        a_reply = P.a.open_raw(uri)
        rows.append({"case": name, "path": rel,
                     "D": d_text.splitlines()[0][:200] if d_text else "",
                     "D_allowed": "CCNM_E_" not in d_text.splitlines()[0] if d_text else False,
                     "A": (a_reply.get("error") or {"ok": True}),
                     "A_allowed": "result" in a_reply})
    return rows


def env_compare(P):
    names = {}
    for path, runner in (("D", P.exec_d), ("A", P.exec_a), ("S", P.exec_s)):
        text, _ = runner(["/usr/bin/env"])
        names[path] = sorted({l.split("=", 1)[0] for l in text.splitlines() if "=" in l and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", l)})
    out = {}
    for path in ("A", "S"):
        extra = sorted(set(names[path]) - set(names["D"]))
        out[path] = {"extra_names": extra, "missing_names": sorted(set(names["D"]) - set(names[path])),
                     "extra_credential_like": [n for n in extra if CREDENTIAL_LIKE.search(n)]}
    out["D_credential_like"] = [n for n in names["D"] if CREDENTIAL_LIKE.search(n)]
    return out


def m1(P, with_n):
    local_env = {"PATH": os.environ["PATH"], "HOME": str(P.home)}
    n_session = None
    if with_n:
        n_session = RawExec(P.ws, P.home)
        n_session.initialize()
    rounds = []
    for r in range(REPEAT):
        row = {"load_before": load()}
        for path in ("D", "A", "S", "N", "L"):
            if path == "N" and not n_session:
                continue
            walls = []
            for _ in range(TRUE_CALLS):
                t0 = time.perf_counter()
                if path == "D":
                    P.d.call("exec_command", {"cmd": ["/usr/bin/true"]})
                elif path == "A":
                    P.a.run(["/usr/bin/true"])
                elif path == "S":
                    x.run_s(CODEX, P.ws, P.home, ["/usr/bin/true"])
                elif path == "N":
                    n_session.run(["/usr/bin/true"], sandbox=False)
                else:
                    subprocess.run(["/usr/bin/true"], env=local_env, cwd=str(P.ws))
                walls.append((time.perf_counter() - t0) * 1000)
            row[path] = {"p50_ms": round(statistics.median(walls), 2), "p95_ms": pct(walls, 0.95)}
        row["load_after"] = load()
        rounds.append(row)
        print("M1 round", r, {k: v for k, v in row.items() if isinstance(v, dict)}, flush=True)
    if n_session:
        n_session.close()
    summary = {}
    for path in rounds[0]:
        if isinstance(rounds[0][path], dict):
            summary[path] = {"median_p50_ms": statistics.median(x_[path]["p50_ms"] for x_ in rounds),
                             "median_p95_ms": statistics.median(x_[path]["p95_ms"] for x_ in rounds)}
    return {"rounds": rounds, "summary": summary}


def m2(P):
    cmd = ["/bin/sh", "-c", f"head -c {p0.BIG_OUTPUT} /dev/zero | tr '\\0' a"]
    rounds = []
    for r in range(REPEAT):
        row = {}
        holder = {}

        def d_preview():
            text, exit_line = P.exec_d(cmd)
            holder["ref"] = re.search(r"output_ref (r-[0-9a-f]+)", text).group(1)
            return {"exit": exit_line}

        row["D_preview"] = p0.measure(d_preview, P.d)
        row["D_drain"] = p0.measure(lambda: p0.drain_output(P.d, holder["ref"]), P.d)

        def a_all():
            reply, code, _, nbytes = P.a.run(cmd, keep_output=False)
            return {"exit": code, "output_bytes": nbytes}

        row["A_all"] = p0.measure(a_all, P.a)
        lb = load()
        t0 = time.perf_counter()
        s = x.run_s(CODEX, P.ws, P.home, cmd)
        row["S_local"] = {"wall_s": round(time.perf_counter() - t0, 4), "output_bytes": len(s.stdout), "load_before": lb}
        lb = load()
        t0 = time.perf_counter()
        loc = subprocess.run(cmd, capture_output=True, cwd=str(P.ws))
        row["L_local"] = {"wall_s": round(time.perf_counter() - t0, 4), "output_bytes": len(loc.stdout), "load_before": lb}
        rounds.append(row)
        print("M2 round", r, {k: v.get("wall_s") for k, v in row.items()}, flush=True)
    return rounds


def a_pages(P, rel, size_hint):
    pages, offset, got = 0, 0, 0
    while True:
        _, data, eof = P.a.read_range(rel, offset, PAGE)
        pages += 1
        got += len(data)
        offset += len(data)
        if eof or not data:
            return {"calls": pages * 3, "pages": pages, "bytes_read": got}


def m3(P):
    out = {}
    for rel in ("data/lines-8m.txt", "data/long-line-8m.txt"):
        rows = []
        for _ in range(REPEAT):
            d = p0.measure(lambda: p0.page_through_file(P.d, rel), P.d)
            a = p0.measure(lambda: a_pages(P, rel, 0), P.a)
            rows.append({"D": d, "A": a})
        out[rel] = rows
        print("M3", rel, [(r["D"]["wall_s"], r["A"]["wall_s"]) for r in rows], flush=True)
    if not P.skip_128m:
        p0.big_lines_file(P.ws, 128 * 1024 * 1024)
        rel = "data/lines-128m.txt"
        d = p0.measure(lambda: p0.page_through_file(P.d, rel), P.d)
        a = p0.measure(lambda: a_pages(P, rel, 0), P.a)
        out[rel] = [{"D": d, "A": a}]
        (P.ws / rel).unlink()
        print("M3", rel, d["wall_s"], a["wall_s"], flush=True)
    return out


def g03_bytes(P):
    """A-read returns the file's bytes; D returns its rendered read_file text."""
    rows = []
    for name in ("empty.txt", "bom.txt", "crlf.txt", "no-newline.txt", "invalid-utf8.txt", "multibyte.txt"):
        rel = f"data/{name}"
        disk = (P.ws / rel).read_bytes()
        got, offset = b"", 0
        while True:
            _, data, eof = P.a.read_range(rel, offset, PAGE)
            got += data
            offset += len(data)
            if eof or not data:
                break
        d_text = P.d.call("read_file", {"path": rel, "max_lines": 2000, "max_bytes": 65536})["content"][0]["text"]
        rows.append({"file": name, "A_bytes_equal_disk": got == disk, "A_bytes": len(got), "disk_bytes": len(disk),
                     "D_first_line": d_text.splitlines()[0][:80] if d_text else "", "D_last_line": d_text.splitlines()[-1][:120] if d_text else ""})
    return rows


def leftovers(session_prefix):
    out = subprocess.run(["ps", "-axEww", "-o", "pid=,command="], capture_output=True, text=True, errors="replace").stdout
    return [l.split()[0] for l in out.splitlines() if f"CCNM_EXEC_SESSION={session_prefix}-" in l]


def guards(runtime):
    d = Path(runtime) / "state" / "ccnm" / "write-guards"
    return sorted(p.read_text() for p in d.glob("*.lock")) if d.exists() else []


# ---------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("out")
    ap.add_argument("--skip-128m", action="store_true")
    args = ap.parse_args()
    base = Path(args.out).resolve()
    if str(base).startswith("/private/tmp/") or str(base).startswith("/tmp/") or str(base).startswith(os.path.realpath(tempfile.gettempdir())):
        sys.exit("run dir must not be under a temporary directory")
    base.mkdir(parents=True)
    pins = {"ccnm_sha256": p0.sha256(CCNM), "codex_sha256": p0.sha256(CODEX)}
    if pins["ccnm_sha256"] != CCNM_SHA_P0 or pins["codex_sha256"] != CODEX_SHA_P0:
        sys.exit(f"binaries differ from the P0 pins: {pins}; rerun P0 first")
    result = {"date": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "pins": pins, "cpus": os.cpu_count(), "load_at_start": load()}
    P = Paths(base, args.skip_128m)
    result["paths"] = {"workspace": str(P.ws), "outside": str(P.outside), "home": str(P.home), "tmp": str(P.tmpdir)}
    try:
        result["probes"] = probes(P)
        result["rpc_read_probes"] = rpc_read_probes(P)
        result["env"] = env_compare(P)
        constraint_ids = {"W2-outside-write", "W3-home-write", "W5-dotgit-write", "R1-outside-read", "R2-home-read", "N1-loopback-connect"}
        a_new = [r["probe"] for r in result["probes"] if r["probe"] in constraint_ids
                 and r["D"]["effect_rounds"] == 5 and r["A"]["effect_rounds"] == 0]
        result["m1"] = m1(P, with_n=bool(a_new))
        result["m2"] = m2(P)
        result["m3"] = m3(P)
        result["g03"] = g03_bytes(P)
    finally:
        result["d_close"] = P.d.close()
        a_session = P.a.session
        result["a_close"] = P.a.close()
        time.sleep(1)
        result["after_close"] = {"a_leftover_pids": leftovers(a_session), "a_guards": guards(P.native_runtime),
                                 "d_guards": guards(P.runtime), "a_stderr_tail": P.a.stderr_tail(8)}
        P.listener.sock.close()
        for f in P.tmpdir.glob("*"):
            f.unlink()
        P.tmpdir.rmdir()
    (base / "p1.json").write_text(json.dumps(result, indent=2, ensure_ascii=False, default=list))
    print(json.dumps({"probes": [{"probe": r["probe"], **{p: r[p]["effect_rounds"] for p in ("D", "A", "S")}} for r in result["probes"]],
                      "m1": result["m1"]["summary"], "after_close": result["after_close"], "env": result["env"]},
                     indent=1, ensure_ascii=False))


if __name__ == "__main__":
    main()
