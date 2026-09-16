#!/usr/bin/env python3
"""Neutral MCP client for the V2-P0 direct-execution control. Standard library only.

Opens `ccnm internal mcp-serve` locally with a managed-open payload (ccnm wire
protocol 4) and a throwaway Runtime config, speaks MCP over its stdio, and
counts every byte that crosses the pipe in each direction. It imports nothing
from ccnm; the tool names and arguments are the frozen `ccnm.workspace-mcp/1`
ones (ccnm docs/protocol/fixtures-mcp/tools-list-coding.json).
"""
import base64
import json
import os
import subprocess
import threading
import time
from pathlib import Path


def b64url(obj):
    return base64.urlsafe_b64encode(json.dumps(obj, separators=(",", ":")).encode()).decode().rstrip("=")


IDENTITY = {"node": "agent", "instance": "claude-main", "provider": "claude", "profile_ref": "default"}


def write_runtime_config(base, root, workspace="p0"):
    """A Runtime config for one workspace, the shape ccnm's exec_serve tests use.
    `allow_unconfined_exec`: this account is not a dedicated Runtime identity,
    and saying so is the only way `exec_command` runs at all."""
    runtime = Path(base) / "runtime"
    (runtime / "home").mkdir(parents=True, exist_ok=True)
    (runtime / "state").mkdir(exist_ok=True)
    config = runtime / "config.toml"
    config.write_text(f'''this = "runtime"

[nodes.runtime]

[nodes.agent]
ssh = "agent-node.invalid"

[workspaces.{workspace}]
root = "{root}"
agent = {{ node = "agent", instance = "claude-main" }}
allow_unconfined_exec = true
''')
    return runtime


class McpSession:
    def __init__(self, ccnm, runtime, workspace="p0", session=None, policy="coding"):
        wire = b64url({"protocol": 4, "workspace": workspace, "agent": IDENTITY,
                       "session": session or f"p0-{os.getpid()}-{int(time.time() * 1000)}",
                       "policy": policy, "interactive": True})
        env = {"PATH": os.environ["PATH"], "HOME": str(runtime / "home"),
               "XDG_STATE_HOME": str(runtime / "state"), "CCNM_CONFIG": str(runtime / "config.toml")}
        self.stderr_path = runtime / f"mcp-serve-{int(time.time() * 1000)}.stderr"
        self.proc = subprocess.Popen([ccnm, "internal", "mcp-serve", "--payload", wire], env=env,
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=open(self.stderr_path, "wb"))
        self.sent_bytes = 0
        self.received_bytes = 0
        self.next_id = 1
        self.replies = {}
        self.cv = threading.Condition()
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        for line in self.proc.stdout:
            msg = json.loads(line)
            with self.cv:
                self.received_bytes += len(line)
                if "id" in msg:
                    self.replies[msg["id"]] = msg
                    self.cv.notify_all()
        with self.cv:
            self.cv.notify_all()

    def send(self, msg):
        data = (json.dumps(msg, separators=(",", ":")) + "\n").encode()
        with self.cv:
            self.sent_bytes += len(data)
        self.proc.stdin.write(data)
        self.proc.stdin.flush()

    def request(self, method, params, timeout=120):
        rid = self.next_id
        self.next_id += 1
        self.send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        deadline = time.time() + timeout
        with self.cv:
            while rid not in self.replies:
                left = deadline - time.time()
                if left <= 0 or self.proc.poll() is not None:
                    raise RuntimeError(f"no reply to {method}; stderr: {self.stderr_tail()}")
                self.cv.wait(left)
            return self.replies.pop(rid)

    def initialize(self):
        reply = self.request("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                            "clientInfo": {"name": "v2-p0-neutral", "version": "0"}})
        if "result" not in reply:
            raise RuntimeError(f"initialize failed: {reply}; stderr: {self.stderr_tail()}")
        self.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        return reply["result"]

    def call(self, name, arguments, timeout=660):
        reply = self.request("tools/call", {"name": name, "arguments": arguments}, timeout)
        if "result" not in reply:
            raise RuntimeError(f"{name} failed at the protocol level: {reply}")
        return reply["result"]

    def stderr_tail(self, n=5):
        try:
            return self.stderr_path.read_text(errors="replace").splitlines()[-n:]
        except OSError:
            return []

    def close(self, timeout=30):
        try:
            self.proc.stdin.close()
        except OSError:
            pass
        try:
            return self.proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            return self.proc.wait()


def structured(result):
    """The tool's structured result; ccnm puts the same JSON in the text block."""
    if "structuredContent" in result:
        return result["structuredContent"]
    for block in result.get("content", []):
        if block.get("type") == "text":
            try:
                return json.loads(block["text"])
            except ValueError:
                return {"text": block["text"]}
    return {}
