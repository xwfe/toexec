#!/usr/bin/env python3
"""V2-G06 第二步：拿真实 Codex 发出的 sandbox 参数，在 stdio exec-server 上重放各种变体。

问的是"权限上限到底由谁决定"：服务端自己管，还是完全信客户端填的 sandbox。
"工作区外"放在 ~/.cache/g06-replay-<pid>（workspace-write 默认可写 /tmp 和 TMPDIR，
放那里测不出来），测完删除。http/request 只打本机 HTTP 服务。环境配置里的"凭据"是合成串。
不起 Agent、不发模型请求。

用法：replay.py <capture.py 录下的 process-start.json> <新输出目录>
"""

import base64
import copy
import json
import os
import shutil
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "g01"))
from probe import Server, short  # noqa: E402

SYNTHETIC = "G06-SYNTHETIC-NOT-A-REAL-TOKEN"


def run_process(srv, params, pid):
    params = dict(params, processId=pid)
    started = srv.request("process/start", params)
    if not isinstance(started, dict) or "result" not in started:
        return {"start": short(started)}
    output, exit_code = b"", None
    after = None
    deadline = time.time() + 15
    while time.time() < deadline:
        read = srv.request("process/read", {"processId": pid, "afterSeq": after, "maxBytes": 65536, "waitMs": 1000})
        res = read.get("result", {}) if isinstance(read, dict) else {}
        for chunk in res.get("chunks", []):
            output += base64.b64decode(chunk.get("chunk", ""))
        after = res.get("nextSeq", after)
        if res.get("exited") or res.get("closed"):
            exit_code = res.get("exitCode")
            break
    return {"exit_code": exit_code, "output": output.decode(errors="replace")[-300:]}


def main():
    captured = json.load(open(sys.argv[1]))["params"]
    out = Path(sys.argv[2]).resolve()
    out.mkdir(parents=True, exist_ok=False)
    work = out / "work"
    work.mkdir()
    outside = Path.home() / ".cache" / f"g06-replay-{os.getpid()}"
    outside.mkdir(parents=True)
    r = {}
    try:
        sandbox = copy.deepcopy(captured["sandbox"])
        sandbox["cwd"] = work.as_uri()
        sandbox["workspaceRoots"] = [work.as_uri()]
        base_params = {k: v for k, v in captured.items() if k not in ("processId", "metadata")}
        base_params.update(cwd=work.as_uri(), env={"PATH": "/usr/bin:/bin"}, envPolicy=None)

        # 服务端的 CODEX_HOME 里放一段带合成凭据的 MCP 配置，看 environmentConfig/read 会不会带出去
        srv = Server(out, "replay", extra_env=None)
        codex_home = Path(srv.proc.args and next(Path(p) for p in out.glob("g01-replay-*"))) / ".codex"
        (codex_home / "config.toml").write_text(
            f'[mcp_servers.demo]\ncommand = "demo-server"\nenv = {{ DEMO_TOKEN = "{SYNTHETIC}" }}\n')
        srv.handshake()
        srv.drain(0.3)

        def write_cmd(name):
            return ["/bin/sh", "-c", f"echo x > {outside}/{name}; echo rc=$?"]

        cases = {
            "process_sandbox_as_codex_sends": dict(base_params, argv=write_cmd("a.txt"), sandbox=sandbox),
            "process_sandbox_null": dict(base_params, argv=write_cmd("b.txt"), sandbox=None),
            "process_sandbox_roots_widened_by_client": dict(
                base_params, argv=write_cmd("c.txt"),
                sandbox=dict(sandbox, workspaceRoots=[work.as_uri(), outside.as_uri()])),
        }
        for i, (name, params) in enumerate(cases.items()):
            result = run_process(srv, params, f"p{i}")
            result["file_written"] = (outside / f"{'abc'[i]}.txt").exists()
            r[name] = result

        payload = base64.b64encode(b"hello\n").decode()
        for name, sb in (("fs_write_sandbox_as_codex_sends", sandbox), ("fs_write_sandbox_null", None)):
            target = outside / f"{name}.txt"
            resp = srv.request("fs/writeFile", {"path": target.as_uri(), "dataBase64": payload, "sandbox": sb})
            r[name] = {"response": short(resp), "file_written": target.exists()}
        secret_file = outside / "secret.txt"
        secret_file.write_text("outside-secret\n")
        for name, sb in (("fs_read_outside_sandbox_as_codex_sends", sandbox), ("fs_read_outside_sandbox_null", None)):
            resp = srv.request("fs/readFile", {"path": secret_file.as_uri(), "sandbox": sb})
            data = base64.b64decode(resp["result"]["dataBase64"]).decode() if isinstance(resp, dict) and "result" in resp else None
            r[name] = {"response": short(resp), "content_returned": data == "outside-secret\n"}

        # http/request：只打本机
        hits = []

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                hits.append(self.path)
                body = b"g06-local-ok"
                self.send_response(200)
                self.send_header("content-length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        http = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=http.serve_forever, daemon=True).start()
        resp = srv.request("http/request", {"method": "GET", "url": f"http://127.0.0.1:{http.server_address[1]}/probe",
                                            "headers": [], "timeoutMs": 5000,
                                            "requestId": "g06-http-1"})
        body = None
        if isinstance(resp, dict) and "result" in resp:
            body = json.dumps(resp["result"])[:300]
        r["http_request_to_local_server"] = {"response": short(resp), "server_saw_request": hits == ["/probe"],
                                             "result_excerpt": body}
        http.shutdown()

        resp = srv.request("environmentConfig/read", {"cwd": work.as_uri(), "configPaths": [["mcp_servers"]],
                                                      "requirementsPaths": [["mcp_servers"]]})
        text = json.dumps(resp)
        r["environment_config_read"] = {
            "result_keys": sorted(resp.get("result", {}).keys()) if isinstance(resp, dict) else resp,
            "synthetic_token_returned": SYNTHETIC in text,
            "hostname_returned": bool(isinstance(resp, dict) and resp.get("result", {}).get("hostname")),
        }
        srv.close()
    finally:
        shutil.rmtree(outside, ignore_errors=True)
    r["outside_dir_removed"] = not outside.exists()
    (out / "results.json").write_text(json.dumps(r, indent=2, ensure_ascii=False))
    print(json.dumps(r, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
