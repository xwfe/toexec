#!/usr/bin/env python3
"""V2-Q1 探针：一个只返回超长 instructions 的最小 MCP stdio 服务器。

标记值从环境变量 Q1_MARKERS 读（逗号分隔），不落盘，模型无法从文件里读到；
没设时进入 --layout 模式，只打印版面（偏移），用于记账。
"""
import json
import os
import sys

FILLER = "这是一段用于测量说明截断位置的中文填充文本，没有任何指令含义。"
# 标记起点（UTF-16 码元偏移，JS 的 string.length 按它计）。
TARGETS = [("Z", 300), ("A", 900), ("B", 2000), ("C", 2100), ("D", 3000)]


def u16(s):
    return len(s.encode("utf-16-le")) // 2


def build(markers):
    text = ""
    line_no = 0
    offsets = []
    for (label, target), value in zip(TARGETS, markers):
        marker_line = f"标记{label}：{value}\n"
        while u16(text) + u16(FILLER) + 8 < target:
            line_no += 1
            text += f"{line_no:03d} {FILLER}\n"
        pad = target - u16(text)
        text += "填" * max(pad, 0)
        start = u16(text)
        byte_start = len(text.encode("utf-8"))
        text += marker_line
        offsets.append(
            {
                "label": label,
                "utf16_start": start,
                "utf16_end": u16(text) - 1,
                "utf8_byte_start": byte_start,
            }
        )
    return text, offsets


def serve(instructions):
    for raw in sys.stdin:
        raw = raw.strip()
        if not raw:
            continue
        msg = json.loads(raw)
        if "id" not in msg:
            continue
        method = msg.get("method")
        if method == "initialize":
            result = {
                "protocolVersion": msg["params"].get("protocolVersion", "2025-06-18"),
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "q1probe", "version": "1"},
                "instructions": instructions,
            }
        elif method == "tools/list":
            result = {
                "tools": [
                    {
                        "name": "noop",
                        "description": "Returns ok.",
                        "inputSchema": {"type": "object", "properties": {}},
                    }
                ]
            }
        elif method == "tools/call":
            result = {"content": [{"type": "text", "text": "ok"}]}
        elif method == "ping":
            result = {}
        else:
            reply = {"jsonrpc": "2.0", "id": msg["id"], "error": {"code": -32601, "message": "not found"}}
            print(json.dumps(reply), flush=True)
            continue
        print(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": result}), flush=True)


if __name__ == "__main__":
    if "--layout" in sys.argv:
        fake = [f"WKQ1-{label}{'0' * 7}" for label, _ in TARGETS]
        text, offsets = build(fake)
        print(json.dumps({"utf16_length": u16(text), "utf8_bytes": len(text.encode("utf-8")), "markers": offsets}, ensure_ascii=False, indent=2))
    else:
        text, _ = build(os.environ["Q1_MARKERS"].split(","))
        serve(text)
