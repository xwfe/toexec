#!/usr/bin/env python3
"""Shared pieces for the ccnm P29 measurements. Standard library only.

One machine, zero model spend: the real `ccnm internal exec-serve` (wire
protocol 6) in front of the real `codex exec-server --listen stdio`, driven by
a neutral JSON-RPC client that imports nothing from ccnm or Codex. Requests
are Codex 0.154.0's own, captured in ccnm P21
(`ccnm/tests/fixtures/codex-0.154.0/exec-server/`), respelled for the
temporary workspace. There is no ssh hop: the client is exec-serve's stdin and
stdout, which is what ssh hands it on a Runtime.

Working directories go under `work/` (ignored by git), not the system temp
dir: exec-server refuses to create its helper links there (ccnm P21), and on
macOS /tmp is a symlink the credential audit reads as unknown.

env: P29_CCNM (default ../../../../ccnm/target/release/ccnm), P29_CODEX,
     P29_WORK (default ./work)
"""
import base64
import json
import os
import shutil
import subprocess
import threading
import time
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
CCNM_REPO = Path(os.environ.get("P29_CCNM_REPO", HERE.parents[3] / "ccnm"))
CCNM = os.environ.get("P29_CCNM", str(CCNM_REPO / "target/release/ccnm"))
CODEX = os.environ.get("P29_CODEX", "/opt/homebrew/bin/codex")
FIXTURES = CCNM_REPO / "tests/fixtures/codex-0.154.0/exec-server"
WORK = Path(os.environ.get("P29_WORK", HERE / "work")).resolve()
IDENTITY = {"node": "agent", "instance": "codex-main", "provider": "codex", "profile_ref": "default"}
MARKER = "CCNM_EXEC_SESSION"


def b64url(obj):
    return base64.urlsafe_b64encode(json.dumps(obj, separators=(",", ":")).encode()).decode().rstrip("=")


def b64(data):
    return base64.b64encode(data).decode()


def versions():
    ccnm = subprocess.run([CCNM, "--version"], capture_output=True, text=True).stdout.strip()
    codex = subprocess.run([CODEX, "--version"], capture_output=True, text=True).stdout.strip()
    commit = subprocess.run(["git", "-C", str(CCNM_REPO), "rev-parse", "--short", "HEAD"],
                            capture_output=True, text=True).stdout.strip()
    return {"ccnm": ccnm, "ccnm_commit": commit, "ccnm_bin": CCNM, "codex": codex,
            "platform": subprocess.run(["uname", "-srm"], capture_output=True, text=True).stdout.strip()}


class Runtime:
    """A Runtime Node's config and state for one workspace `demo`."""

    def __init__(self, name, root=None):
        self.dir = WORK / name
        if self.dir.exists():
            shutil.rmtree(self.dir)
        (self.dir / "runtime" / "home").mkdir(parents=True)
        (self.dir / "runtime" / "state").mkdir()
        self.work = Path(root).resolve() if root else self.dir / "work"
        self.work.mkdir(exist_ok=True)
        self.outside = self.dir / "outside"
        self.outside.mkdir()
        self.config = self.dir / "runtime" / "config.toml"
        # `allow_unconfined_exec` and a credential-free HOME are what ccnm's
        # exec_serve integration tests use too: the developer account running
        # this would otherwise fail the audit.
        self.config.write_text(f'''this = "runtime"

[nodes.runtime]
codex_bin = "{CODEX}"

[nodes.agent]
ssh = "agent-node.invalid"

[workspaces.demo]
root = "{self.work}"
agent = {{ node = "agent", instance = "codex-main" }}
allow_unconfined_exec = true
codex_exec_server = true
''')

    def env(self):
        return {"PATH": os.environ["PATH"], "HOME": str(self.dir / "runtime" / "home"),
                "XDG_STATE_HOME": str(self.dir / "runtime" / "state"), "CCNM_CONFIG": str(self.config),
                "CCNM_LOG": "info"}

    def guards(self):
        """Write-guard marker files: 'released', or 'held <session> <workspace>'."""
        folder = self.dir / "runtime" / "state" / "ccnm" / "write-guards"
        return {p.name: p.read_text().strip() for p in sorted(folder.glob("*.lock"))}

    def guard_released(self):
        guards = self.guards()
        return bool(guards) and all(v == "released" for v in guards.values())

    def homes_left(self):
        folder = self.dir / "runtime" / "state" / "ccnm" / "exec-server"
        return sorted(p.name for p in folder.glob("*")) if folder.exists() else []

    def uri(self, path):
        return f"file://{path}"

    def fixture(self, name, id_, **extra):
        text = ((FIXTURES / name).read_text().replace("{ROOT}", str(self.work))
                .replace("{OUTSIDE}", str(self.outside)).replace("{SERVER_HOME}", str(self.dir / "runtime" / "home")))
        msg = json.loads(text)
        msg["id"] = id_
        params = msg.get("params", {})
        # The captured attribution was replaced by placeholders, and the real
        # executor requires a UUID thread id; the field is optional.
        params.pop("metadata", None)
        if "env" in params:
            params["env"] = {}
        params.update(extra)
        return msg

    def sandbox(self):
        return self.fixture("fs-write-file-workspace-write.json", 0)["params"]["sandbox"]

    def start(self, id_, script, process_id=None):
        return self.fixture("process-start-workspace-write.json", id_, processId=process_id or f"p{id_}",
                            argv=["/bin/sh", "-c", script])

    def escalated(self, id_, script):
        """What Codex sends once a person approves an escalation: no sandbox."""
        msg = self.start(id_, script)
        msg["params"]["sandbox"] = None
        return msg

    def write_file(self, id_, path, data):
        return self.fixture("fs-write-file-workspace-write.json", id_, path=self.uri(path), dataBase64=b64(data))

    def read_file(self, id_, path, sandboxed=True):
        """apply_patch reads with the sandbox; the startup AGENTS.md read has none (P21)."""
        return {"id": id_, "method": "fs/readFile",
                "params": {"path": self.uri(path), "sandbox": self.sandbox() if sandboxed else None}}


def request(id_, method, **params):
    return {"id": id_, "method": method, "params": params}


class Session:
    """One `ccnm internal exec-serve`, spoken to over its stdin and stdout.

    Answers ccnm's liveness requests the way Codex 0.154.0 does (-32601).
    `on_output(params)` sees each `process/output` notification; without it
    the decoded bytes are kept per process id. `reading` can be cleared to
    stop taking bytes from the supervisor, like a client that stopped reading.
    """

    def __init__(self, rt, on_output=None, keep_notes=True):
        self.rt = rt
        self.session = str(uuid.uuid4())
        wire = b64url({"protocol": 6, "workspace": "demo", "agent": IDENTITY, "session": self.session})
        self.stderr_path = rt.dir / f"exec-serve-{self.session[:8]}.stderr"
        self.proc = subprocess.Popen([CCNM, "internal", "exec-serve", "--payload", wire], env=rt.env(),
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=open(self.stderr_path, "wb"))
        self.t0 = time.time()
        self.cv = threading.Condition()
        self.wlock = threading.Lock()
        self.replies = {}
        self.notes = []
        self.outputs = {}
        self.lines = 0
        self.bad_lines = []
        self.pings = 0
        self.eof = False
        self.on_output = on_output
        self.keep_notes = keep_notes
        self.reading = threading.Event()
        self.reading.set()
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        stream = self.proc.stdout
        while True:
            self.reading.wait()
            line = stream.readline()
            if not line:
                break
            self.lines += 1
            try:
                msg = json.loads(line)
            except ValueError:
                self.bad_lines.append(line[:200].decode(errors="replace"))
                continue
            method = msg.get("method")
            if method == "ccnm/liveness":
                self.pings += 1
                self.send({"id": msg["id"], "error": {"code": -32601, "message": "Method not found"}})
            elif method == "process/output":
                params = msg["params"]
                if self.on_output:
                    self.on_output(params)
                else:
                    self.outputs.setdefault(params["processId"], bytearray()).extend(
                        base64.b64decode(params["chunk"]))
            elif method:
                if self.keep_notes:
                    with self.cv:
                        self.notes.append({"t": round(time.time() - self.t0, 3), **msg})
                        self.cv.notify_all()
            else:
                with self.cv:
                    self.replies.setdefault(msg.get("id"), []).append(msg)
                    self.cv.notify_all()
        with self.cv:
            self.eof = True
            self.cv.notify_all()

    def send(self, msg):
        data = (json.dumps(msg) + "\n").encode()
        with self.wlock:
            try:
                self.proc.stdin.write(data)
                self.proc.stdin.flush()
            except (BrokenPipeError, ValueError):
                pass

    def wait(self, id_, timeout=30):
        with self.cv:
            self.cv.wait_for(lambda: id_ in self.replies or self.eof, timeout)
            got = self.replies.get(id_)
            return got[0] if got else None

    def wait_note(self, method, process_id, timeout=30):
        def found():
            return next((n for n in self.notes if n.get("method") == method
                         and n.get("params", {}).get("processId") == process_id), None)
        with self.cv:
            self.cv.wait_for(lambda: found() is not None or self.eof, timeout)
            return found()

    def call(self, msg, timeout=30):
        self.send(msg)
        return self.wait(msg["id"], timeout)

    def handshake(self):
        reply = self.call({"id": "init", "method": "initialize",
                           "params": {"clientName": "codex-environment", "resumeSessionId": None}})
        assert reply and "result" in reply, f"handshake failed: {reply} {self.stderr()}"
        self.send({"method": "initialized", "params": {}})
        return reply

    def close(self, timeout=60):
        with self.wlock:
            try:
                self.proc.stdin.close()
            except (BrokenPipeError, ValueError):
                pass
        return self.proc.wait(timeout=timeout)

    def stderr(self):
        return self.stderr_path.read_text(errors="replace")

    def executor_pid(self):
        """The `codex exec-server` child of this supervisor, if running."""
        for pid, ppid, _, _, args in processes():
            if ppid == self.proc.pid and is_executor(args):
                return pid
        return None


def is_executor(args):
    # `comm` is cut to 16 bytes by ps; the arguments are not.
    return args.split()[1:4] == ["exec-server", "--listen", "stdio"]


def processes():
    """(pid, ppid, rss KiB, comm, args) for every process."""
    out = subprocess.run(["ps", "-axo", "pid=,ppid=,rss=,comm=,args="], capture_output=True, text=True,
                         errors="replace").stdout
    rows = []
    for line in out.splitlines():
        parts = line.split(None, 4)
        if len(parts) >= 4:
            rows.append((int(parts[0]), int(parts[1]), int(parts[2]), parts[3], parts[4] if len(parts) > 4 else ""))
    return rows


def marked(marker_value=None):
    """Processes whose environment carries an exec-server session marker (ccnm's
    own sweep criterion; `ps -E` shows this account's environments)."""
    out = subprocess.run(["ps", "-axEww", "-o", "pid=", "-o", "command="], capture_output=True, text=True,
                         errors="replace").stdout
    wanted = f"{MARKER}={marker_value}" if marker_value else f"{MARKER}="
    found = []
    for line in out.splitlines():
        words = line.split()
        if any(w.startswith(wanted) if not marker_value else w == wanted for w in words[1:]):
            found.append(int(words[0]))
    own = os.getpid()
    return [p for p in found if p != own]


def descendants(root_pid):
    rows = processes()
    children = {}
    for pid, ppid, rss, comm, args in rows:
        children.setdefault(ppid, []).append((pid, rss, comm, args))
    out, stack = [], [root_pid]
    while stack:
        for pid, rss, comm, args in children.get(stack.pop(), []):
            out.append({"pid": pid, "rss_kib": rss, "comm": comm, "args": args[:120]})
            stack.append(pid)
    return out


class RssSampler:
    """Samples the resident size of a supervisor and everything under it."""

    def __init__(self, session, interval=0.1):
        self.session, self.interval = session, interval
        self.peak_supervisor = 0
        self.peak_executor = 0
        self.peak_tree = 0
        self.samples = 0
        self.series = []
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _run(self):
        sup = self.session.proc.pid
        while not self.stop.is_set():
            rows = processes()
            rss = {pid: r for pid, _, r, _, _ in rows}
            executor = next((pid for pid, ppid, _, _, args in rows if ppid == sup and is_executor(args)), None)
            s = rss.get(sup, 0)
            e = rss.get(executor, 0) if executor else 0
            # Everything under the supervisor: exec-server, its fs helpers, commands.
            children = {}
            for pid, ppid, _, _, _ in rows:
                children.setdefault(ppid, []).append(pid)
            tree, stack = 0, [sup]
            while stack:
                for pid in children.get(stack.pop(), []):
                    tree += rss.get(pid, 0)
                    stack.append(pid)
            self.peak_supervisor = max(self.peak_supervisor, s)
            self.peak_executor = max(self.peak_executor, e)
            self.peak_tree = max(self.peak_tree, tree)
            self.samples += 1
            self.series.append((round(time.time() - self.session.t0, 2), s, e))
            time.sleep(self.interval)

    def finish(self):
        self.stop.set()
        self.thread.join(timeout=5)
        return {"supervisor_peak_rss_mib": round(self.peak_supervisor / 1024, 1),
                "executor_peak_rss_mib": round(self.peak_executor / 1024, 1),
                "under_supervisor_peak_rss_mib": round(self.peak_tree / 1024, 1), "samples": self.samples}


def write_summary(name, summary):
    path = HERE / "runs" / f"{name}.json"
    summary = {"versions": versions(), **summary}
    path.write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n")
    print(json.dumps(summary, indent=1, ensure_ascii=False))
    return path
