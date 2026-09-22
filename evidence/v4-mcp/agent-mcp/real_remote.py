#!/usr/bin/env python3
"""真实的远端 server 经真实 ccnm 的 Agent 端服务（`ccnm internal agent-skills`，P50）和它背后的
curl 连一次：DeepWiki（公开、免费、不用 key，第 2 步 call_sizes.py 调过的同一个地址和同两个
只读工具）。

不经模型：这里直接当 MCP 客户端，看 ccnm 那一跳——TLS、代理、SSE、会话号——在真实网络上
通不通，以及 read_wiki_contents 那个 839 KB 的结果能不能经 read_mcp_result 一段段读全。
只记大小和页数，不记内容。临时 HOME 里只有 deepwiki 一条，不读本机的 ~/.claude.json。
代理照当前环境的 HTTP(S)_PROXY 交给 ccnm（它再交给 curl）。

用法：CCNM_BIN=<ccnm> python3 real_remote.py <输出文件>
"""

import base64
import json
import os
import subprocess
import sys
import tempfile
import shutil
import time
from pathlib import Path

CCNM = os.environ["CCNM_BIN"]
URL = "https://mcp.deepwiki.com/mcp"
REPO = "modelcontextprotocol/rust-sdk"


class Client:
    def __init__(self, argv, env):
        self.proc = subprocess.Popen(argv, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE)
        self.next = 0

    def call(self, method, params):
        self.next += 1
        line = json.dumps({"jsonrpc": "2.0", "id": self.next, "method": method, "params": params})
        self.proc.stdin.write(line.encode() + b"\n")
        self.proc.stdin.flush()
        while True:
            raw = self.proc.stdout.readline()
            if not raw:
                raise RuntimeError(self.proc.stderr.read().decode(errors="replace")[-400:])
            message = json.loads(raw)
            if message.get("id") == self.next:
                if "error" in message:
                    raise RuntimeError(message["error"])
                return message["result"]

    def tool(self, name, arguments):
        return self.call("tools/call", {"name": name, "arguments": arguments})

    def close(self):
        self.proc.stdin.close()
        self.proc.wait(timeout=30)


def texts(result):
    return [c["text"] for c in result.get("content", []) if c.get("type") == "text"]


def main():
    out = Path(sys.argv[1])
    home = Path(tempfile.mkdtemp(prefix="ccnm-real-remote-")).resolve()
    (home / ".claude.json").write_text(json.dumps({"mcpServers": {
        "deepwiki": {"type": "http", "url": URL}}}))
    body = {"protocol": 1, "home": str(home), "session": "real-remote", "mcp": {}}
    payload = base64.urlsafe_b64encode(json.dumps(body).encode()).decode().rstrip("=")
    env = {"PATH": os.environ["PATH"]}
    env.update({k: v for k, v in os.environ.items() if k.lower() in ("http_proxy", "https_proxy",
                                                                   "no_proxy")})
    client = Client([CCNM, "internal", "agent-skills", "--payload", payload], env)
    client.call("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                               "clientInfo": {"name": "real-remote-probe", "version": "1"}})
    record = {"ccnm": subprocess.run([CCNM, "--version"], capture_output=True, text=True).stdout.strip(),
              "url": URL, "proxy_vars_passed": sorted(k for k in env if k != "PATH")}
    tools = client.call("tools/list", {})["tools"]
    record["tools"] = sorted(t["name"] for t in tools)
    call = "call_mcp_tool"

    started = time.monotonic()
    listed = client.tool(call, {"server": "deepwiki"})
    record["list"] = {"is_error": bool(listed.get("isError")), "seconds": round(time.monotonic() - started, 2),
                      "first_line": texts(listed)[0].splitlines()[0][:120]}

    started = time.monotonic()
    structure = client.tool(call, {"server": "deepwiki", "tool": "read_wiki_structure",
                                   "arguments": {"repoName": REPO}})
    record["read_wiki_structure"] = {"is_error": bool(structure.get("isError")),
                                     "seconds": round(time.monotonic() - started, 2),
                                     "text_bytes": sum(len(t.encode()) for t in texts(structure)),
                                     "blocks": len(structure.get("content", []))}

    started = time.monotonic()
    contents = client.tool(call, {"server": "deepwiki", "tool": "read_wiki_contents",
                                  "arguments": {"repoName": REPO}})
    parts = texts(contents)
    first, note = parts[0], parts[1] if len(parts) > 1 else ""
    whole, pages = first, 1
    if "ref=" in note:
        ref = note.split("ref=")[1].split()[0]
        offset = int(note.split("offset=")[1].split(".")[0])
        while True:
            page = texts(client.tool("read_mcp_result", {"ref": ref, "offset": offset}))
            whole += page[0]
            offset += len(page[0].encode())
            pages += 1
            if "that is the end" in page[1]:
                break
    size = int(note.split("the result is ")[1].split()[0]) if "the result is " in note else len(first)
    record["read_wiki_contents"] = {
        "is_error": bool(contents.get("isError")),
        "seconds_first_part": round(time.monotonic() - started, 2),
        "first_part_bytes": len(first.encode()), "note": note[:200],
        "declared_bytes": size, "reassembled_bytes": len(whole.encode()), "pages": pages,
        "structured_content_dropped": not any(c.get("type") == "resource" for c in contents["content"]),
    }
    if contents.get("isError"):
        record["read_wiki_contents"]["said"] = first[:400]
    client.close()
    shutil.rmtree(home)
    record["ccnm_agent_exit"] = client.proc.returncode
    out.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(record, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
