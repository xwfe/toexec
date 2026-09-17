#!/usr/bin/env python3
"""探针 MCP server（stdio，只用标准库）：工具结果里带图片、带文件内容时，Host 交给
模型的是什么。

工具 media_probe 按参数 kind 返回一种内容块：
  image         {"type":"image"}，一张 2x2 的 PNG
  resource_png  {"type":"resource"}，blob 是同一张 PNG
  resource_pdf  {"type":"resource"}，blob 是一个最小的 PDF
每个结果都带一段文本标记 MARK_TOOL_TEXT，方便在模型请求里定位。收到的方法和
tools/call 参数记到 PROBE_LOG。
"""

import base64
import json
import os
import struct
import sys
import zlib


def png_2x2():
    def chunk(kind, data):
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)
    raw = b"".join(b"\x00" + b"\xff\x00\x00" * 2 for _ in range(2))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 2, 2, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


PNG_B64 = base64.b64encode(png_2x2()).decode()
PDF_B64 = base64.b64encode(
    b"%PDF-1.1\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
    b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
    b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 72 72]>>endobj\n"
    b"trailer<</Root 1 0 R>>\n%%EOF\n").decode()


def log(**fields):
    path = os.environ.get("PROBE_LOG")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(fields, ensure_ascii=False) + "\n")


def result(kind):
    text = {"type": "text", "text": f"MARK_TOOL_TEXT kind={kind}"}
    if kind == "image":
        block = {"type": "image", "data": PNG_B64, "mimeType": "image/png"}
    elif kind == "resource_png":
        block = {"type": "resource", "resource": {
            "uri": "file:///probe/red.png", "mimeType": "image/png", "blob": PNG_B64}}
    elif kind == "resource_pdf":
        block = {"type": "resource", "resource": {
            "uri": "file:///probe/blank.pdf", "mimeType": "application/pdf", "blob": PDF_B64}}
    else:
        return {"content": [text], "isError": True}
    return {"content": [text, block]}


def answer(method, params):
    if method == "initialize":
        return {"protocolVersion": params.get("protocolVersion", "2025-06-18"),
                "serverInfo": {"name": "media", "version": "0"},
                "capabilities": {"tools": {}}}
    if method == "tools/list":
        return {"tools": [{
            "name": "media_probe",
            "description": "Returns one kind of content block.",
            "inputSchema": {"type": "object", "properties": {"kind": {"type": "string"}},
                            "required": ["kind"]},
        }]}
    if method == "tools/call":
        return result((params.get("arguments") or {}).get("kind"))
    if method == "ping":
        return {}
    raise NotImplementedError(method)


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        msg = json.loads(line)
        method = msg.get("method")
        log(method=method, arguments=(msg.get("params") or {}).get("arguments"))
        if "id" not in msg:
            continue
        try:
            reply = {"jsonrpc": "2.0", "id": msg["id"], "result": answer(method, msg.get("params") or {})}
        except NotImplementedError:
            reply = {"jsonrpc": "2.0", "id": msg["id"],
                     "error": {"code": -32601, "message": f"method not found: {method}"}}
        sys.stdout.write(json.dumps(reply) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
