#!/usr/bin/env python3
"""最小的 stdio MCP server：一个只读工具 read_file，一个会写的工具 apply_patch。

用来冒充 ccnm 的远端工具：看受管 Claude Code 在各种 --tools 组合下，模型拿到的
工具表里有没有它们、子代理能不能拿到。只用标准库。
"""

import json
import sys

TOOLS = [
    {"name": "read_file", "description": "Read a file on the Runtime.",
     "inputSchema": {"type": "object", "properties": {"path": {"type": "string"}}},
     "annotations": {"readOnlyHint": True}},
    {"name": "apply_patch", "description": "Change files on the Runtime.",
     "inputSchema": {"type": "object", "properties": {"patch": {"type": "string"}}},
     "annotations": {"readOnlyHint": False, "destructiveHint": True}},
]


def reply(msg_id, result):
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": msg_id, "result": result}) + "\n")
    sys.stdout.flush()


for line in sys.stdin:
    try:
        msg = json.loads(line)
    except ValueError:
        continue
    method, msg_id = msg.get("method"), msg.get("id")
    if msg_id is None:
        continue
    if method == "initialize":
        reply(msg_id, {"protocolVersion": msg.get("params", {}).get("protocolVersion", "2025-06-18"),
                       "capabilities": {"tools": {}}, "serverInfo": {"name": "tiny", "version": "0"}})
    elif method == "tools/list":
        reply(msg_id, {"tools": TOOLS})
    elif method == "tools/call":
        reply(msg_id, {"content": [{"type": "text", "text": "tiny ok"}]})
    else:
        reply(msg_id, {})
