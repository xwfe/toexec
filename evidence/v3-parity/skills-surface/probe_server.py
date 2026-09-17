#!/usr/bin/env python3
"""探针 MCP server（stdio）：看一个 MCP Host 怎么对待工具 description、prompts 和
SEP-2640 skills 扩展。只用标准库。

它做两件事：
  1. 给出一个 description 很长、里面按固定偏移埋了标记的工具，外加一个 prompt、
     一个 skill——Host 把什么原样交给模型，从模型请求里找标记就知道。
  2. 把收到的每个方法名记到 PROBE_LOG 指向的文件——Host 连上之后到底调了
     哪些方法（prompts/list？resources/list？skills/list？），不用猜。

用法：PROBE_LOG=<文件> python3 probe_server.py
      python3 probe_server.py --layout    只打印标记的位置
"""

import hashlib
import json
import os
import sys

# 标记的 UTF-16 偏移。2048 两侧各放一个，是因为 Claude Code 对 instructions 的
# 上限就是 2048 个 UTF-16 码元（V2-Q1）。
MARKERS = [("Z", 300), ("A", 1000), ("B", 2000), ("C", 2100), ("D", 3000), ("E", 5900)]
TOTAL = 6000
SKILL_MD = (
    "---\nname: probe-skill\ndescription: >\n  A probe skill. MARK_SKILL_DESC.\n---\n\n"
    "# Probe skill\n\nMARK_SKILL_BODY. Base: ${CLAUDE_SKILL_DIR}\n"
)
SKILL_URI = "skill://probe-skill/SKILL.md"


def long_description():
    text = "Probe tool. "
    for name, at in MARKERS:
        token = f"[MARK_{name}_{at}]"
        text += "x" * (at - len(text)) + token
    return text + "y" * (TOTAL - len(text))


def layout():
    text = long_description()
    for name, at in MARKERS:
        token = f"[MARK_{name}_{at}]"
        print(name, "utf16_start", text.index(token), "utf16_end", text.index(token) + len(token))
    print("total", len(text))


def log(**fields):
    path = os.environ.get("PROBE_LOG")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(fields, ensure_ascii=False) + "\n")


def skill_entry():
    raw = SKILL_MD.encode()
    return {
        "uri": SKILL_URI,
        "frontmatter": {"name": "probe-skill", "description": "A probe skill. MARK_SKILL_DESC."},
        "resources": [{"uri": SKILL_URI, "digest": "sha256:" + hashlib.sha256(raw).hexdigest(),
                       "size": len(raw)}],
    }


def answer(method, params):
    if method == "initialize":
        return {
            "protocolVersion": params.get("protocolVersion", "2025-06-18"),
            "serverInfo": {"name": "probe", "version": "0"},
            "instructions": "Probe server. MARK_INSTRUCTIONS.",
            "capabilities": {
                "tools": {}, "prompts": {}, "resources": {},
                "extensions": {"io.modelcontextprotocol/skills": {}},
            },
        }
    if method == "tools/list":
        return {"tools": [{
            "name": "probe_tool",
            "description": long_description(),
            "inputSchema": {"type": "object", "properties": {"x": {"type": "string"}}},
        }]}
    if method == "tools/call":
        return {"content": [{"type": "text", "text": "MARK_TOOL_RESULT"}]}
    if method == "prompts/list":
        return {"prompts": [{
            "name": "probe-prompt", "description": "A probe prompt. MARK_PROMPT_DESC.",
            "arguments": [{"name": "target", "description": "what", "required": False}],
        }]}
    if method == "prompts/get":
        return {"messages": [{"role": "user", "content": {
            "type": "text", "text": "MARK_PROMPT_BODY " + json.dumps(params.get("arguments"))}}]}
    if method == "resources/list":
        return {"resources": []}
    if method == "resources/templates/list":
        return {"resourceTemplates": []}
    if method == "resources/read":
        if params.get("uri") == SKILL_URI:
            return {"contents": [{"uri": SKILL_URI, "mimeType": "text/markdown", "text": SKILL_MD}]}
        raise KeyError("unknown resource")
    if method == "skills/list":
        return {"resultType": "complete", "skills": [skill_entry()],
                "ttlMs": 60000, "cacheScope": "private"}
    if method == "skills/get":
        return {"resultType": "complete", "skill": skill_entry(),
                "ttlMs": 60000, "cacheScope": "private"}
    if method == "ping":
        return {}
    raise NotImplementedError(method)


def main():
    if "--layout" in sys.argv:
        return layout()
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        msg = json.loads(line)
        method = msg.get("method")
        log(method=method, has_id="id" in msg,
            client=(msg.get("params") or {}).get("clientInfo"),
            client_caps=(msg.get("params") or {}).get("capabilities"))
        if "id" not in msg:
            continue
        try:
            reply = {"jsonrpc": "2.0", "id": msg["id"], "result": answer(method, msg.get("params") or {})}
        except NotImplementedError:
            reply = {"jsonrpc": "2.0", "id": msg["id"],
                     "error": {"code": -32601, "message": f"method not found: {method}"}}
        except KeyError as exc:
            reply = {"jsonrpc": "2.0", "id": msg["id"],
                     "error": {"code": -32602, "message": str(exc)}}
        sys.stdout.write(json.dumps(reply) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
