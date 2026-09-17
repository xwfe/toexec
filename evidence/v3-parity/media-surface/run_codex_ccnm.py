#!/usr/bin/env python3
"""真实 Codex 连真实的 ccnm（`ccnm internal mcp-serve`，外部 MCP 的 read 模式），
假模型调一次 view_image，看图片以什么形状进了下一轮模型请求。零额度，隔离做法
同 run_codex.py。ccnm 的配置、状态目录、工作区都在输出目录里临时建。

用法：CCNM_BIN=<ccnm 二进制> python3 run_codex_ccnm.py <新输出目录>
"""

import base64
import json
import os
import struct
import subprocess
import sys
import threading
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from run_codex import CODEX, SANDBOX_PROFILE, shorten, sse  # noqa: E402


def png_2x2():
    def chunk(kind, data):
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)
    raw = b"".join(b"\x00" + b"\xff\x00\x00" * 2 for _ in range(2))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 2, 2, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


class MockModel:
    def __init__(self, out):
        self.out, self.step, self.called = out, 0, None
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
                namespace = next((t.get("name") for t in request.get("tools", [])
                                  if t.get("type") == "namespace"
                                  and any(x.get("name") == "view_image" for x in t.get("tools", []))),
                                 None)
                if n == 0 and namespace:
                    model.called = f"{namespace} / view_image"
                    item = {"type": "function_call", "id": "fc_0", "call_id": "call_0",
                            "namespace": namespace, "name": "view_image",
                            "arguments": json.dumps({"path": "shots/red.png"})}
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
    ccnm = Path(os.environ["CCNM_BIN"]).resolve()
    out = Path(sys.argv[1]).resolve()
    out.mkdir(parents=True)
    # ccnm 一侧：工作区、配置、HOME、状态目录。都在解析过的真实路径下——ccnm 的
    # 凭据检查遇到 symlink 祖先会拒绝启动。
    root, ccnm_home, state = out / "project", out / "ccnm-home", out / "ccnm-state"
    for d in (root / "shots", ccnm_home, state):
        d.mkdir(parents=True)
    png = png_2x2()
    (root / "shots" / "red.png").write_bytes(png)
    config = out / "ccnm.toml"
    config.write_text(f'''this = "runtime"

[nodes.runtime]

[nodes.agent]
ssh = "agent-node.invalid"

[workspaces.demo]
root = "{root}"
agent = {{ node = "agent", instance = "claude-main" }}
external_mcp = "read"
''')
    payload = base64.urlsafe_b64encode(json.dumps(
        {"protocol": 5, "workspace": "demo", "session": "codex-view-image", "mode": "read"}
    ).encode()).decode().rstrip("=")

    home, work = out / "home", out / "work"
    (home / ".codex").mkdir(parents=True)
    work.mkdir()
    profile = out / "no-egress.sb"
    profile.write_text(SANDBOX_PROFILE)
    model = MockModel(out)
    provider = "probemock"
    env_toml = (f'{{HOME="{ccnm_home}",XDG_STATE_HOME="{state}",CCNM_CONFIG="{config}",'
                f'PATH="{os.environ["PATH"]}"}}')
    cmd = ["sandbox-exec", "-f", str(profile), CODEX, "exec", "--ignore-user-config",
           "--skip-git-repo-check", "--json", "-s", "read-only",
           "-c", f'model_provider="{provider}"',
           "-c", f'model_providers.{provider}.name="{provider}"',
           "-c", f'model_providers.{provider}.base_url="http://127.0.0.1:{model.port}/v1"',
           "-c", f'model_providers.{provider}.wire_api="responses"',
           "-c", f"model_providers.{provider}.requires_openai_auth=false",
           "-c", f'mcp_servers.ccnm.command="{ccnm}"',
           "-c", f"mcp_servers.ccnm.args={json.dumps(['internal', 'mcp-serve', '--payload', payload])}",
           "-c", f"mcp_servers.ccnm.env={env_toml}",
           "-c", "mcp_servers.ccnm.default_tools_approval_mode=\"approve\"",
           "-m", "gpt-5.1-codex", "look at the image"]
    env = {"HOME": str(home), "CODEX_HOME": str(home / ".codex"), "PATH": os.environ["PATH"]}
    proc = subprocess.run(cmd, cwd=work, env=env, stdin=subprocess.DEVNULL,
                          capture_output=True, timeout=120)
    (out / "codex.stdout").write_bytes(proc.stdout)
    (out / "codex.stderr").write_bytes(proc.stderr)
    requests = sorted(out.glob("model-request-*.json"), key=lambda p: int(p.stem.rsplit("-", 1)[1]))
    output, image_bytes_match = None, None
    for p in requests:
        for item in json.loads(p.read_text()).get("input", []):
            if isinstance(item, dict) and item.get("type") == "function_call_output" \
                    and item.get("call_id") == "call_0":
                raw_output = item.get("output")
                output = shorten(raw_output)
                for block in raw_output if isinstance(raw_output, list) else []:
                    url = block.get("image_url", "")
                    if block.get("type") == "input_image" and url.startswith("data:image/png;base64,"):
                        image_bytes_match = base64.b64decode(url.split(",", 1)[1]) == png
    ccnm_version = subprocess.run([str(ccnm), "--version"], capture_output=True, text=True).stdout.strip()
    codex_version = subprocess.run([CODEX, "--version"], capture_output=True, text=True).stdout.strip()
    summary = {
        "host": codex_version, "server": ccnm_version, "rc": proc.returncode,
        "model_requests": len(requests), "tool_called": model.called,
        "function_call_output": output,
        "input_image_is_the_file_byte_for_byte": image_bytes_match,
    }
    (out / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
