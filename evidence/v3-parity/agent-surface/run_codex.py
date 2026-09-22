#!/usr/bin/env python3
"""受管 Codex 会话打开 web_search 之后，模型请求里多了什么（v3 第 5 节第 6 步）。

零额度：模型接口是本机假服务，只回一句话并把请求存下来；HOME / CODEX_HOME 是临时空目录；
进程树用 sandbox-exec 禁掉非本机出站。启动参数照 ccnm 的 `build_launch_cmd`
（crates/ccnm-core/src/provider/codex/mod.rs，print 模式）拼：只读 sandbox、
agents.enabled=false、关掉的 feature 列表、ccnm 这个 MCP server 只开指定工具；
只换 web_search 的取值，Code Mode 开关各测一遍——Code Mode 的
excluded_tool_namespaces 会不会把搜索也排除掉，是这次要回答的问题之一。

用法：python3 run_codex.py <新输出目录，放仓库外>
环境变量 PROBE_CODEX 指定 codex 二进制（ccnm 钉的是 0.154.0）；PROBE_MODEL 指定模型
（ccnm 不写 model 时不传 --model，这里默认也不传）；PROBE_BUILTIN=1 改走内置的 openai
provider（openai_base_url 指到假接口、假 key 登录），用来排除"自定义 provider 才这样"。
"""

import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
CODEX = os.environ.get("PROBE_CODEX", "codex")
MODEL = os.environ.get("PROBE_MODEL")
BUILTIN = os.environ.get("PROBE_BUILTIN") == "1"
SANDBOX_PROFILE = """(version 1)
(allow default)
(deny network-outbound)
(allow network-outbound (remote ip "localhost:*"))
(allow network-outbound (remote unix-socket))
"""
# 抄自 ccnm provider/codex/mod.rs 的 DISABLED（ccnm b59f864）。
DISABLED = [
    "shell_tool", "unified_exec", "unified_exec_tty", "view_image", "apps", "plugins", "hooks",
    "multi_agent", "multi_agent_v2", "browser_use", "computer_use", "image_generation",
    "memories", "workspace_dependencies", "skill_search", "shell_snapshot", "goals", "tool_suggest",
]
WEB_SEARCH = ["disabled", "cached", "live"]


def sse(events):
    return "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events).encode()


class MockModel:
    def __init__(self, out):
        self.out, self.step = out, 0
        model = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                body = json.dumps({"data": [], "models": []}).encode()
                self.send_response(200)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                raw = self.rfile.read(int(self.headers.get("content-length") or 0))
                n = model.step
                model.step += 1
                (model.out / f"model-request-{n}.json").write_bytes(raw)
                rid = f"resp_{n}"
                payload = sse([
                    {"type": "response.created", "response": {"id": rid}},
                    {"type": "response.output_item.done", "item": {
                        "type": "message", "role": "assistant", "id": f"msg_{n}",
                        "content": [{"type": "output_text", "text": "done"}]}},
                    {"type": "response.completed", "response": {"id": rid, "usage": {
                        "input_tokens": 0, "input_tokens_details": None, "output_tokens": 0,
                        "output_tokens_details": None, "total_tokens": 0}}},
                ])
                self.send_response(200)
                self.send_header("content-type", "text/event-stream")
                self.send_header("content-length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()


def run(out, web_search, code_mode):
    out.mkdir(parents=True)
    model = MockModel(out)
    home, work = out / "home", out / "work"
    (home / ".codex").mkdir(parents=True)
    work.mkdir()
    profile = out / "no-egress.sb"
    profile.write_text(SANDBOX_PROFILE)
    env = {"HOME": str(home), "CODEX_HOME": str(home / ".codex"), "PATH": os.environ["PATH"]}
    if BUILTIN:
        env["OPENAI_BASE_URL"] = f"http://127.0.0.1:{model.port}/v1"
        subprocess.run([CODEX, "login", "--with-api-key"], env=env, input=b"sk-probe-not-a-real-key",
                       capture_output=True, timeout=30, check=True)
        # 0.154.0 不认 OPENAI_BASE_URL 环境变量，认配置键 openai_base_url；内置 provider
        # 默认先走 WebSocket，假接口只会 HTTP，所以把两个 websocket feature 关掉。
        provider_args = ["-c", f'openai_base_url="http://127.0.0.1:{model.port}/v1"',
                         "--disable", "responses_websockets", "--disable", "responses_websockets_v2"]
    else:
        provider = "probemock"
        provider_args = [
            "-c", f'model_provider="{provider}"',
            "-c", f'model_providers.{provider}.name="{provider}"',
            "-c", f'model_providers.{provider}.base_url="http://127.0.0.1:{model.port}/v1"',
            "-c", f'model_providers.{provider}.wire_api="responses"',
            "-c", f"model_providers.{provider}.requires_openai_auth=false"]
    cmd = ["sandbox-exec", "-f", str(profile), CODEX, "exec", "--ignore-user-config",
           "--ignore-rules", "--skip-git-repo-check", "--ephemeral", "--json", "--color", "never",
           *provider_args,
           *(["--model", MODEL] if MODEL else []),
           "--sandbox", "read-only", "-c", 'approval_policy="never"',
           "-c", f'web_search="{web_search}"', "-c", "agents.enabled=false"]
    if code_mode:
        cmd += ["--enable", "code_mode_only", "-c",
                'features.code_mode.excluded_tool_namespaces=["functions","collaboration"]']
    for feature in DISABLED:
        cmd += ["--disable", feature]
    cmd += ["-c", f"mcp_servers.ccnm.command={json.dumps(sys.executable)}",
            "-c", f"mcp_servers.ccnm.args={json.dumps([str(HERE / 'tiny_server.py')])}",
            "-c", "mcp_servers.ccnm.required=true",
            "-c", 'mcp_servers.ccnm.default_tools_approval_mode="approve"',
            "-c", 'mcp_servers.ccnm.enabled_tools=["read_file","apply_patch"]',
            "-"]
    try:
        proc = subprocess.run(cmd, cwd=work, env=env, input=b"go", capture_output=True, timeout=90)
        rc, stdout, stderr = proc.returncode, proc.stdout, proc.stderr
    except subprocess.TimeoutExpired as exc:
        rc, stdout, stderr = "timeout", exc.stdout or b"", exc.stderr or b""
    (out / "codex.stdout").write_bytes(stdout)
    (out / "codex.stderr").write_bytes(stderr)
    first = out / "model-request-0.json"
    request = json.loads(first.read_text()) if first.exists() else {}
    tools = offered(request)
    web = [t for t in tools if "web" in json.dumps(t).lower() and t.get("type") != "namespace"]
    return {"web_search": web_search, "code_mode": code_mode, "model": request.get("model"),
            "rc": rc,
            "tools": [label(t) for t in tools], "web_tools": web,
            "stderr_tail": stderr.decode(errors="replace")[-400:]}


def offered(request):
    """模型拿到的工具。CLI 默认模型下不在顶层 tools，而在 input 里 type 为
    additional_tools 的那一项（../media-surface 的记录里写过）；两处都收。"""
    tools = list(request.get("tools") or [])
    for item in request.get("input") or []:
        if isinstance(item, dict) and item.get("type") == "additional_tools":
            tools += item.get("tools") or []
    return tools


def label(tool):
    """namespace 展开成 ns{a,b}；内置的服务端工具（没有 name）用 type。"""
    if tool.get("type") == "namespace":
        inner = ",".join(t.get("name") or t.get("type", "?") for t in tool.get("tools", []))
        return f"{tool.get('name')}{{{inner}}}"
    return tool.get("name") or tool.get("type", "?")


def main():
    out = Path(sys.argv[1]).resolve()
    version = subprocess.run([CODEX, "--version"], capture_output=True, text=True).stdout.strip()
    runs = [run(out / f"{value}-{'code' if code else 'direct'}", value, code)
            for code in (True, False) for value in WEB_SEARCH]
    summary = {"host": version, "runs": runs}
    (out / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
