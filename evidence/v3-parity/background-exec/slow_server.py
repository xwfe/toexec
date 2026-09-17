#!/usr/bin/env python3
"""探针 MCP server（stdio，只用标准库）：工具 slow 睡 seconds 秒再回答。每个请求和通知
（含 notifications/cancelled）连同收到的时刻记到 PROBE_LOG，用来看 Host 等多久、超时后
有没有告诉 server。请求在各自的线程里答，睡着的时候照样能收到取消通知。"""

import json
import os
import sys
import threading
import time

LOCK = threading.Lock()


def log(**fields):
    path = os.environ.get("PROBE_LOG")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps({"t": round(time.time(), 2), **fields}, ensure_ascii=False) + "\n")


def send(message):
    with LOCK:
        sys.stdout.write(json.dumps(message) + "\n")
        sys.stdout.flush()


def handle(request):
    method, params, rid = request.get("method"), request.get("params") or {}, request.get("id")
    log(method=method, params=params if method != "initialize" else None)
    if rid is None:
        return
    if method == "initialize":
        result = {"protocolVersion": params.get("protocolVersion", "2025-06-18"),
                  "serverInfo": {"name": "slow", "version": "0"}, "capabilities": {"tools": {}}}
    elif method == "tools/list":
        result = {"tools": [{"name": "slow", "description": "Sleeps, then answers.",
                             "inputSchema": {"type": "object",
                                             "properties": {"seconds": {"type": "number"}},
                                             "required": ["seconds"]}}]}
    elif method == "tools/call":
        seconds = float((params.get("arguments") or {}).get("seconds", 1))
        time.sleep(seconds)
        log(method="tools/call finished", seconds=seconds)
        result = {"content": [{"type": "text", "text": f"slept {seconds} s"}]}
    elif method == "ping":
        result = {}
    else:
        send({"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": method}})
        return
    send({"jsonrpc": "2.0", "id": rid, "result": result})


def main():
    for line in sys.stdin:
        line = line.strip()
        if line:
            threading.Thread(target=handle, args=(json.loads(line),), daemon=True).start()


if __name__ == "__main__":
    main()
