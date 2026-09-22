#!/usr/bin/env python3
"""要 OAuth 登录的真实远端 server 经真实 ccnm 的 Agent 端服务（P50）连一次，模型会看到什么。

这台机器上没有登录过的 OAuth server，也不该有：ccnm 不读 Claude Code 存的令牌。所以这里只看
"没登录"那一面——对每个地址：先用 curl 不带任何凭据 POST 一次 initialize，记下 HTTP 状态和
WWW-Authenticate 头的开头（和 ccnm 无关的对照）；再把它写进临时 HOME 的 ~/.claude.json，经
`ccnm internal agent-skills` 的 call_mcp_tool 列一次、连一次，记下两段原话。不发任何令牌。

地址都是各家公开文档里给 MCP 客户端的那个，本来就是谁都能 POST 的入口。代理照当前环境的
HTTP(S)_PROXY 交给 ccnm 和 curl。

用法：CCNM_BIN=<ccnm> python3 real_oauth.py <输出文件>
"""

import base64
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from real_remote import CCNM, Client, texts  # noqa: E402

SERVERS = {
    "notion": "https://mcp.notion.com/mcp",
    "linear": "https://mcp.linear.app/mcp",
    "sentry": "https://mcp.sentry.dev/mcp",
    "github": "https://api.githubcopilot.com/mcp/",
    "atlassian": "https://mcp.atlassian.com/v1/mcp",
    "stripe": "https://mcp.stripe.com",
}
INIT = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
    "protocolVersion": "2025-06-18", "capabilities": {},
    "clientInfo": {"name": "oauth-probe", "version": "1"}}})


def bare(url, env):
    """不经 ccnm：状态码和 WWW-Authenticate 的开头。"""
    proc = subprocess.run(
        ["curl", "-sS", "-o", "/dev/null", "-D", "-", "--max-time", "20", "-X", "POST",
         "-H", "content-type: application/json", "-H", "accept: application/json, text/event-stream",
         "-H", "user-agent: oauth-probe/1", "--data-binary", INIT, url],
        env=env, capture_output=True, text=True)
    status, auth = None, None
    for line in proc.stdout.splitlines():
        if line.startswith("HTTP/"):
            status = int(line.split()[1])
        elif line.lower().startswith("www-authenticate:"):
            auth = line.split(":", 1)[1].strip()[:120]
    return {"status": status, "www_authenticate": auth, "curl_error": proc.stderr.strip()[:200] or None}


def main():
    out = Path(sys.argv[1])
    env = {"PATH": os.environ["PATH"]}
    env.update({k: v for k, v in os.environ.items() if k.lower() in ("http_proxy", "https_proxy",
                                                                   "no_proxy")})
    home = Path(tempfile.mkdtemp(prefix="ccnm-real-oauth-")).resolve()
    (home / ".claude.json").write_text(json.dumps({"mcpServers": {
        name: {"type": "http", "url": url} for name, url in SERVERS.items()}}))
    body = {"protocol": 1, "home": str(home), "session": "real-oauth", "mcp": {}}
    payload = base64.urlsafe_b64encode(json.dumps(body).encode()).decode().rstrip("=")
    client = Client([CCNM, "internal", "agent-skills", "--payload", payload], env)
    client.call("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                               "clientInfo": {"name": "real-oauth-probe", "version": "1"}})
    record = {"ccnm": subprocess.run([CCNM, "--version"], capture_output=True, text=True).stdout.strip(),
              "curl": subprocess.run(["curl", "--version"], capture_output=True,
                                     text=True).stdout.splitlines()[0],
              "proxy_vars_passed": sorted(k for k in env if k != "PATH")}
    record["listing"] = texts(client.tool("call_mcp_tool", {}))[0]
    record["servers"] = {}
    for name, url in SERVERS.items():
        result = client.tool("call_mcp_tool", {"server": name})
        record["servers"][name] = {"url": url, "bare": bare(url, env),
                                   "ccnm_is_error": bool(result.get("isError")),
                                   "ccnm_said": "\n".join(texts(result))[:600]}
    record["listing_after"] = texts(client.tool("call_mcp_tool", {}))[0]
    client.close()
    shutil.rmtree(home)
    record["ccnm_agent_exit"] = client.proc.returncode
    out.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(record, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
