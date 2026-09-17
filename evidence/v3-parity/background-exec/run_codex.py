#!/usr/bin/env python3
"""Codex CLI 自己的后台命令（unified exec）和 MCP 调用超时，实际是什么样。零额度：模型接口
是本机假服务，按请求序号回写好的 function_call；进程树用 sandbox-exec 禁掉非本机出站，
HOME / CODEX_HOME 是临时空目录。做法沿用 ../media-surface/run_codex.py。

场景：
  tools      只抓第一个模型请求里的 exec_command / write_stdin 定义
  lifecycle  exec_command 起一个 2 秒的命令（yield 500 ms）→ write_stdin 空轮询等它结束 →
             再起一个 sleep 60 → 结束这一轮；codex 退出后看那个 sleep 还在不在
  timeout    调一个睡 75 秒的 MCP 工具，看 Codex 等多久、给模型什么、有没有通知 server 取消

用法：python3 run_codex.py <新输出目录> [场景…]
"""

import json
import os
import re
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
SCENARIOS = ["tools", "lifecycle", "timeout"]


def sse(events):
    return "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events).encode()


def call(n, name, arguments, namespace=None):
    item = {"type": "function_call", "id": f"fc_{n}", "call_id": f"call_{n}", "name": name,
            "arguments": json.dumps(arguments)}
    if namespace:
        item["namespace"] = namespace
    return item


def outputs(request):
    """call_id → 这一轮请求里 function_call_output 的文本。"""
    found = {}
    for item in request.get("input", []):
        if isinstance(item, dict) and item.get("type") == "function_call_output":
            out = item.get("output")
            if isinstance(out, list):
                out = "\n".join(p.get("text", "") for p in out if isinstance(p, dict))
            found[item.get("call_id")] = out
    return found


def session_id(text):
    m = re.search(r"session ID (\d+)", text or "")
    return int(m.group(1)) if m else None


class MockModel:
    def __init__(self, out, script):
        self.out, self.script, self.step, self.times = out, script, 0, []
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
                model.times.append(time.time())
                (model.out / f"model-request-{n}.json").write_bytes(raw)
                item = model.script(n, json.loads(raw)) or {
                    "type": "message", "role": "assistant", "id": f"msg_{n}",
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


def codex(out, model, extra, timeout, sandbox="workspace-write"):
    home, work = out / "home", out / "work"
    (home / ".codex").mkdir(parents=True)
    work.mkdir()
    profile = out / "no-egress.sb"
    profile.write_text(SANDBOX_PROFILE)
    provider = "probemock"
    cmd = ["sandbox-exec", "-f", str(profile), CODEX, "exec", "--ignore-user-config",
           "--skip-git-repo-check", "--json", "-s", sandbox,
           "-c", f'model_provider="{provider}"',
           "-c", f'model_providers.{provider}.name="{provider}"',
           "-c", f'model_providers.{provider}.base_url="http://127.0.0.1:{model.port}/v1"',
           "-c", f'model_providers.{provider}.wire_api="responses"',
           "-c", f"model_providers.{provider}.requires_openai_auth=false",
           *extra, "-m", "gpt-5.1-codex", "go"]
    env = {"HOME": str(home), "CODEX_HOME": str(home / ".codex"), "PATH": os.environ["PATH"]}
    started = time.time()
    try:
        proc = subprocess.run(cmd, cwd=work, env=env, stdin=subprocess.DEVNULL,
                              capture_output=True, timeout=timeout)
        rc, stdout, stderr = proc.returncode, proc.stdout, proc.stderr
    except subprocess.TimeoutExpired as exc:
        rc, stdout, stderr = "timeout", exc.stdout or b"", exc.stderr or b""
    (out / "codex.stdout").write_bytes(stdout)
    (out / "codex.stderr").write_bytes(stderr)
    return rc, round(time.time() - started, 2), work


def requests(out):
    paths = sorted(out.glob("model-request-*.json"), key=lambda p: int(p.stem.rsplit("-", 1)[1]))
    return [json.loads(p.read_text()) for p in paths]


def alive(pid):
    return subprocess.run(["kill", "-0", str(pid)], capture_output=True).returncode == 0


def tools(out):
    out.mkdir(parents=True)
    model = MockModel(out, lambda n, req: None)
    rc, seconds, _ = codex(out, model, [], 60)
    offered = {}
    for tool in requests(out)[0].get("tools", []):
        if tool.get("name") in ("exec_command", "write_stdin"):
            offered[tool["name"]] = tool
    return {"scenario": "tools", "rc": rc, "seconds": seconds, "tools": offered}


def lifecycle(out):
    out.mkdir(parents=True)

    def script(n, req):
        got = outputs(req)
        if n == 0:
            return call(0, "exec_command", {
                "cmd": "echo started; sleep 2; echo finished", "yield_time_ms": 500})
        if n == 1:
            return call(1, "write_stdin", {
                "session_id": session_id(got.get("call_0")), "chars": "", "yield_time_ms": 10000})
        if n == 2:
            return call(2, "exec_command", {
                "cmd": "echo $$ > orphan.pid; exec sleep 60", "yield_time_ms": 500})
        return None

    model = MockModel(out, script)
    # Codex 自己的 seatbelt 套不进外层 sandbox-exec（sandbox_apply: Operation not
    # permitted），所以命令不再加一层；出站仍由外层挡住。
    rc, seconds, work = codex(out, model, [], 90, "danger-full-access")
    time.sleep(1)
    reqs = requests(out)
    last = outputs(reqs[-1]) if reqs else {}
    pid_file = work / "orphan.pid"
    pid = int(pid_file.read_text().strip()) if pid_file.exists() else None
    still = alive(pid) if pid else None
    if still:
        subprocess.run(["kill", str(pid)])
    return {"scenario": "lifecycle", "rc": rc, "seconds": seconds, "model_requests": len(reqs),
            "outputs": last, "sleep_pid": pid, "sleep_alive_after_codex_exit": still}


def timeout(out):
    out.mkdir(parents=True)
    probe_log = out / "probe-methods.jsonl"
    namespace = {}

    def script(n, req):
        if n == 0:
            ns = next((t.get("name") for t in req.get("tools", []) if t.get("type") == "namespace"
                       and any(x.get("name") == "slow" for x in t.get("tools", []))), None)
            namespace["name"] = ns
            return call(0, "slow", {"seconds": 75}, ns)
        return None

    model = MockModel(out, script)
    args = json.dumps([str(HERE / "slow_server.py")])
    extra = ["-c", f'mcp_servers.slow.command="{sys.executable}"',
             "-c", f"mcp_servers.slow.args={args}",
             "-c", f'mcp_servers.slow.env={{PROBE_LOG="{probe_log}"}}',
             "-c", 'mcp_servers.slow.default_tools_approval_mode="approve"']
    rc, seconds, _ = codex(out, model, extra, 150)
    reqs = requests(out)
    methods = []
    if probe_log.exists():
        methods = [json.loads(line) for line in probe_log.read_text().splitlines()]
    waited = round(model.times[1] - model.times[0], 1) if len(model.times) > 1 else None
    return {"scenario": "timeout", "rc": rc, "seconds": seconds, "namespace": namespace.get("name"),
            "seconds_between_call_and_next_request": waited,
            "outputs": outputs(reqs[-1]) if len(reqs) > 1 else {},
            "server_saw": [m for m in methods if m.get("method") != "tools/list"]}


def main():
    out = Path(sys.argv[1]).resolve()
    chosen = sys.argv[2:] or SCENARIOS
    version = subprocess.run([CODEX, "--version"], capture_output=True, text=True).stdout.strip()
    runs = [globals()[name](out / name) for name in chosen]
    summary = {"host": version, "runs": runs}
    out.mkdir(parents=True, exist_ok=True)
    (out / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
