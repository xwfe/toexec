#!/usr/bin/env python3
"""V2-G05 候选方案实测：exec-server 连接能力放在 URL 路径里的一次性随机串。

不发任何真实模型请求：模型接口换成本机假服务，codex 进程树用 sandbox-exec
禁掉非本机出站；CODEX_HOME 是临时目录，里面没有凭据。只用标准库。

用法：harness.py <场景> <输出目录>，场景见 SCENARIOS。
"""

import asyncio
import hmac
import json
import os
import secrets
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

CODEX = os.environ.get("G05_CODEX", "codex")
SANDBOX_PROFILE = """(version 1)
(allow default)
(deny network-outbound)
(allow network-outbound (remote ip "localhost:*"))
(allow network-outbound (remote unix-socket))
"""
PROBE = "G05_PROBE_OK"


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Log:
    def __init__(self, path):
        self.path = path
        self.lock = threading.Lock()

    def __call__(self, **fields):
        fields["t"] = round(time.time(), 3)
        with self.lock, open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(fields, ensure_ascii=False) + "\n")


# ---------------------------------------------------------------- 假模型接口

def shell_call(tools, command, call_id):
    """按请求里实际声明的工具挑一个能跑命令的，参数名随工具走。"""
    names = {t.get("name") for t in tools if isinstance(t, dict)}
    if "exec_command" in names:
        return call_id, "exec_command", {"cmd": command}
    if "shell_command" in names:
        return call_id, "shell_command", {"command": command}
    if "shell" in names:
        return call_id, "shell", {"command": ["bash", "-lc", command]}
    raise RuntimeError(f"no shell-like tool offered: {sorted(n for n in names if n)}")


def sse(events):
    out = []
    for ev in events:
        out.append(f"event: {ev['type']}\ndata: {json.dumps(ev)}\n\n")
    return "".join(out).encode()


def completed(rid):
    return {"type": "response.completed", "response": {"id": rid, "usage": {
        "input_tokens": 0, "input_tokens_details": None, "output_tokens": 0,
        "output_tokens_details": None, "total_tokens": 0}}}


class MockModel:
    """plan 是一串动作：("call", 命令) 发起一次命令调用；("say", 文本) 结束。
    每收到一次 /responses 请求就往下走一步；before_step 可在某一步前触发钩子。"""

    def __init__(self, out, plan, log, before_step=None):
        self.out, self.plan, self.log = out, plan, log
        self.step = 0
        self.shell_offered = []
        self.before_step = before_step or {}
        model = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                model.log(side="model", method="GET", path=self.path)
                body = json.dumps({"data": [], "models": []}).encode()
                self.send_response(200)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                length = int(self.headers.get("content-length") or 0)
                raw = self.rfile.read(length)
                n = model.step
                (model.out / f"model-request-{n}.json").write_bytes(raw)
                model.log(side="model", method="POST", path=self.path, step=n, bytes=len(raw))
                if not self.path.rstrip("/").endswith("/responses"):
                    self.send_response(404)
                    self.end_headers()
                    return
                body = json.loads(raw or b"{}")
                if n in model.before_step:
                    model.before_step[n]()
                action = model.plan[min(n, len(model.plan) - 1)]
                model.step += 1
                rid = f"resp_{n}"
                events = [{"type": "response.created", "response": {"id": rid}}]
                offered = {t.get("name") for t in body.get("tools", []) if isinstance(t, dict)}
                model.shell_offered.append(bool(offered & {"exec_command", "shell_command", "shell"}))
                if action[0] == "call" and not model.shell_offered[-1]:
                    action = ("say", "no shell tool offered")
                if action[0] == "call":
                    call_id, name, args = shell_call(body.get("tools", []), action[1], f"call_{n}")
                    events.append({"type": "response.output_item.done", "item": {
                        "type": "function_call", "call_id": call_id, "name": name,
                        "arguments": json.dumps(args)}})
                else:
                    events.append({"type": "response.output_item.done", "item": {
                        "type": "message", "role": "assistant", "id": f"msg_{n}",
                        "content": [{"type": "output_text", "text": action[1]}]}})
                events.append(completed(rid))
                payload = sse(events)
                self.send_response(200)
                self.send_header("content-type", "text/event-stream")
                self.send_header("content-length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()


# ---------------------------------------------------------------- 本机桥

class Bridge:
    """只接受路径与令牌完全一致的一次升级，成功后立即作废；转发时把路径改成 /，
    上游 exec-server 看不到令牌。日志里不写令牌本身。"""

    def __init__(self, token, upstream_port, log):
        self.expected = b"/" + token.encode()
        self.upstream_port = upstream_port
        self.log = log
        self.used = False
        self.accepted = 0
        self.refused = []
        self.bytes_up = 0
        self.bytes_down = 0
        self.live = []
        self.capture = None
        self.loop = asyncio.new_event_loop()
        self.port = free_port()
        ready = threading.Event()

        def run():
            asyncio.set_event_loop(self.loop)
            self.server = self.loop.run_until_complete(
                asyncio.start_server(self.handle, "127.0.0.1", self.port))
            ready.set()
            self.loop.run_forever()

        threading.Thread(target=run, daemon=True).start()
        ready.wait()

    async def handle(self, reader, writer):
        try:
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 10)
        except Exception as exc:  # noqa: BLE001 - 记录后关闭即可
            self.refused.append("bad-head")
            self.log(side="bridge", event="refused", reason="bad-head", error=type(exc).__name__)
            writer.close()
            return
        line, rest = head.split(b"\r\n", 1)
        parts = line.split(b" ")
        target = parts[1] if len(parts) == 3 else b""
        path_ok = hmac.compare_digest(target, self.expected)
        if not path_ok or self.used:
            reason = "replayed" if path_ok else "wrong-path"
            self.refused.append(reason)
            self.log(side="bridge", event="refused", reason=reason, target_len=len(target))
            writer.write(b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
            await writer.drain()
            writer.close()
            return
        self.used = True
        self.accepted += 1
        self.log(side="bridge", event="accepted")
        up_r, up_w = await asyncio.open_connection("127.0.0.1", self.upstream_port)
        up_w.write(parts[0] + b" / " + parts[2] + b"\r\n" + rest)
        await up_w.drain()
        self.live.append((writer, up_w))

        async def pipe(src, dst, attr):
            try:
                while True:
                    chunk = await src.read(65536)
                    if not chunk:
                        break
                    setattr(self, attr, getattr(self, attr) + len(chunk))
                    if attr == "bytes_down" and self.capture:
                        # 服务端到客户端的帧不加掩码，录下来能直接看到进程输出从哪条连接回来。
                        with open(self.capture, "ab") as f:
                            f.write(chunk)
                    dst.write(chunk)
                    await dst.drain()
            except Exception:  # noqa: BLE001 - 任一侧断开都结束这条转发
                pass
            finally:
                dst.close()

        await asyncio.gather(pipe(reader, up_w, "bytes_up"), pipe(up_r, writer, "bytes_down"))
        self.log(side="bridge", event="closed")

    def drop_all(self):
        """模拟传输层断开：两侧都关掉。令牌已经用过，重连只会被拒。"""
        def close():
            for a, b in self.live:
                a.close()
                b.close()
            self.live.clear()
        self.loop.call_soon_threadsafe(close)
        self.log(side="bridge", event="dropped")
        time.sleep(0.5)


def raw_upgrade(port, path):
    with socket.create_connection(("127.0.0.1", port), timeout=5) as s:
        s.sendall(
            f"GET {path} HTTP/1.1\r\nHost: 127.0.0.1\r\nUpgrade: websocket\r\n"
            "Connection: Upgrade\r\nSec-WebSocket-Version: 13\r\n"
            "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n\r\n".encode())
        return s.recv(64).split(b"\r\n", 1)[0].decode()


# ---------------------------------------------------------------- 编排

SCENARIOS = {
    # 经 exec-server 起的命令能不能读到 CODEX_EXEC_SERVER_URL。
    "env": {"url_token": "real", "plan": [("call", f"echo {PROBE}; "
            'if [ -n "$CODEX_EXEC_SERVER_URL" ]; then echo URL_ENV_PRESENT; else echo URL_ENV_ABSENT; fi'),
            ("say", "done")]},
    # 令牌对：一次命令经 exec-server 跑完，然后收尾。
    "ok": {"url_token": "real", "plan": [("call", f"echo {PROBE}; pwd"), ("say", "done")]},
    # Codex 手里的令牌不对（比如桥重启过）：看失败时这个串流到哪。
    "wrong": {"url_token": "other", "plan": [("call", f"echo {PROBE}"), ("say", "done")]},
    # 第一条命令成功后断线；第二条命令逼客户端重连，令牌已作废。
    "drop": {"url_token": "real", "plan": [
        ("call", f"echo {PROBE}"), ("call", "echo AFTER_DROP"), ("say", "done")],
        "drop_before_step": 1},
}


def main():
    name, out = sys.argv[1], Path(sys.argv[2]).resolve()
    scenario = SCENARIOS[name]
    out.mkdir(parents=True, exist_ok=False)
    log = Log(out / "harness.jsonl")
    token = secrets.token_hex(32)  # 256 位
    held = token if scenario["url_token"] == "real" else secrets.token_hex(32)
    (out / "secrets.json").write_text(json.dumps({"bridge": token, "codex": held}))

    home = out / "home"
    work = out / "work"
    server_home = out / "server-home"
    for d in (home / ".codex", work, server_home / ".codex"):
        d.mkdir(parents=True)
    (work / "hello.txt").write_text("hello\n")

    server_port = free_port()
    server_env = {"HOME": str(server_home), "CODEX_HOME": str(server_home / ".codex"),
                  "PATH": os.environ["PATH"]}
    server = subprocess.Popen(
        [CODEX, "exec-server", "--listen", f"ws://127.0.0.1:{server_port}"],
        env=server_env, stdin=subprocess.DEVNULL,
        stdout=open(out / "exec-server.stdout", "wb"), stderr=open(out / "exec-server.stderr", "wb"))
    for _ in range(100):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{server_port}/readyz", timeout=1) as r:
                if r.status == 200:
                    break
        except Exception:  # noqa: BLE001 - 还没起来
            time.sleep(0.1)
    else:
        raise SystemExit("exec-server not ready")
    log(side="harness", event="server-ready", version=subprocess.run(
        [CODEX, "--version"], capture_output=True, text=True, env=server_env).stdout.strip())

    bridge = Bridge(token, server_port, log)
    bridge.capture = out / "bridge-down.bin"
    hooks = {}
    if "drop_before_step" in scenario:
        hooks[scenario["drop_before_step"]] = bridge.drop_all
    model = MockModel(out, scenario["plan"], log, hooks)

    profile = out / "no-egress.sb"
    profile.write_text(SANDBOX_PROFILE)
    env = {"HOME": str(home), "CODEX_HOME": str(home / ".codex"), "PATH": os.environ["PATH"],
           "CODEX_EXEC_SERVER_URL": f"ws://127.0.0.1:{bridge.port}/{held}"}
    provider = "g05mock"
    cmd = ["sandbox-exec", "-f", str(profile), CODEX, "exec", "--ignore-user-config",
           "--skip-git-repo-check", "--json", "-s", "workspace-write",
           "-c", f'model_provider="{provider}"',
           "-c", f'model_providers.{provider}.name="{provider}"',
           "-c", f'model_providers.{provider}.base_url="http://127.0.0.1:{model.port}/v1"',
           "-c", f'model_providers.{provider}.wire_api="responses"',
           "-c", f"model_providers.{provider}.requires_openai_auth=false",
           "-m", "gpt-5.1-codex", "run the probe"]
    started = time.time()
    try:
        proc = subprocess.run(cmd, cwd=work, env=env, stdin=subprocess.DEVNULL,
                              capture_output=True, timeout=180)
        rc, stdout, stderr = proc.returncode, proc.stdout, proc.stderr
    except subprocess.TimeoutExpired as exc:
        rc, stdout, stderr = "timeout", exc.stdout or b"", exc.stderr or b""
    (out / "codex.stdout").write_bytes(stdout)
    (out / "codex.stderr").write_bytes(stderr)
    log(side="harness", event="codex-exited", rc=rc, seconds=round(time.time() - started, 2))

    checks = {
        "bridge_token_after_run": raw_upgrade(bridge.port, "/" + token),
        "wrong_token": raw_upgrade(bridge.port, "/" + secrets.token_hex(32)),
        "no_path": raw_upgrade(bridge.port, "/"),
    }
    server.terminate()
    try:
        server.wait(10)
    except subprocess.TimeoutExpired:
        server.kill()
    time.sleep(0.3)

    hits = scan(out, {"bridge_token": token, "codex_url_token": held})
    summary = {
        "scenario": name, "codex_rc": rc, "bridge_accepted": bridge.accepted,
        "bridge_refused": bridge.refused, "bytes_up": bridge.bytes_up,
        "bytes_down": bridge.bytes_down, "model_requests": model.step,
        "shell_tool_offered": model.shell_offered,
        "env_marker": sorted({m for p in out.glob("model-request-*.json")
                              for m in ("URL_ENV_PRESENT", "URL_ENV_ABSENT")
                              if (m + "\\n").encode() in p.read_bytes()}),
        "post_checks": checks, "secret_hits": hits,
        "probe_in_exec_server_downstream": probe_in_downstream(out / "bridge-down.bin"),
        "probe_in_model_requests": [p.name for p in sorted(out.glob("model-request-*.json"))
                                    if PROBE.encode() in p.read_bytes()],
    }
    (out / "summary.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False))
    print(json.dumps(summary, indent=2, ensure_ascii=False))


def probe_in_downstream(path):
    """exec-server 把进程输出按 base64 发回；解码下行里像 base64 的串找探针。"""
    if not path.exists():
        return False
    import base64
    import re
    for chunk in re.findall(rb"[A-Za-z0-9+/]{12,}={0,2}", path.read_bytes()):
        try:
            if PROBE.encode() in base64.b64decode(chunk + b"=" * (-len(chunk) % 4)):
                return True
        except ValueError:
            continue
    return False


def scan(root, needles):
    """在所有产物里找令牌：全长和前 16 个字符各找一遍，只报出现位置，不打印令牌。"""
    hits = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.name in ("secrets.json", "summary.json"):
            continue
        data = path.read_bytes()
        for label, secret in needles.items():
            for variant, needle in (("full", secret), ("prefix16", secret[:16])):
                count = data.count(needle.encode())
                if count:
                    hits.setdefault(str(path.relative_to(root)), []).append(f"{label}:{variant}x{count}")
    return hits


if __name__ == "__main__":
    main()
