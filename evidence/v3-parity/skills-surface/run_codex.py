#!/usr/bin/env python3
"""让 Codex CLI 连探针 MCP server，看它把什么交给模型。零额度：模型接口是本机
假服务，codex 进程树用 sandbox-exec 禁掉非本机出站，HOME / CODEX_HOME 是临时
目录、里面没有凭据。只用标准库。做法沿用 evidence/v2-c/g05/harness.py。

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
NEEDLES = ["MARK_Z_300", "MARK_A_1000", "MARK_B_2000", "MARK_C_2100", "MARK_D_3000", "MARK_E_5900",
           "MARK_INSTRUCTIONS", "MARK_PROMPT_DESC", "MARK_SKILL_DESC", "MARK_SKILL_BODY"]


def sse(events):
    return "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events).encode()


class MockModel:
    """每个 /responses 请求原样存盘，然后回一句话结束这一轮。"""

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


def main():
    out = Path(sys.argv[1]).resolve()
    out.mkdir(parents=True)
    home, work = out / "home", out / "work"
    (home / ".codex").mkdir(parents=True)
    work.mkdir()
    probe_log = out / "probe-methods.jsonl"
    profile = out / "no-egress.sb"
    profile.write_text(SANDBOX_PROFILE)
    model = MockModel(out)

    version = subprocess.run([CODEX, "--version"], capture_output=True, text=True).stdout.strip()
    provider = "probemock"
    args = json.dumps([str(HERE / "probe_server.py")])
    cmd = ["sandbox-exec", "-f", str(profile), CODEX, "exec", "--ignore-user-config",
           "--skip-git-repo-check", "--json", "-s", "read-only",
           "-c", f'model_provider="{provider}"',
           "-c", f'model_providers.{provider}.name="{provider}"',
           "-c", f'model_providers.{provider}.base_url="http://127.0.0.1:{model.port}/v1"',
           "-c", f'model_providers.{provider}.wire_api="responses"',
           "-c", f"model_providers.{provider}.requires_openai_auth=false",
           "-c", f'mcp_servers.probe.command="{sys.executable}"',
           "-c", f"mcp_servers.probe.args={args}",
           "-c", f'mcp_servers.probe.env={{PROBE_LOG="{probe_log}"}}',
           "-m", "gpt-5.1-codex", "say done"]
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

    requests = sorted(out.glob("model-request-*.json"))
    seen = {}
    for needle in NEEDLES:
        seen[needle] = [p.name for p in requests if needle in p.read_text(errors="replace")]
    description_lengths = []
    for p in requests:
        def walk(node):
            if isinstance(node, dict):
                d = node.get("description")
                if isinstance(d, str) and d.startswith("Probe tool."):
                    description_lengths.append(len(d))
                for v in node.values():
                    walk(v)
            elif isinstance(node, list):
                for v in node:
                    walk(v)
        walk(json.loads(p.read_text()))
    methods = []
    if probe_log.exists():
        methods = [json.loads(line)["method"] for line in probe_log.read_text().splitlines()]
    client = None
    if probe_log.exists():
        first = json.loads(probe_log.read_text().splitlines()[0])
        client = {"clientInfo": first.get("client"), "capabilities": first.get("client_caps")}
    summary = {
        "host": version, "rc": rc, "seconds": round(time.time() - started, 2),
        "model_requests": len(requests),
        "mcp_methods_called": methods,
        "client_initialize": client,
        "markers_in_model_requests": {k: bool(v) for k, v in seen.items()},
        "probe_tool_description_lengths_sent": sorted(set(description_lengths)),
    }
    (out / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
