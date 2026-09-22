"""一个最小的 MCP server，给第 4 步当"Agent 机器上装好的 server"。

`python3 fake_agent_server.py` 走 stdio；`python3 fake_agent_server.py --http <端口文件>`
在 127.0.0.1 的随机端口上走 streamable HTTP（只回 JSON，不开 SSE），把端口写进那个文件。
两种都说 2025-06-18。工具：

- echo：原样回 arguments；
- big：52 000 字节（1000 行，每行 52 字节），和 ccnm P49 的测试同一个大小；
- huge：400 000 字节（8000 行，每行 50 字节），大约 10 万 token，超过 Claude Code 默认的
  MCP 输出上限；
- pid：回自己的进程号；
- sized：回 `bytes` 个字节（每行 64 字节，行号打头），用来找客户端从多大开始截；
- sized_meta：同上，工具定义里带 `_meta["anthropic/maxResultSizeChars"] = 200000`；
- env：自己拿到的环境变量名，和 `PROBE_` 开头的那几个的值（只放测试自己造的假值）。
"""

import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

TOOLS = [
    {"name": "echo", "description": "Echo the arguments back.", "inputSchema": {"type": "object"}},
    {"name": "big", "description": "52 000 bytes of text.", "inputSchema": {"type": "object"}},
    {"name": "huge", "description": "400 000 bytes of text.", "inputSchema": {"type": "object"}},
    {"name": "pid", "description": "Its own process id.", "inputSchema": {"type": "object"}},
    {"name": "sized", "description": "Exactly `bytes` bytes of text.",
     "inputSchema": {"type": "object", "properties": {"bytes": {"type": "integer"}}}},
    {"name": "sized_meta", "description": "Same, declaring a larger result size to Claude Code.",
     "inputSchema": {"type": "object", "properties": {"bytes": {"type": "integer"}}},
     "_meta": {"anthropic/maxResultSizeChars": 200000}},
    {"name": "env", "description": "The names of its environment variables.",
     "inputSchema": {"type": "object"}},
]


def sized(n):
    lines = "".join(f"{i:07d} {'z' * 55}\n" for i in range(n // 64 + 1))
    return lines[:n]


def text(body):
    return {"content": [{"type": "text", "text": body}]}


def answer(message):
    """一条请求的回复；通知返回 None。"""
    if "id" not in message:
        return None
    method = message.get("method")
    if method == "initialize":
        result = {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}},
                  "serverInfo": {"name": "fake-agent", "version": "1.0"}}
    elif method == "tools/list":
        result = {"tools": TOOLS}
    elif method == "tools/call":
        name = message["params"]["name"]
        arguments = message["params"].get("arguments") or {}
        if name == "echo":
            result = text(json.dumps(arguments, sort_keys=True))
        elif name == "big":
            result = text("".join(f"line {i:05d} {'x' * 40}\n" for i in range(1000)))
        elif name == "huge":
            result = text("".join(f"row {i:05d} {'y' * 39}\n" for i in range(8000)))
        elif name == "pid":
            result = text(str(os.getpid()))
        elif name in ("sized", "sized_meta"):
            result = text(sized(int(arguments.get("bytes", 0))))
        elif name == "env":
            result = text(json.dumps({"names": sorted(os.environ),
                                      "probe": {k: v for k, v in os.environ.items()
                                                if k.startswith("PROBE_")}}))
        else:
            result = {**text("no such tool"), "isError": True}
    elif method == "ping":
        result = {}
    else:
        return {"jsonrpc": "2.0", "id": message["id"],
                "error": {"code": -32601, "message": "method not found"}}
    return {"jsonrpc": "2.0", "id": message["id"], "result": result}


def stdio():
    for line in sys.stdin:
        try:
            message = json.loads(line)
        except ValueError:
            continue
        reply = answer(message)
        if reply is not None:
            sys.stdout.write(json.dumps(reply) + "\n")
            sys.stdout.flush()


def http(port_file):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            self.send_response(405)
            self.end_headers()

        def do_DELETE(self):
            self.send_response(200)
            self.end_headers()

        def do_POST(self):
            raw = self.rfile.read(int(self.headers.get("content-length") or 0))
            message = json.loads(raw)
            reply = answer(message)
            if reply is None:
                self.send_response(202)
                self.end_headers()
                return
            body = json.dumps(reply).encode()
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(body)))
            if message.get("method") == "initialize":
                self.send_header("mcp-session-id", "fake-session")
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    with open(port_file, "w") as f:
        f.write(str(server.server_address[1]))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    threading.Event().wait()


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--http":
        http(sys.argv[2])
    else:
        stdio()
