#!/usr/bin/env python3
"""让 Codex CLI 真的调用一次返回图片 / 文件的 MCP 工具，看工具结果以什么形状进了
下一轮模型请求。零额度：模型接口是本机假服务，第一轮回一个 function_call 去调
media_probe，第二轮原样存盘后结束；进程树用 sandbox-exec 禁掉非本机出站，HOME /
CODEX_HOME 是临时空目录。做法沿用 ../skills-surface/run_codex.py。

用法：python3 run_codex.py <新输出目录>
"""

import json
import os
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
CODEX = os.environ.get("PROBE_CODEX", "codex")
SANDBOX_PROFILE = """(version 1)
(allow default)
(deny network-outbound)
(allow network-outbound (remote ip "localhost:*"))
(allow network-outbound (remote unix-socket))
"""
KINDS = ["image", "resource_png", "resource_pdf"]


def sse(events):
    return "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events).encode()


def tool_names(node, found):
    if isinstance(node, dict):
        if isinstance(node.get("name"), str):
            found.append(node["name"])
        for v in node.values():
            tool_names(v, found)
    elif isinstance(node, list):
        for v in node:
            tool_names(v, found)
    return found


class MockModel:
    def __init__(self, out, kind):
        self.out, self.kind, self.step, self.called = out, kind, 0, None
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
                request = json.loads(raw)
                # Codex 0.154.0 把 MCP 工具放进 {"type":"namespace","name":"mcp__<server>"}；
                # 调用时 name 写命名空间内的名字、namespace 写命名空间（形状见 README）。
                namespace = next((t.get("name") for t in request.get("tools", [])
                                  if t.get("type") == "namespace"
                                  and "media_probe" in tool_names(t.get("tools", []), [])), None)
                if n == 0 and namespace:
                    model.called = f"{namespace} / media_probe"
                    item = {"type": "function_call", "id": "fc_0", "call_id": "call_0",
                            "namespace": namespace, "name": "media_probe",
                            "arguments": json.dumps({"kind": model.kind})}
                else:
                    item = {"type": "message", "role": "assistant", "id": f"msg_{n}",
                            "content": [{"type": "output_text", "text": "done"}]}
                payload = sse([
                    {"type": "response.created", "response": {"id": rid}},
                    {"type": "response.output_item.done", "item": item},
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


def tool_output(requests):
    """最后一个请求里 call_0 的 function_call_output，原样返回（base64 截短）。"""
    for p in reversed(requests):
        for item in json.loads(p.read_text()).get("input", []):
            if isinstance(item, dict) and item.get("type") == "function_call_output" \
                    and item.get("call_id") == "call_0":
                return shorten(item.get("output"))
    return None


def shorten(node):
    if isinstance(node, dict):
        return {k: shorten(v) for k, v in node.items()}
    if isinstance(node, list):
        return [shorten(v) for v in node]
    if isinstance(node, str) and len(node) > 120:
        return node[:60] + f"...<{len(node)} chars>"
    return node


def run(out, kind):
    out.mkdir(parents=True)
    home, work = out / "home", out / "work"
    (home / ".codex").mkdir(parents=True)
    work.mkdir()
    probe_log = out / "probe-methods.jsonl"
    profile = out / "no-egress.sb"
    profile.write_text(SANDBOX_PROFILE)
    model = MockModel(out, kind)
    provider = "probemock"
    args = json.dumps([str(HERE / "media_server.py")])
    cmd = ["sandbox-exec", "-f", str(profile), CODEX, "exec", "--ignore-user-config",
           "--skip-git-repo-check", "--json", "-s", "read-only",
           "-c", f'model_provider="{provider}"',
           "-c", f'model_providers.{provider}.name="{provider}"',
           "-c", f'model_providers.{provider}.base_url="http://127.0.0.1:{model.port}/v1"',
           "-c", f'model_providers.{provider}.wire_api="responses"',
           "-c", f"model_providers.{provider}.requires_openai_auth=false",
           "-c", f'mcp_servers.media.command="{sys.executable}"',
           "-c", f"mcp_servers.media.args={args}",
           "-c", f'mcp_servers.media.env={{PROBE_LOG="{probe_log}"}}',
           "-c", "mcp_servers.media.default_tools_approval_mode=\"approve\"",
           "-m", "gpt-5.1-codex", "call the probe"]
    env = {"HOME": str(home), "CODEX_HOME": str(home / ".codex"), "PATH": os.environ["PATH"]}
    started = time.time()
    try:
        proc = subprocess.run(cmd, cwd=work, env=env, stdin=subprocess.DEVNULL,
                              capture_output=True, timeout=120)
        rc, stdout, stderr = proc.returncode, proc.stdout, proc.stderr
    except subprocess.TimeoutExpired as exc:
        rc, stdout, stderr = "timeout", exc.stdout or b"", exc.stderr or b""
    (out / "codex.stdout").write_bytes(stdout)
    (out / "codex.stderr").write_bytes(stderr)
    requests = sorted(out.glob("model-request-*.json"), key=lambda p: int(p.stem.rsplit("-", 1)[1]))
    calls = []
    if probe_log.exists():
        calls = [json.loads(line) for line in probe_log.read_text().splitlines()]
    return {
        "kind": kind, "rc": rc, "seconds": round(time.time() - started, 2),
        "model_requests": len(requests),
        "tool_name_offered": model.called,
        "mcp_calls": [c for c in calls if c["method"] == "tools/call"],
        "function_call_output": tool_output(requests),
    }


def main():
    out = Path(sys.argv[1]).resolve()
    version = subprocess.run([CODEX, "--version"], capture_output=True, text=True).stdout.strip()
    summary = {"host": version, "runs": [run(out / kind, kind) for kind in KINDS]}
    out.mkdir(parents=True, exist_ok=True)
    (out / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
