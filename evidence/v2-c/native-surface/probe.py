#!/usr/bin/env python3
"""ccnm P21：Codex 连"项目路径只在对面存在"的 exec-server 时，到底发什么。

拓扑：exec-server 跑在 Linux 容器里（项目在 /srv/p21/work，Mac 上没有这个路径）；
Codex 跑在 macOS 上，经本机一个逐帧转发的网桥连过去。网桥把两个方向的 JSON-RPC
都记下来，也能按规则替 exec-server 回答某些请求（P21.3）。

不发任何真实模型请求：模型接口是本机假服务，Codex 进程树套 sandbox-exec 禁非本机
出站，HOME/CODEX_HOME 是临时目录。只用标准库。

用法：probe.py <实验名> <新输出目录>，实验见 EXPERIMENTS。
"""

import asyncio
import base64
import json
import os
import shlex
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

CODEX = os.environ.get("P21_CODEX", "codex")
UPSTREAM = ("127.0.0.1", int(os.environ.get("P21_UPSTREAM_PORT", "47121")))
CONTAINER = os.environ.get("P21_CONTAINER", "p21-exec")
ROOT = "/srv/p21/work"
OUTSIDE = "/home/runner/outside"
SANDBOX_PROFILE = """(version 1)
(allow default)
(deny network-outbound)
(allow network-outbound (remote ip "localhost:*"))
(allow network-outbound (remote unix-socket))
"""
# ccnm 现在 Managed Codex 关掉的 feature，去掉三个执行相关的——原生链要的就是它们。
CCNM_DISABLED = ["shell_tool", "unified_exec", "unified_exec_tty", "view_image", "apps", "plugins",
                 "hooks", "multi_agent", "multi_agent_v2", "browser_use", "computer_use",
                 "image_generation", "memories", "workspace_dependencies", "skill_search",
                 "shell_snapshot", "goals", "tool_suggest"]
NATIVE_KEEP = {"shell_tool", "unified_exec", "unified_exec_tty"}


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Log:
    def __init__(self, path):
        self.path, self.lock = path, threading.Lock()

    def __call__(self, **fields):
        fields["t"] = round(time.time(), 3)
        with self.lock, open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(fields, ensure_ascii=False) + "\n")


# ---------------------------------------------------------------- 假模型接口

def sse(events):
    return "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events).encode()


class MockModel:
    """plan 里每一步是一个动作：
    ("exec", 命令)           调 exec_command（没有就退到 shell_command / shell）
    ("tool", 名字, 参数)      调一个 function 工具
    ("patch", patch 文本)     有 apply_patch 工具就调它，没有就用 exec_command 跑 heredoc
    ("say", 文本)             结束
    每收到一次 /responses 请求就走一步。"""

    def __init__(self, out, plan, log):
        self.out, self.plan, self.log = out, plan, log
        self.step, self.done = 0, threading.Event()
        self.lock, self.side_requests = threading.Lock(), 0
        model = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                body = json.dumps({"data": [], "models": []}).encode()
                self.send_response(200)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                raw = self.rfile.read(int(self.headers.get("content-length") or 0))
                if not self.path.rstrip("/").endswith("/responses"):
                    self.send_response(404)
                    self.end_headers()
                    return
                body = json.loads(raw or b"{}")
                fmt = ((body.get("text") or {}).get("format") or {})
                title = fmt.get("type") == "json_schema" and "title" in json.dumps(fmt.get("schema"))
                with model.lock:
                    if title:
                        n = None
                        model.side_requests += 1
                        (model.out / f"model-side-request-{model.side_requests}.json").write_bytes(raw)
                    else:
                        n = model.step
                        model.step += 1
                        (model.out / f"model-request-{n}.json").write_bytes(raw)
                tools, code_mode = offered_tools(body)
                if title:
                    item = {"type": "message", "role": "assistant", "id": "msg_title",
                            "content": [{"type": "output_text", "text": json.dumps({"title": "probe"})}]}
                else:
                    item = model.item(n, tools, code_mode)
                model.log(side="model", step=n, title=title, code_mode=code_mode, offered=sorted(tools), emitted=item)
                rid = f"resp_{n if n is not None else 'title'}_{time.time_ns()}"
                events = [{"type": "response.created", "response": {"id": rid}},
                          {"type": "response.output_item.done", "item": item},
                          {"type": "response.completed", "response": {"id": rid, "usage": {
                              "input_tokens": 0, "input_tokens_details": None, "output_tokens": 0,
                              "output_tokens_details": None, "total_tokens": 0}}}]
                payload = sse(events)
                self.send_response(200)
                self.send_header("content-type", "text/event-stream")
                self.send_header("content-length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                if n is not None and n >= len(model.plan) - 1:
                    model.done.set()

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def item(self, n, tools, code_mode):
        action = self.plan[min(n, len(self.plan) - 1)]
        call_id = f"call_{n}"

        def say(text):
            return {"type": "message", "role": "assistant", "id": f"msg_{n}",
                    "content": [{"type": "output_text", "text": text}]}

        def function(name, args):
            return {"type": "function_call", "call_id": call_id, "name": name, "arguments": json.dumps(args)}

        def js(code):
            return {"type": "custom_tool_call", "call_id": call_id, "name": "exec", "input": code}

        def exec_like(command):
            if code_mode:
                return js(f"const r = await tools.exec_command({json.dumps({'cmd': command})});\n"
                          "text(JSON.stringify(r));")
            if "exec_command" in tools:
                return function("exec_command", {"cmd": command})
            if "shell_command" in tools:
                return function("shell_command", {"command": command})
            if "shell" in tools:
                return function("shell", {"command": ["bash", "-lc", command]})
            return say(f"no shell-like tool offered: {sorted(tools)}")

        kind = action[0]
        if kind == "say":
            return say(action[1])
        if kind == "exec":
            return exec_like(action[1])
        if kind == "js":
            return js(action[1]) if code_mode else say("code mode not offered")
        if kind == "tool":
            if action[1] not in tools:
                return say(f"tool {action[1]} not offered")
            return function(action[1], action[2])
        if kind == "patch":
            if code_mode:
                return js(f"const r = await tools.apply_patch({json.dumps(action[1])});\ntext(JSON.stringify(r));")
            tool = tools.get("apply_patch")
            if tool and tool.get("type") == "custom":
                return {"type": "custom_tool_call", "call_id": call_id, "name": "apply_patch", "input": action[1]}
            if tool:
                return function("apply_patch", {"input": action[1]})
            return exec_like(f"apply_patch <<'EOF'\n{action[1]}\nEOF")
        raise ValueError(action)


def offered_tools(body):
    """顶层 tools 和 additional_tools 输入项里的工具都算；Code Mode 的标志是 functions 命名空间里的 exec。"""
    found, code_mode = {}, False

    def walk(items, prefix=""):
        nonlocal code_mode
        for t in items or []:
            if not isinstance(t, dict):
                continue
            if t.get("type") == "namespace":
                walk(t.get("tools"), t.get("name", "") + ".")
                continue
            name = prefix + str(t.get("name"))
            found[name] = t
            if name == "functions.exec" and t.get("type") == "custom":
                code_mode = True

    walk(body.get("tools"))
    for item in body.get("input", []):
        if isinstance(item, dict) and item.get("type") == "additional_tools":
            walk(item.get("tools"))
    return found, code_mode


# ---------------------------------------------------------------- 逐帧网桥

def read_frame(buf):
    """从 buf 头上切一整帧；不够一帧返回 None。返回 (原始字节, fin, opcode, 已去掩码的负载)。"""
    if len(buf) < 2:
        return None
    b0, b1 = buf[0], buf[1]
    length, pos = b1 & 0x7F, 2
    if length == 126:
        if len(buf) < 4:
            return None
        length, pos = int.from_bytes(buf[2:4], "big"), 4
    elif length == 127:
        if len(buf) < 10:
            return None
        length, pos = int.from_bytes(buf[2:10], "big"), 10
    mask = b""
    if b1 & 0x80:
        if len(buf) < pos + 4:
            return None
        mask, pos = buf[pos:pos + 4], pos + 4
    if len(buf) < pos + length:
        return None
    payload = buf[pos:pos + length]
    if mask:
        payload = bytes(c ^ mask[i % 4] for i, c in enumerate(payload))
    return buf[:pos + length], bool(b0 & 0x80), b0 & 0x0F, payload


def text_frame(data, masked=False):
    head = bytes([0x81])
    n = len(data)
    mbit = 0x80 if masked else 0
    if n < 126:
        head += bytes([mbit | n])
    elif n < 65536:
        head += bytes([mbit | 126]) + n.to_bytes(2, "big")
    else:
        head += bytes([mbit | 127]) + n.to_bytes(8, "big")
    if masked:
        key = os.urandom(4)
        return head + key + bytes(c ^ key[i % 4] for i, c in enumerate(data))
    return head + data


class Bridge:
    """整帧转发，两个方向都记 JSON；intercept(msg) 返回一个响应对象时，这条请求不转发，
    由网桥直接回给客户端。"""

    def __init__(self, out, log, intercept=None):
        self.log, self.intercept = log, intercept
        self.messages = open(out / "messages.jsonl", "w", encoding="utf-8")
        self.connections = 0
        self.loop = asyncio.new_event_loop()
        self.port = free_port()
        ready = threading.Event()

        def run():
            asyncio.set_event_loop(self.loop)
            self.loop.run_until_complete(asyncio.start_server(self.handle, "127.0.0.1", self.port))
            ready.set()
            self.loop.run_forever()

        threading.Thread(target=run, daemon=True).start()
        ready.wait()

    def record(self, direction, msg, **extra):
        self.messages.write(json.dumps({"dir": direction, "msg": msg, **extra}, ensure_ascii=False) + "\n")
        self.messages.flush()

    async def handle(self, reader, writer):
        self.connections += 1
        self.log(side="bridge", event="accepted", n=self.connections)
        try:
            head = await reader.readuntil(b"\r\n\r\n")
            up_r, up_w = await asyncio.open_connection(*UPSTREAM)
            up_w.write(head)
            await up_w.drain()
            writer.write(await up_r.readuntil(b"\r\n\r\n"))
            await writer.drain()
        except Exception as exc:  # noqa: BLE001
            self.log(side="bridge", event="handshake-failed", error=repr(exc))
            writer.close()
            return

        async def pump(src, dst, direction, client_side):
            buf = b""
            try:
                while chunk := await src.read(65536):
                    buf += chunk
                    while (frame := read_frame(buf)) is not None:
                        raw, fin, opcode, payload = frame
                        buf = buf[len(raw):]
                        msg = None
                        if opcode == 1 and fin:
                            try:
                                msg = json.loads(payload)
                            except ValueError:
                                msg = {"undecodable_bytes": len(payload)}
                        if client_side and msg is not None and self.intercept:
                            reply = self.intercept(msg)
                            if reply == {"__drop__": True}:
                                self.record(direction, msg, intercepted=True, dropped=True)
                                continue
                            if reply is not None:
                                self.record(direction, msg, intercepted=True)
                                self.record("bridge->client", reply)
                                writer.write(text_frame(json.dumps(reply).encode()))
                                await writer.drain()
                                continue
                        if msg is not None:
                            self.record(direction, msg)
                        dst.write(raw)
                        await dst.drain()
            except Exception as exc:  # noqa: BLE001
                self.log(side="bridge", event="pump-error", direction=direction, error=repr(exc))
            finally:
                dst.close()

        await asyncio.gather(pump(reader, up_w, "client->server", True),
                             pump(up_r, writer, "server->client", False))
        self.log(side="bridge", event="closed")


# ---------------------------------------------------------------- 直接连 exec-server 的小客户端

class WsClient:
    def __init__(self):
        self.sock = socket.create_connection(UPSTREAM, timeout=10)
        key = base64.b64encode(os.urandom(16)).decode()
        self.sock.sendall((f"GET / HTTP/1.1\r\nHost: {UPSTREAM[0]}:{UPSTREAM[1]}\r\nUpgrade: websocket\r\n"
                           f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
        self.buf = b""
        while b"\r\n\r\n" not in self.buf:
            self.buf += self.sock.recv(65536)
        head, self.buf = self.buf.split(b"\r\n\r\n", 1)
        assert b" 101 " in head.split(b"\r\n")[0], head
        self.next_id = 1

    def send(self, obj):
        self.sock.sendall(text_frame(json.dumps(obj).encode(), masked=True))

    def recv(self):
        while (frame := read_frame(self.buf)) is None:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise EOFError
            self.buf += chunk
        raw, _, _, payload = frame
        self.buf = self.buf[len(raw):]
        return json.loads(payload)

    def request(self, method, params):
        rid = self.next_id
        self.next_id += 1
        self.send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        while True:
            msg = self.recv()
            if msg.get("id") == rid:
                return msg

    def handshake(self):
        init = self.request("initialize", {"clientName": "p21-probe", "resumeSessionId": None})
        self.send({"jsonrpc": "2.0", "method": "initialized", "params": {}})
        return init


def container(cmd):
    return subprocess.run(["docker", "exec", "-u", "runner", CONTAINER, "sh", "-c", cmd],
                          capture_output=True, text=True, errors="replace", timeout=30)


# ---------------------------------------------------------------- 起 Codex

def codex_args(model_port, native_keep=NATIVE_KEEP, enable=()):
    p = "p21mock"
    args = ["--sandbox", "workspace-write", "-c", 'web_search="disabled"', "-c", "agents.enabled=false",
            "-c", f'model_provider="{p}"', "-c", f'model_providers.{p}.name="{p}"',
            "-c", f'model_providers.{p}.base_url="http://127.0.0.1:{model_port}/v1"',
            "-c", f'model_providers.{p}.wire_api="responses"',
            "-c", f"model_providers.{p}.requires_openai_auth=false"]
    for feature in CCNM_DISABLED:
        if feature not in native_keep and feature not in enable:
            args += ["--disable", feature]
    return args


def env_for(out, bridge_port):
    home = out / "home"
    (home / ".codex").mkdir(parents=True, exist_ok=True)
    return {"HOME": str(home), "CODEX_HOME": str(home / ".codex"), "PATH": os.environ["PATH"],
            "TERM": "xterm-256color", "CODEX_EXEC_SERVER_URL": f"ws://127.0.0.1:{bridge_port}"}


def run_exec(out, log, bridge, model, cwd_flag, local_cwd, extra=()):
    profile = out / "no-egress.sb"
    profile.write_text(SANDBOX_PROFILE)
    cmd = ["sandbox-exec", "-f", str(profile), CODEX, "exec", "--ignore-user-config", "--ignore-rules",
           "--skip-git-repo-check", "--ephemeral", "--json", "--color", "never", "-c", 'approval_policy="never"']
    cmd += codex_args(model.port, *extra)
    if cwd_flag:
        cmd += ["-C", cwd_flag]
    cmd += ["run the probe"]
    local_cwd.mkdir(parents=True, exist_ok=True)
    log(side="harness", event="codex-exec", argv=cmd, cwd=str(local_cwd))
    proc = subprocess.run(cmd, cwd=local_cwd, env=env_for(out, bridge.port), stdin=subprocess.DEVNULL,
                          capture_output=True, timeout=180)
    (out / "codex.stdout").write_bytes(proc.stdout)
    (out / "codex.stderr").write_bytes(proc.stderr)
    return {"codex_rc": proc.returncode, "stderr_tail": proc.stderr.decode(errors="replace")[-600:]}


def run_tui(out, log, bridge, model, cwd_flag, local_cwd, extra=(), timeout=120, approval="decline"):
    profile = out / "no-egress.sb"
    profile.write_text(SANDBOX_PROFILE)
    env = env_for(out, bridge.port)
    inner = ["env", "-i"] + [f"{k}={v}" for k, v in env.items()]
    inner += ["sandbox-exec", "-f", str(profile), CODEX, "--no-alt-screen", "-c", 'approval_policy="on-request"']
    inner += codex_args(model.port, *extra)
    if cwd_flag:
        inner += ["-C", cwd_flag]
    inner += ["--", "run the probe"]
    local_cwd.mkdir(parents=True, exist_ok=True)
    sock = f"p21-{os.getpid()}"
    shell_cmd = shlex.join(inner)
    log(side="harness", event="codex-tui", argv=inner, cwd=str(local_cwd))
    subprocess.run(["tmux", "-L", sock, "new-session", "-d", "-s", "p21", "-x", "200", "-y", "60",
                    "-c", str(local_cwd), shell_cmd + "; echo CODEX_EXITED=$?; sleep 600"], check=True)
    panes = []
    deadline = time.time() + timeout
    finished_at = None
    answered_trust = False
    approvals, approvals_seen = [], {}
    try:
        while time.time() < deadline:
            time.sleep(2)
            pane = subprocess.run(["tmux", "-L", sock, "capture-pane", "-p", "-t", "p21", "-S", "-200"],
                                  capture_output=True, text=True).stdout
            panes.append(pane)
            if "CODEX_EXITED=" in pane:
                break
            if "Do you trust the contents of this directory" in pane and not answered_trust:
                answered_trust = True
                (out / "tui-trust-prompt.txt").write_text(pane)
                log(side="harness", event="trust-prompt-answered", choice="1. Yes, continue")
                subprocess.run(["tmux", "-L", sock, "send-keys", "-t", "p21", "Enter"], check=True)
                continue
            tail = pane[-1500:]
            if ("Would you like to" in tail and "Press enter to confirm" in tail
                    and tail.count("Would you like to") > approvals_seen.get(len(panes) - 1, 0)):
                approvals.append(tail)
                (out / f"tui-approval-{len(approvals)}.txt").write_text(tail)
                key = "y" if approval == "accept" else "Escape"
                log(side="harness", event="approval-prompt", answer=key)
                subprocess.run(["tmux", "-L", sock, "send-keys", "-t", "p21", key], check=True)
                time.sleep(1)
                continue
            if model.done.is_set():
                finished_at = finished_at or time.time()
                if time.time() - finished_at > 6:
                    break
    finally:
        (out / "tui-pane.txt").write_text(panes[-1] if panes else "")
        subprocess.run(["tmux", "-L", sock, "kill-server"], capture_output=True)
    return {"model_steps": model.step, "plan_done": model.done.is_set(), "approval_prompts": len(approvals),
            "pane_tail": (panes[-1] if panes else "")[-1500:]}


# ---------------------------------------------------------------- 汇总

def summarize(out):
    msgs = [json.loads(line) for line in open(out / "messages.jsonl")] if (out / "messages.jsonl").exists() else []
    methods, fs_calls, starts = {}, [], []
    responses = {}
    for m in msgs:
        msg = m["msg"]
        if m["dir"] == "server->client" and "id" in msg:
            responses[msg["id"]] = msg
    for m in msgs:
        msg = m["msg"]
        if m["dir"] != "client->server" or "method" not in msg:
            continue
        meth = msg["method"]
        methods[meth] = methods.get(meth, 0) + 1
        params = msg.get("params") or {}
        resp = responses.get(msg.get("id"))
        outcome = None
        if resp is not None:
            outcome = "error " + json.dumps(resp["error"])[:200] if "error" in resp else "ok"
        if m.get("intercepted"):
            outcome = "intercepted"
        if meth.startswith("fs/"):
            fs_calls.append({"method": meth, "path": params.get("path") or params.get("sourcePath"),
                             "sandbox": None if params.get("sandbox") is None else "present", "outcome": outcome})
        if meth == "process/start":
            sb = params.get("sandbox") or {}
            starts.append({"argv": params.get("argv"), "cwd": params.get("cwd"), "sandbox_cwd": sb.get("cwd"),
                           "workspaceRoots": sb.get("workspaceRoots"),
                           "entries": [(e["path"].get("value", e["path"]), e["access"])
                                       for e in (sb.get("permissions", {}).get("file_system", {}) or {}).get("entries", [])],
                           "network": sb.get("permissions", {}).get("network"), "outcome": outcome})
        if meth in ("environmentConfig/read", "capabilityRoots/discoverV1", "initialize"):
            fs_calls.append({"method": meth, "params": params, "outcome": outcome})
    return {"methods": methods, "fs_and_config_calls": fs_calls, "process_starts": starts}


def process_outputs(out):
    """把 exec-server 发回的进程输出解出来（base64），方便看命令真正打印了什么。"""
    text = {}
    for line in open(out / "messages.jsonl"):
        m = json.loads(line)
        msg = m["msg"]
        res = msg.get("result") if isinstance(msg, dict) else None
        if isinstance(res, dict) and "chunks" in res:
            for c in res["chunks"]:
                text.setdefault(res.get("processId", "?"), "")
                text[res.get("processId", "?")] += base64.b64decode(c.get("chunk", "")).decode(errors="replace")
        params = msg.get("params") if isinstance(msg, dict) else None
        if msg.get("method") == "process/output" and isinstance(params, dict):
            pid = params.get("processId", "?")
            text[pid] = text.get(pid, "") + base64.b64decode(params.get("chunk", "")).decode(errors="replace")
    return text


def main():
    name, out = sys.argv[1], Path(sys.argv[2]).resolve()
    out.mkdir(parents=True, exist_ok=False)
    log = Log(out / "harness.jsonl")
    EXPERIMENTS[name](out, log)


def markers(out):
    """两边放了内容不同的 AGENTS.md 时，看发给模型的第一条请求里带的是哪一边的。"""
    req = out / "model-request-0.json"
    if not req.exists():
        return None
    text = req.read_text(errors="replace")
    return {"LOCAL-AGENTS-MARKER": "LOCAL-AGENTS-MARKER" in text, "REMOTE-AGENTS-MARKER": "REMOTE-AGENTS-MARKER" in text}


def ancestor_git_not_found(msg):
    """P21.3：工作区根以上的 .git 查询不转发，按 exec-server 对不存在路径的原样错误回答。"""
    if msg.get("method") != "fs/getMetadata" or "id" not in msg:
        return None
    path = (msg.get("params") or {}).get("path", "")
    if not path.startswith("file://") or not path.endswith("/.git"):
        return None
    parent = path[len("file://"):-len("/.git")] or "/"
    inside = parent == ROOT or parent.startswith(ROOT + "/")
    if inside:
        return None
    return {"id": msg["id"], "error": {"code": -32004, "message": "No such file or directory (os error 2)"}}


READ_METHODS = {"fs/getMetadata", "fs/readFile", "fs/open", "fs/readDirectory", "fs/walk", "fs/canonicalize"}
WRITE_METHODS = {"fs/writeFile", "fs/remove", "fs/copy", "fs/createDirectory"}
HANDLE_METHODS = {"fs/readBlock", "fs/close"}
PROCESS_METHODS = {"process/read", "process/write", "process/signal", "process/terminate"}
ALLOWED_ENTRIES = [
    ({"type": "special", "value": {"kind": "root"}}, "read"),
    ({"type": "special", "value": {"kind": "project_roots"}}, "write"),
    ({"type": "special", "value": {"kind": "slash_tmp"}}, "write"),
    ({"type": "special", "value": {"kind": "tmpdir"}}, "write"),
    ({"type": "special", "value": {"kind": "project_roots", "subpath": ".git"}}, "read"),
    ({"type": "special", "value": {"kind": "project_roots", "subpath": ".agents"}}, "read"),
    ({"type": "special", "value": {"kind": "project_roots", "subpath": ".codex"}}, "read"),
]


def refuse(msg, why):
    return {"id": msg["id"], "error": {"code": -32600, "message": f"ccnm refused {msg.get('method')}: {why}"}}


def inside_root(uri):
    if not isinstance(uri, str) or not uri.startswith("file://"):
        return False
    path = uri[len("file://"):]
    if ".." in path.split("/"):
        return False
    return path == ROOT or path.startswith(ROOT + "/")


def sandbox_problem(sb):
    """原型：sandbox 必须在场，根钉在 ROOT，条目只能是实测 workspace-write 形状里的那几条，网络 restricted。"""
    if not isinstance(sb, dict):
        return "sandbox is required"
    root_uri = "file://" + ROOT
    if sb.get("cwd") not in (None, root_uri) and not inside_root(sb.get("cwd")):
        return "sandbox cwd is outside the workspace"
    if sb.get("workspaceRoots") != [root_uri]:
        return "sandbox workspaceRoots must be exactly the workspace root"
    perms = sb.get("permissions") or {}
    if perms.get("type") != "managed" or perms.get("network") != "restricted":
        return "sandbox permissions must be managed with restricted network"
    fs = perms.get("file_system") or {}
    if fs.get("type") != "restricted":
        return "sandbox file system must be restricted"
    for entry in fs.get("entries") or []:
        if (entry.get("path"), entry.get("access")) not in ALLOWED_ENTRIES:
            return "sandbox entry is wider than the workspace-write shape: " + json.dumps(entry)[:120]
    return None


def ccnm_policy(msg):
    """P21 的规则表原型（Python，只用于实测 Codex 对拒绝的反应；实现归 P22）。返回 None 表示放行。"""
    method = msg.get("method")
    if method is None:
        return None
    params = msg.get("params") or {}
    if "id" not in msg:
        return None if method == "initialized" else {"__drop__": True}
    if method == "initialize":
        return None if params.get("resumeSessionId") is None else refuse(msg, "resume is not supported")
    if method in ("environment/info", "environment/status", "environmentConfig/read") or method in HANDLE_METHODS:
        return None
    if method in READ_METHODS:
        not_found = ancestor_git_not_found(msg)
        if not_found:
            return not_found
        return None if inside_root(params.get("path")) else refuse(msg, "path is outside the workspace")
    if method in WRITE_METHODS:
        for key in ("path", "sourcePath", "destinationPath"):
            if key in params and not inside_root(params[key]):
                return refuse(msg, "path is outside the workspace")
        problem = sandbox_problem(params.get("sandbox"))
        return refuse(msg, problem) if problem else None
    if method == "process/start":
        if not inside_root(params.get("cwd")):
            return refuse(msg, "cwd is outside the workspace")
        problem = sandbox_problem(params.get("sandbox"))
        return refuse(msg, problem) if problem else None
    if method in PROCESS_METHODS:
        return None
    return {"id": msg["id"], "error": {"code": -32601, "message": f"ccnm does not forward {method}"}}


def reset_container():
    r = container(f"rm -rf {ROOT}/* {ROOT}/.[!.]* {OUTSIDE}/* /srv/p21/.git; ls -la {ROOT} {OUTSIDE}")
    return r.stdout


def after_state():
    r = container(f"cd {ROOT} && ls -la && for f in *; do echo \"== $f\"; head -c 300 \"$f\" | tr -cd '[:print:]\\n'; done; "
                  f"echo '== outside'; ls -la {OUTSIDE}")
    return r.stdout


PROBE_CMD = f"pwd; id -un; echo inside > inside.txt; echo outside > {OUTSIDE}/o.txt; echo rc=$?; ls -a"
PATCH = "*** Begin Patch\n*** Add File: patched.txt\n+hello from apply_patch\n*** End Patch"


def exp_codex(mode, cwd_flag, plan, extra=(), intercept=None, approval="decline", setup=None, shared=False):
    def run(out, log):
        global ROOT
        if shared:
            # 两边都有这个路径：Mac 上放 LOCAL 标记，容器里放 REMOTE 标记
            ROOT = str(Path(__file__).resolve().parent / "shared" / "work")
            Path(ROOT).mkdir(parents=True, exist_ok=True)
            (Path(ROOT) / "AGENTS.md").write_text("LOCAL-AGENTS-MARKER\n")
            subprocess.run(["docker", "exec", CONTAINER, "sh", "-c",
                            f"mkdir -p {ROOT} && chown -R runner:runner {Path(ROOT).parent}"], check=True)
        reset_container()
        if shared:
            container(f"echo REMOTE-AGENTS-MARKER > {ROOT}/AGENTS.md")
        if setup:
            log(side="harness", event="setup", cmd=setup, result=container(setup).stdout[-500:])

        model = MockModel(out, plan, log)
        bridge = Bridge(out, log, intercept)
        local_cwd = out / "local-cwd"
        runner = run_exec if mode == "exec" else run_tui
        kwargs = {"approval": approval} if mode == "tui" else {}
        result = runner(out, log, bridge, model, cwd_flag, local_cwd, extra, **kwargs)
        time.sleep(1)
        bridge.messages.flush()
        summary = {"experiment": sys.argv[1], "mode": mode, "cwd_flag": cwd_flag, "local_cwd": str(local_cwd),
                   "bridge_connections": bridge.connections, **result, **summarize(out),
                   "process_output": process_outputs(out), "container_after": after_state(),
                   "markers_in_model_request": markers(out)}
        (out / "summary.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False))
        print(json.dumps(summary, indent=2, ensure_ascii=False)[:6000])
    return run


def shared_root():
    return str(Path(__file__).resolve().parent / "shared" / "work")


BASIC_PLAN = [("exec", PROBE_CMD), ("patch", PATCH), ("say", "done")]

ESCALATE_PLAN = [
    ("js", "const r = await tools.exec_command({cmd: " + json.dumps(f"echo escalated > {OUTSIDE}/esc.txt; echo rc=$?")
     + ", sandbox_permissions: 'require_escalated', justification: 'p21 probe'});\ntext(JSON.stringify(r));"),
    ("patch", f"*** Begin Patch\n*** Add File: {OUTSIDE}/patched-outside.txt\n+outside\n*** End Patch"),
    ("say", "done"),
]
GIT_PLAN = [("exec", "git status --short 2>&1 | head -3; echo rc=$?"), ("say", "done")]

PNG_B64 = ("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")
SURFACE_SETUP = (f"cd {ROOT} && printf 'one\\ntwo\\n' > a.txt && printf 'gone\\n' > b.txt && "
                 f"echo {PNG_B64} | base64 -d > pic.png && cp pic.png {OUTSIDE}/pic.png && mkdir -p sub && echo x > sub/c.txt")
SURFACE_PLAN = [
    ("js", "const r = await tools.skills__list({authority: {kind: 'executor'}});\ntext(JSON.stringify(r));"),
    ("patch", "*** Begin Patch\n*** Update File: a.txt\n*** Move to: moved.txt\n@@\n one\n-two\n+TWO\n"
              "*** Delete File: b.txt\n*** End Patch"),
    ("js", f"const r = await tools.view_image({{path: '{ROOT}/pic.png'}});\ntext(JSON.stringify(r).slice(0, 200));"),
    ("js", f"const r = await tools.view_image({{path: '{OUTSIDE}/pic.png'}});\ntext(JSON.stringify(r).slice(0, 200));"),
    ("js", "text(JSON.stringify(ALL_TOOLS.map(t => t.name)));"),
    ("say", "done"),
]

AGENT_MARKER = Path(__file__).resolve().parent / "agent-side-marker"
PROJECT_CONFIG_SETUP = (f"mkdir -p {ROOT}/.codex && printf '%s\\n' 'model = \"REMOTE-PROJECT-MODEL\"' "
                        f"'[mcp_servers.projectprobe]' 'command = \"/usr/bin/touch\"' "
                        f"'args = [\"{AGENT_MARKER}\"]' > {ROOT}/.codex/config.toml && cat {ROOT}/.codex/config.toml")
UNTRUSTED = ("-c", f'projects."{ROOT}".trust_level="untrusted"')

EXPERIMENTS = {
    "exec-remote-cwd": exp_codex("exec", ROOT, BASIC_PLAN),
    "exec-local-cwd": exp_codex("exec", None, BASIC_PLAN),
    "tui-remote-cwd": exp_codex("tui", ROOT, BASIC_PLAN),
    "tui-no-cwd": exp_codex("tui", None, BASIC_PLAN),
    "tui-escalate-accept": exp_codex("tui", ROOT, ESCALATE_PLAN, approval="accept"),
    "tui-escalate-decline": exp_codex("tui", ROOT, ESCALATE_PLAN, approval="decline"),
    "git-none": exp_codex("tui", ROOT, GIT_PLAN),
    "git-parent-forwarded": exp_codex("tui", ROOT, GIT_PLAN, setup="git init -q /srv/p21"),
    "git-parent-intercepted": exp_codex("tui", ROOT, GIT_PLAN, setup="git init -q /srv/p21",
                                        intercept=ancestor_git_not_found),
    "git-root": exp_codex("tui", ROOT, GIT_PLAN, setup=f"git init -q {ROOT}"),
    "tui-surface": exp_codex("tui", ROOT, SURFACE_PLAN, extra=(NATIVE_KEEP, ("view_image",)), setup=SURFACE_SETUP),
    "policy-basic": exp_codex("tui", ROOT, BASIC_PLAN, intercept=ccnm_policy),
    "policy-surface": exp_codex("tui", ROOT, SURFACE_PLAN, extra=(NATIVE_KEEP, ("view_image",)), setup=SURFACE_SETUP,
                                intercept=ccnm_policy),
    "policy-escalate-accept": exp_codex("tui", ROOT, ESCALATE_PLAN, approval="accept", intercept=ccnm_policy),
    "project-config-trusted": exp_codex("tui", ROOT, GIT_PLAN, setup=PROJECT_CONFIG_SETUP),
    "tui-shared-path": exp_codex("tui", shared_root(), BASIC_PLAN, shared=True),
    "exec-shared-path": exp_codex("exec", shared_root(), BASIC_PLAN, shared=True),
}

if __name__ == "__main__":
    main()
