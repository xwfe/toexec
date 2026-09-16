#!/usr/bin/env python3
"""V2-G01 协议实测：codex exec-server（stdio）真正接受什么、拒绝什么。

中立客户端，不 import 任何 Codex 代码，只用标准库；不起 Agent、不发模型请求。
每一项只记录实际看到的响应，判定写在 README，不写在这里。

用法：probe.py <新输出目录>
"""

import json
import os
import queue
import secrets
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

CODEX = os.environ.get("G01_CODEX", "codex")
REGISTERED = [  # 源码 exec-server/src/server/registry.rs 里注册的全部方法（rust-v0.154.0）
    "initialize", "http/request", "process/start", "environment/info", "environmentConfig/read",
    "environment/status", "capabilityRoots/discoverV1", "process/read", "process/write",
    "process/signal", "process/terminate", "fs/readFile", "fs/open", "fs/readBlock", "fs/close",
    "fs/writeFile", "fs/createDirectory", "fs/getMetadata", "fs/canonicalize",
    "fs/readDirectory", "fs/walk", "fs/remove", "fs/copy",
]
NOT_REGISTERED = ["process/output", "process/exited", "network/policyRequest", "shutdown", "exec"]


class Server:
    def __init__(self, out, name, extra_env=None):
        home = Path(tempfile.mkdtemp(prefix=f"g01-{name}-", dir=out))
        (home / ".codex").mkdir()
        env = {"HOME": str(home), "CODEX_HOME": str(home / ".codex"), "PATH": os.environ["PATH"]}
        env.update(extra_env or {})
        self.proc = subprocess.Popen([CODEX, "exec-server", "--listen", "stdio"], env=env,
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=open(out / f"{name}.stderr", "wb"))
        self.lines = queue.Queue()
        self.next_id = 1
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        for raw in self.proc.stdout:
            self.lines.put(raw)
        self.lines.put(None)

    def send_raw(self, data):
        try:
            self.proc.stdin.write(data)
            self.proc.stdin.flush()
            return True
        except (BrokenPipeError, OSError):
            return False

    def send(self, obj):
        return self.send_raw(json.dumps(obj).encode() + b"\n")

    def request(self, method, params=None, **extra):
        rid = self.next_id
        self.next_id += 1
        msg = {"jsonrpc": "2.0", "id": rid, "method": method}
        if params is not None:
            msg["params"] = params
        msg.update(extra)
        self.send(msg)
        return self.wait_id(rid)

    def wait_id(self, rid, timeout=10):
        deadline = time.time() + timeout
        while time.time() < deadline:
            msg = self.next_message(deadline - time.time())
            if msg is None or msg == "closed":
                return msg
            if msg.get("id") == rid:
                return msg
        return None

    def next_message(self, timeout=5):
        try:
            raw = self.lines.get(timeout=max(timeout, 0.01))
        except queue.Empty:
            return None
        if raw is None:
            return "closed"
        try:
            return json.loads(raw)
        except ValueError:
            return {"unparseable_line": raw[:200].decode(errors="replace")}

    def drain(self, seconds=0.5):
        got = []
        end = time.time() + seconds
        while time.time() < end:
            m = self.next_message(end - time.time())
            if m is None:
                break
            got.append(m)
            if m == "closed":
                break
        return got

    def handshake(self):
        resp = self.request("initialize", {"clientName": "g01-probe"})
        self.send({"jsonrpc": "2.0", "method": "initialized", "params": {}})
        return resp

    def close(self):
        try:
            self.proc.stdin.close()
        except OSError:
            pass
        try:
            self.proc.wait(10)
        except subprocess.TimeoutExpired:
            self.proc.kill()
        return self.proc.returncode


def short(msg):
    if isinstance(msg, dict) and "error" in msg:
        return {"id": msg.get("id"), "error": {"code": msg["error"].get("code"),
                                                "message": str(msg["error"].get("message"))[:160]}}
    if isinstance(msg, dict) and "result" in msg:
        return {"id": msg.get("id"), "result_keys": sorted(msg["result"].keys())
                if isinstance(msg["result"], dict) else type(msg["result"]).__name__}
    return msg


def main():
    out = Path(sys.argv[1]).resolve()
    out.mkdir(parents=True, exist_ok=False)
    r = {"codex_version": subprocess.run([CODEX, "--version"], capture_output=True, text=True).stdout.strip()}

    # 1. 握手之前就调方法
    s = Server(out, "before-init")
    r["request_before_initialize"] = short(s.request("environment/info", {}))
    r["initialize_after_that"] = short(s.handshake())
    s.close()

    # 2. 正常握手：响应字段、多余字段（冒充的版本号）是否被忽略、重复 initialize
    s = Server(out, "handshake")
    first = s.request("initialize", {"clientName": "g01-probe", "protocolVersion": "999.0",
                                     "unknownField": True})
    r["initialize_with_unknown_fields"] = short(first)
    if isinstance(first, dict) and "result" in first:
        info = first["result"].get("environmentInfo") or {}
        r["initialize_result"] = {"sessionId_present": bool(first["result"].get("sessionId")),
                                  "environmentInfo_keys": sorted(info.keys()),
                                  "executorVersion": info.get("executorVersion")}
    r["request_before_initialized_notification"] = short(s.request("environment/info", {}))
    s.send({"jsonrpc": "2.0", "method": "initialized", "params": {}})
    r["second_initialize"] = short(s.request("initialize", {"clientName": "again"}))

    r["exit_code_on_stdin_close"] = s.close()

    # 3/4. 每种异常输入单独一个服务端：记下回了什么、之后连接还在不在
    def isolated(name, action):
        srv = Server(out, name)
        srv.handshake()
        srv.drain(0.3)
        seen = action(srv)
        follow = srv.request("environment/info")
        srv.close()
        return {"seen": seen, "follow_up_environment_info": short(follow)}

    r["unknown_notification"] = isolated("unknown-notify", lambda srv: (
        srv.send({"jsonrpc": "2.0", "method": "bogus/notify", "params": {}}),
        [short(m) for m in srv.drain(1.0)])[1])
    r["unknown_method"] = isolated("unknown-method", lambda srv: short(srv.request("bogus/method", {})))
    r["malformed_json_line"] = isolated("malformed", lambda srv: (
        srv.send_raw(b"{not json\n"), [short(m) for m in srv.drain(1.0)])[1])
    r["missing_jsonrpc_field"] = isolated("no-jsonrpc", lambda srv: (
        srv.send({"id": 77, "method": "environment/info"}), [short(m) for m in srv.drain(1.0)])[1])
    r["string_id"] = isolated("string-id", lambda srv: (
        srv.send({"jsonrpc": "2.0", "id": "s-1", "method": "environment/info"}),
        [short(m) for m in srv.drain(1.0)])[1])
    r["null_id"] = isolated("null-id", lambda srv: (
        srv.send({"jsonrpc": "2.0", "id": None, "method": "environment/info"}),
        [short(m) for m in srv.drain(1.0)])[1])
    r["unknown_field_in_params"] = isolated("unknown-param", lambda srv: short(
        srv.request("process/read", {"processId": "nope", "extraField": 1})))

    # 方法表：空参数调一遍，只看是"方法不存在"还是别的；连接一断就换新实例接着测
    reg = {}
    srv = Server(out, "method-table")
    srv.handshake()
    srv.drain(0.3)
    for method in REGISTERED[1:] + NOT_REGISTERED:
        resp = srv.request(method, {})
        if resp in (None, "closed"):
            reg[method] = resp or "no-response"
            srv.close()
            srv = Server(out, "method-table")
            srv.handshake()
            srv.drain(0.3)
            continue
        reg[method] = "result" if "result" in resp else resp["error"]["code"]
        srv.drain(0.2)
    srv.close()
    r["method_table_with_empty_params"] = reg

    # 5. 大帧：略小于 64 MiB 上限的合法请求，与略超过上限的一行
    s = Server(out, "large-ok")
    s.handshake()
    s.drain(0.3)
    pad = "x" * (63 * 1024 * 1024)
    started = time.time()
    s.send({"jsonrpc": "2.0", "id": 900, "method": "process/read", "params": {"processId": "nope", "pad": pad}})
    r["frame_63MiB"] = {"response": short(s.wait_id(900, 60)), "seconds": round(time.time() - started, 2)}
    s.close()
    s = Server(out, "large-over")
    s.handshake()
    s.drain(0.3)
    started = time.time()
    ok = s.send_raw(b'{"jsonrpc":"2.0","id":901,"method":"environment/info","params":{"pad":"'
                    + b"x" * (64 * 1024 * 1024 + 16) + b'"}}\n')
    r["frame_over_64MiB"] = {"write_completed": ok, "after": [short(m) for m in s.drain(5.0)],
                             "seconds": round(time.time() - started, 2),
                             "server_exit_code": s.proc.poll()}
    s.close()

    # 6. stdin 关闭时，它起的进程会不会被清理
    s = Server(out, "eof")
    s.handshake()
    s.drain(0.3)
    marker = "G01MARK" + secrets.token_hex(6)
    work = Path(tempfile.mkdtemp(prefix="g01-work-", dir=out))
    start = s.request("process/start", {
        "processId": "p1", "argv": ["/bin/sh", "-c", f"sleep 300; : {marker}"],
        "cwd": work.as_uri(), "env": {"PATH": "/usr/bin:/bin"}, "tty": False, "arg0": None})
    time.sleep(0.5)
    before = subprocess.run(["pgrep", "-f", marker], capture_output=True, text=True).stdout.split()
    code = s.close()
    time.sleep(2)
    after = subprocess.run(["pgrep", "-f", marker], capture_output=True, text=True).stdout.split()
    r["stdin_eof_with_running_process"] = {"process_start": short(start), "running_before": len(before),
                                           "server_exit_code": code, "still_running_2s_after": len(after)}
    for pid in after:
        subprocess.run(["kill", pid])

    # 7. WebSocket 监听：带 Origin 头的升级请求
    port_sock = socket.socket()
    port_sock.bind(("127.0.0.1", 0))
    port = port_sock.getsockname()[1]
    port_sock.close()
    home = Path(tempfile.mkdtemp(prefix="g01-ws-", dir=out))
    ws = subprocess.Popen([CODEX, "exec-server", "--listen", f"ws://127.0.0.1:{port}"],
                          env={"HOME": str(home), "CODEX_HOME": str(home), "PATH": os.environ["PATH"]},
                          stdout=subprocess.DEVNULL, stderr=open(out / "ws.stderr", "wb"))
    for _ in range(100):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/readyz", timeout=1)
            break
        except Exception:  # noqa: BLE001 - 还没起来
            time.sleep(0.1)

    def upgrade(extra):
        with socket.create_connection(("127.0.0.1", port), timeout=5) as c:
            c.sendall(("GET / HTTP/1.1\r\nHost: 127.0.0.1\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                       "Sec-WebSocket-Version: 13\r\nSec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n"
                       + extra + "\r\n").encode())
            return c.recv(64).split(b"\r\n", 1)[0].decode()

    r["ws_upgrade_without_origin"] = upgrade("")
    r["ws_upgrade_with_origin"] = upgrade("Origin: http://evil.example\r\n")
    ws.terminate()
    ws.wait(10)

    (out / "results.json").write_text(json.dumps(r, indent=2, ensure_ascii=False))
    print(json.dumps(r, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
