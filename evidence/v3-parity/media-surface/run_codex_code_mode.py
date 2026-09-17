#!/usr/bin/env python3
"""Code Mode 下（ccnm 受管 Codex 会话默认开着它）MCP 工具返回的图片怎么到模型面前。

Code Mode 里模型不直接调工具，而是给 `exec` 写一段 JS；MCP 工具的结果是个对象，
要调 `image(result.content[i])` 才会变成模型能看的图片。假模型第一轮发一段这样的
JS，第二轮把 Codex 发来的请求存盘。其余做法同 run_codex.py。

用法：python3 run_codex_code_mode.py <新输出目录>
"""

import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from run_codex import CODEX, SANDBOX_PROFILE, shorten, sse  # noqa: E402

SCRIPT = """const r = await tools.mcp__media__media_probe({kind: "image"});
text(r.content[0].text);
text(JSON.stringify(r.content.map((b) => b.type)));
image(r.content[1]);
"""


class MockModel:
    def __init__(self, out):
        self.out, self.step, self.sent = out, 0, False
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
                request = json.loads(raw)
                main_turn = any(isinstance(i, dict) and i.get("type") == "additional_tools"
                                for i in request.get("input", []))
                if main_turn and not model.sent:
                    model.sent = True
                    item = {"type": "custom_tool_call", "id": "ctc_0", "call_id": "call_0",
                            "name": "exec", "input": SCRIPT}
                else:
                    item = {"type": "message", "role": "assistant", "id": f"msg_{n}",
                            "content": [{"type": "output_text", "text": "done"}]}
                rid = f"resp_{n}"
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
    provider = "probemock"
    cmd = ["sandbox-exec", "-f", str(profile), CODEX, "exec", "--ignore-user-config",
           "--skip-git-repo-check", "--json", "-s", "read-only",
           "-c", f'model_provider="{provider}"',
           "-c", f'model_providers.{provider}.name="{provider}"',
           "-c", f'model_providers.{provider}.base_url="http://127.0.0.1:{model.port}/v1"',
           "-c", f'model_providers.{provider}.wire_api="responses"',
           "-c", f"model_providers.{provider}.requires_openai_auth=false",
           "-c", f'mcp_servers.media.command="{sys.executable}"',
           "-c", f"mcp_servers.media.args={json.dumps([str(HERE / 'media_server.py')])}",
           "-c", f'mcp_servers.media.env={{PROBE_LOG="{probe_log}"}}',
           "-c", "mcp_servers.media.default_tools_approval_mode=\"approve\"",
           # ccnm 受管 Codex 会话不指定模型时用的就是这两项（provider/codex/mod.rs）。
           "--enable", "code_mode_only",
           "-c", 'features.code_mode.excluded_tool_namespaces=["functions","collaboration"]',
           "call the probe"]
    env = {"HOME": str(home), "CODEX_HOME": str(home / ".codex"), "PATH": os.environ["PATH"]}
    proc = subprocess.run(cmd, cwd=work, env=env, stdin=subprocess.DEVNULL,
                          capture_output=True, timeout=120)
    (out / "codex.stdout").write_bytes(proc.stdout)
    (out / "codex.stderr").write_bytes(proc.stderr)
    requests = sorted(out.glob("model-request-*.json"), key=lambda p: int(p.stem.rsplit("-", 1)[1]))
    output = None
    for p in requests:
        for item in json.loads(p.read_text()).get("input", []):
            if isinstance(item, dict) and item.get("type") == "custom_tool_call_output" \
                    and item.get("call_id") == "call_0":
                output = shorten(item.get("output"))
    calls = []
    if probe_log.exists():
        calls = [json.loads(line) for line in probe_log.read_text().splitlines()]
    version = subprocess.run([CODEX, "--version"], capture_output=True, text=True).stdout.strip()
    summary = {
        "host": version, "rc": proc.returncode, "model_requests": len(requests),
        "model": json.loads(requests[0].read_text()).get("model") if requests else None,
        "script": SCRIPT, "mcp_calls": [c for c in calls if c["method"] == "tools/call"],
        "custom_tool_call_output": output,
    }
    (out / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
