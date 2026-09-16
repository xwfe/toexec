#!/usr/bin/env python3
"""A neutral client for the ccnm P24 real-machine round. Standard library only.

It speaks to `ccnm internal exec-serve` (the Codex exec-server chain, wire
protocol 6) and `ccnm internal mcp-serve` (the managed MCP open, protocol 4)
over the **real ssh** from this Mac to ccrun on hpsrv. It imports nothing from
ccnm or Codex: requests are Codex 0.154.0's own, as captured in ccnm P21
(`ccnm/tests/fixtures/codex-0.154.0/exec-server/`), respelled for hpsrv.

The ssh options are the ones ccnm's `Ssh::exec_transport_cmd` builds (pinned by
ccnm's unit test `mcp_transport_cmd_is_one_plain_ssh_without_control_master`),
so what this client exercises on the wire is what `ccnm internal
exec-transport` execs for a real Codex session.
"""

import base64
import json
import os
import signal
import subprocess
import threading
import time
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
CCNM_REPO = Path(os.environ.get("P24_CCNM_REPO", HERE.parents[3] / "ccnm"))
FIXTURES = CCNM_REPO / "tests/fixtures/codex-0.154.0/exec-server"

ALIAS = os.environ.get("P24_ALIAS", "ccnm-p24-hpsrv")
CCNM_BIN = "/home/ccrun/.local/bin/ccnm"
ROOT = "/home/ccrun/p24-demo"
OUTSIDE = "/home/ccrun/p24-outside"
SERVER_HOME = "/home/ccrun"
WORKSPACE = "p24"
IDENTITY = {"node": "mbp", "instance": "codex-main", "provider": "codex", "profile_ref": "default"}

SSH_OPTIONS = [
    "-o", "SendEnv=-*", "-o", "SetEnv=CCNM_TRANSPORT=1", "-o", "ForwardAgent=no",
    "-o", "ClearAllForwardings=yes", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
    "-o", "ClearAllForwardings=yes", "-o", "ControlMaster=no", "-o", "ControlPath=none",
    "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=20",
    "-o", "SendEnv=-ANTHROPIC_*", "-o", "SendEnv=-CLAUDE_*",
]


def b64url(obj):
    return base64.urlsafe_b64encode(json.dumps(obj, separators=(",", ":")).encode()).decode().rstrip("=")


def new_session():
    return f"p24-{uuid.uuid4().hex[:12]}"


def native_wire(session):
    return b64url({"protocol": 6, "workspace": WORKSPACE, "agent": IDENTITY, "session": session})


def managed_wire(session):
    return b64url({"protocol": 4, "workspace": WORKSPACE, "agent": IDENTITY, "session": session,
                   "policy": "coding", "interactive": True})


def ssh_argv(internal, wire):
    return ["/usr/bin/ssh", *SSH_OPTIONS, "-T", ALIAS, CCNM_BIN, "internal", internal, "--payload", wire]


def remote(script, timeout=60):
    """Run a shell script as ccrun on hpsrv (a separate plain ssh, for checks)."""
    out = subprocess.run(["/usr/bin/ssh", "-o", "BatchMode=yes", "-T", ALIAS, "bash", "-s"], input=script,
                         capture_output=True, text=True, timeout=timeout)
    return out.stdout


def guard_state():
    """The workspace write-guard marker(s) on hpsrv: 'released', 'held <session> <ws>', or 'none'."""
    text = remote("cat ~/.local/state/ccnm/write-guards/*.lock 2>/dev/null || echo none").strip()
    return text or "empty"


MARKED = r"""for d in /proc/[0-9]*; do
  if tr '\0' '\n' < "$d/environ" 2>/dev/null | grep -q '^CCNM_EXEC_SESSION='; then
    printf '%s %s\n' "${d#/proc/}" "$(tr '\0' ' ' < "$d/cmdline" | cut -c1-80)"
  fi
done"""


def marked_processes():
    """ccrun processes that carry an exec-server session marker (the sweep's own criterion)."""
    return [l for l in remote(MARKED).splitlines() if l.strip()]


def fixture(name, id_, **extra):
    text = (FIXTURES / name).read_text().replace("{ROOT}", ROOT).replace("{OUTSIDE}", OUTSIDE) \
        .replace("{SERVER_HOME}", SERVER_HOME)
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


def start(id_, script, sandbox=True, process_id=None):
    msg = fixture("process-start-workspace-write.json", id_, processId=process_id or f"p{id_}",
                  argv=["/bin/sh", "-c", script])
    if not sandbox:
        msg["params"]["sandbox"] = None
    return msg


def write_file(id_, path, text):
    return fixture("fs-write-file-workspace-write.json", id_, path=f"file://{path}",
                   dataBase64=base64.b64encode(text.encode()).decode())


class Session:
    """One ssh + one remote supervisor. Line-delimited JSON-RPC both ways."""

    def __init__(self, internal="exec-serve", session=None, wire=None, argv=None, env=None):
        self.session = session or new_session()
        if argv is None:
            wire = wire or (native_wire(self.session) if internal == "exec-serve" else managed_wire(self.session))
            argv = ssh_argv(internal, wire)
        self.argv = argv
        self.proc = subprocess.Popen(self.argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, env=env)
        self.cv = threading.Condition()
        self.replies, self.notes, self.stderr, self.eof = {}, [], bytearray(), False
        threading.Thread(target=self._read, daemon=True).start()
        threading.Thread(target=self._read_err, daemon=True).start()

    def _read(self):
        for line in self.proc.stdout:
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            with self.cv:
                if "id" in msg and "method" not in msg:
                    self.replies[msg["id"]] = msg
                else:
                    self.notes.append(msg)
                self.cv.notify_all()
        with self.cv:
            self.eof = True
            self.cv.notify_all()

    def _read_err(self):
        for chunk in iter(lambda: self.proc.stderr.read1(4096), b""):
            self.stderr.extend(chunk)

    def send(self, msg):
        self.proc.stdin.write((json.dumps(msg) + "\n").encode())
        self.proc.stdin.flush()

    def call(self, msg, timeout=30):
        """The reply, or None when the connection ended or the time ran out."""
        self.send(msg)
        deadline = time.time() + timeout
        with self.cv:
            while msg["id"] not in self.replies and not self.eof and time.time() < deadline:
                self.cv.wait(timeout=max(0.05, deadline - time.time()))
            return self.replies.get(msg["id"])

    def handshake(self, timeout=60):
        reply = self.call({"id": 0, "method": "initialize",
                           "params": {"clientName": "codex-environment", "resumeSessionId": None}}, timeout)
        if reply and "result" in reply:
            self.send({"method": "initialized", "params": {}})
        return reply

    def mcp_handshake(self, timeout=60):
        reply = self.call({"jsonrpc": "2.0", "id": 0, "method": "initialize",
                           "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                                      "clientInfo": {"name": "p24-neutral", "version": "0"}}}, timeout)
        if reply and "result" in reply:
            self.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        return reply

    def output(self, process_id):
        with self.cv:
            chunks = [n["params"] for n in self.notes
                      if n.get("method") == "process/output" and n["params"].get("processId") == process_id]
        return b"".join(base64.b64decode(c["chunk"]) for c in sorted(chunks, key=lambda c: c["seq"]))

    def wait_exited(self, process_id, timeout=30):
        deadline = time.time() + timeout
        with self.cv:
            while time.time() < deadline and not self.eof:
                for n in self.notes:
                    if n.get("method") == "process/exited" and n["params"].get("processId") == process_id:
                        return n["params"].get("exitCode")
                self.cv.wait(timeout=0.2)
        return None

    def run(self, id_, script, timeout=30, **kw):
        """process/start and wait for exit. Returns (reply, exit_code, output text)."""
        reply = self.call(start(id_, script, **kw))
        code = self.wait_exited(f"p{id_}", timeout) if reply and "result" in reply else None
        time.sleep(0.2)
        return reply, code, self.output(f"p{id_}").decode(errors="replace")

    def alive(self):
        return self.proc.poll() is None and not self.eof

    def close(self, timeout=60):
        try:
            self.proc.stdin.close()
        except OSError:
            pass
        try:
            rc = self.proc.wait(timeout)
        except subprocess.TimeoutExpired:
            rc = None
        time.sleep(0.2)
        return rc, self.stderr.decode(errors="replace")

    def kill_local(self, sig=signal.SIGKILL):
        os.kill(self.proc.pid, sig)


def error_code(reply):
    return (reply or {}).get("error", {}).get("code")
