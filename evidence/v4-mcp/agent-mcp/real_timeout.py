#!/usr/bin/env python3
"""慢网络上的大结果撞上调用上限时，模型看到什么；下一次调用能不能自己重连。

起因：在 hpsrv（Debian 13）上跑 real_remote.py，DeepWiki 那个 839 KB 的回复第一次超过了默认的
60 秒（那台机器下载只有约 16–19 KB/s）。这里用 Codex 配置的 `tool_timeout_sec = 10` 让它确定地
超时，记下原话，再调一次小的看是不是自己重连了。和 real_remote.py 一样不经模型、不用 key。

用法：CCNM_BIN=<ccnm> python3 real_timeout.py <输出文件>
"""

import base64
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from real_remote import CCNM, REPO, URL, Client, texts  # noqa: E402


def main():
    out = Path(sys.argv[1])
    home = Path(tempfile.mkdtemp(prefix="ccnm-real-timeout-")).resolve()
    (home / ".codex").mkdir()
    (home / ".codex" / "config.toml").write_text(
        f'[mcp_servers.deepwiki]\nurl = "{URL}"\ntool_timeout_sec = 10\n')
    body = {"protocol": 1, "home": str(home), "session": "real-timeout", "mcp": {}}
    payload = base64.urlsafe_b64encode(json.dumps(body).encode()).decode().rstrip("=")
    env = {"PATH": os.environ["PATH"]}
    env.update({k: v for k, v in os.environ.items() if k.lower() in ("http_proxy", "https_proxy",
                                                                   "no_proxy")})
    client = Client([CCNM, "internal", "agent-skills", "--payload", payload], env)
    client.call("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                               "clientInfo": {"name": "real-timeout-probe", "version": "1"}})
    record = {"ccnm": subprocess.run([CCNM, "--version"], capture_output=True, text=True).stdout.strip(),
              "tool_timeout_sec": 10}
    started = time.monotonic()
    big = client.tool("call_mcp_tool", {"server": "deepwiki", "tool": "read_wiki_contents",
                                        "arguments": {"repoName": REPO}})
    record["read_wiki_contents"] = {"seconds": round(time.monotonic() - started, 1),
                                    "is_error": bool(big.get("isError")),
                                    "said": "\n".join(texts(big))[:600]}
    started = time.monotonic()
    small = client.tool("call_mcp_tool", {"server": "deepwiki", "tool": "read_wiki_structure",
                                          "arguments": {"repoName": REPO}})
    record["next_call"] = {"seconds": round(time.monotonic() - started, 1),
                           "is_error": bool(small.get("isError")),
                           "text_bytes": sum(len(t.encode()) for t in texts(small))}
    client.close()
    shutil.rmtree(home)
    record["ccnm_agent_exit"] = client.proc.returncode
    out.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(record, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
