#!/usr/bin/env python3
"""Neutral exec-server client for V2-P1 (candidates A-proc and A-read). Standard library only.

Opens `ccnm internal exec-serve` locally (ccnm wire protocol 6) with a throwaway
Runtime config and speaks the Codex 0.154.0 exec-server protocol over its stdio.
It imports nothing from ccnm or Codex. `process/start` carries the sandbox Codex
0.154.0 really sent for workspace-write (captured in ccnm P21,
tests/fixtures/codex-0.154.0/exec-server/process-start-workspace-write.json),
respelled for this workspace; ccnm's rule table accepts exactly that shape.

fs/open, fs/readBlock and fs/close parameter names were read off the executor's
own -32602 messages (V2-P1 notes): open {handleId, path}, readBlock {handleId,
offset, len} -> {chunk (base64), eof}, close {handleId}.
"""
import base64
import json
import os
import subprocess
import threading
import time
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
CCNM_REPO = HERE.parents[3] / "ccnm"
CAPTURED = CCNM_REPO / "tests/fixtures/codex-0.154.0/exec-server/process-start-workspace-write.json"
IDENTITY = {"node": "agent", "instance": "codex-main", "provider": "codex", "profile_ref": "default"}


def b64url(obj):
    return base64.urlsafe_b64encode(json.dumps(obj, separators=(",", ":")).encode()).decode().rstrip("=")


def write_native_config(base, root, home, codex_bin, workspace="p1"):
    """A second Runtime config for the same workspace directory. Its own state
    directory, so its write guard never meets the direct path's; the same HOME,
    so a command sees the same home directory on both paths."""
    runtime = Path(base) / "runtime-native"
    (runtime / "state").mkdir(parents=True, exist_ok=True)
    config = runtime / "config.toml"
    config.write_text(f'''this = "runtime"

[nodes.runtime]
codex_bin = "{codex_bin}"

[nodes.agent]
ssh = "agent-node.invalid"

[workspaces.{workspace}]
root = "{root}"
agent = {{ node = "agent", instance = "codex-main" }}
allow_unconfined_exec = true
codex_exec_server = true
''')
    return runtime


def captured_sandbox(root):
    cap = json.loads(CAPTURED.read_text())
    return json.loads(json.dumps(cap["params"]["sandbox"]).replace("{ROOT}", str(root)))


class ExecSession:
    def __init__(self, ccnm, runtime, home, root, workspace="p1"):
        self.session = f"p1-{uuid.uuid4().hex[:12]}"
        wire = b64url({"protocol": 6, "workspace": workspace, "agent": IDENTITY, "session": self.session})
        env = {"PATH": os.environ["PATH"], "HOME": str(home), "XDG_STATE_HOME": str(runtime / "state"),
               "CCNM_CONFIG": str(runtime / "config.toml")}
        self.root = Path(root)
        self.stderr_path = runtime / f"exec-serve-{self.session}.stderr"
        self.proc = subprocess.Popen([ccnm, "internal", "exec-serve", "--payload", wire], env=env,
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=open(self.stderr_path, "wb"))
        self.sent_bytes = self.received_bytes = 0
        self.next_id = 1
        self.replies, self.cv = {}, threading.Condition()
        self.proc_state = {}  # processId -> {"chunks": {seq: bytes}, "exit": code, "closed": bool}
        self.eof = False
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        for line in self.proc.stdout:
            msg = json.loads(line)
            with self.cv:
                self.received_bytes += len(line)
                method = msg.get("method")
                if "id" in msg and method is None:
                    self.replies[msg["id"]] = msg
                elif method in ("process/output", "process/exited", "process/closed"):
                    p = msg["params"]
                    st = self.proc_state.setdefault(p["processId"], {"chunks": {}, "exit": None, "closed": False, "bytes": 0})
                    if method == "process/output":
                        data = base64.b64decode(p["chunk"])
                        st["chunks"][(p.get("stream", ""), p["seq"])] = data
                        st["bytes"] += len(data)
                    elif method == "process/exited":
                        st["exit"] = p.get("exitCode")
                    else:
                        st["closed"] = True
                self.cv.notify_all()
        with self.cv:
            self.eof = True
            self.cv.notify_all()

    def send(self, msg):
        data = (json.dumps(msg, separators=(",", ":")) + "\n").encode()
        with self.cv:
            self.sent_bytes += len(data)
        self.proc.stdin.write(data)
        self.proc.stdin.flush()

    def request(self, method, params, timeout=120):
        with self.cv:
            rid = self.next_id
            self.next_id += 1
        self.send({"id": rid, "method": method, "params": params})
        deadline = time.time() + timeout
        with self.cv:
            while rid not in self.replies:
                left = deadline - time.time()
                if left <= 0 or self.eof:
                    raise RuntimeError(f"no reply to {method}; stderr: {self.stderr_tail()}")
                self.cv.wait(left)
            return self.replies.pop(rid)

    def initialize(self):
        reply = self.request("initialize", {"clientName": "codex-environment", "resumeSessionId": None})
        if "result" not in reply:
            raise RuntimeError(f"initialize failed: {reply}; stderr: {self.stderr_tail()}")
        self.send({"method": "initialized", "params": {}})
        return reply["result"]

    def run(self, argv, timeout=120, sandbox=True, keep_output=True):
        """process/start with the captured workspace-write sandbox; wait for
        exit and close. Returns (reply, exit_code, stdout+stderr bytes, byte count)."""
        cap = json.loads(CAPTURED.read_text().replace("{ROOT}", str(self.root)).replace("{OUTSIDE}", "/nonexistent"))
        params = cap["params"]
        pid = f"p{uuid.uuid4().hex[:10]}"
        params["processId"] = pid
        params["argv"] = argv
        params.pop("metadata", None)
        # ccnm, not a model, would be the caller: no Codex session variables.
        params["env"] = {}
        if not sandbox:
            params["sandbox"] = None
        reply = self.request("process/start", params)
        if "result" not in reply:
            return reply, None, b"", 0
        deadline = time.time() + timeout
        with self.cv:
            while True:
                st = self.proc_state.get(pid)
                if st and st["exit"] is not None and st["closed"]:
                    break
                left = deadline - time.time()
                if left <= 0 or self.eof:
                    break
                self.cv.wait(left)
            st = self.proc_state.pop(pid, {"chunks": {}, "exit": None, "bytes": 0})
        data = b"".join(v for _, v in sorted(st["chunks"].items(), key=lambda kv: (kv[0][0], kv[0][1]))) if keep_output else b""
        return reply, st["exit"], data, st["bytes"]

    def read_range(self, rel, offset, length):
        """One page the way an adapter would serve it: open, one readBlock, close."""
        handle = f"h{uuid.uuid4().hex[:10]}"
        opened = self.request("fs/open", {"handleId": handle, "path": f"file://{self.root / rel}"})
        if "result" not in opened:
            return opened, b"", True
        block = self.request("fs/readBlock", {"handleId": handle, "offset": offset, "len": length})
        self.request("fs/close", {"handleId": handle})
        if "result" not in block:
            return block, b"", True
        return block, base64.b64decode(block["result"]["chunk"]), block["result"]["eof"]

    def open_raw(self, uri):
        handle = f"h{uuid.uuid4().hex[:10]}"
        reply = self.request("fs/open", {"handleId": handle, "path": uri})
        if "result" in reply:
            self.request("fs/close", {"handleId": handle})
        return reply

    def stderr_tail(self, n=6):
        try:
            return self.stderr_path.read_text(errors="replace").splitlines()[-n:]
        except OSError:
            return []

    def close(self, timeout=60):
        try:
            self.proc.stdin.close()
        except OSError:
            pass
        try:
            return self.proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            return self.proc.wait()


def sandbox_state(root):
    """The `codex sandbox --sandbox-state-json` value carrying the same
    permissions object exec-serve accepts (shape found in V2-P1: the object
    under `permissionProfile`, plus `sandboxCwd` and `workspaceRoots`)."""
    sb = captured_sandbox(root)
    return {"permissionProfile": sb["permissions"], "sandboxCwd": sb["cwd"], "workspaceRoots": sb["workspaceRoots"]}


def run_s(codex, root, home, argv, timeout=120):
    """Candidate S: the same argv under `codex sandbox`, no RPC."""
    env = {"PATH": os.environ["PATH"], "HOME": str(home), "CODEX_HOME": str(Path(home) / ".codex-s")}
    Path(env["CODEX_HOME"]).mkdir(exist_ok=True)
    return subprocess.run([codex, "sandbox", "--sandbox-state-json", json.dumps(sandbox_state(root)), "--"] + argv,
                          env=env, cwd=str(root), capture_output=True, timeout=timeout)
