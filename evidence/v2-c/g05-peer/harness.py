#!/usr/bin/env python3
"""V2-G05 方向 2 实测：网桥按连接对端的 OS 用户放行，URL 里没有任何秘密。

假模型接口、禁出站沙箱、临时 CODEX_HOME 和探针检查都复用上一轮 ../g05/harness.py，
这里只换网桥。仍然不发任何真实模型请求。

用法：harness.py <场景> <新输出目录>，场景见 SCENARIOS；bench 只测查询延迟，不起 Codex。
"""

import asyncio
import json
import os
import socket
import statistics
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "g05"))

import harness as base  # noqa: E402  上一轮的 ../g05/harness.py
from peer import peer_uid  # noqa: E402

# 本目录的 harness 会遮住同名模块，按路径显式加载上一轮那份。
if Path(base.__file__).resolve().parent == HERE:
    import importlib.util

    spec = importlib.util.spec_from_file_location("g05_base", HERE.parent / "g05" / "harness.py")
    base = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(base)


class PeerBridge:
    """连进来先查客户端 socket 的 uid，等于 expected_uid 才转发；查不到一律拒绝。
    allow_reconnect=False 时整个网桥只放行一条连接，断了就不再接受——对应 ccnm v1 不 resume。"""

    def __init__(self, expected_uid, upstream_port, log, allow_reconnect=False):
        self.expected_uid = expected_uid
        self.upstream_port = upstream_port
        self.allow_reconnect = allow_reconnect
        self.log = log
        self.used = False
        self.accepted = 0
        self.refused = []
        self.lookup_ms = []
        self.capture = None
        self.live = []
        self.loop = asyncio.new_event_loop()
        self.port = base.free_port()
        ready = threading.Event()

        def run():
            asyncio.set_event_loop(self.loop)
            self.loop.run_until_complete(asyncio.start_server(self.handle, "127.0.0.1", self.port))
            ready.set()
            self.loop.run_forever()

        threading.Thread(target=run, daemon=True).start()
        ready.wait()

    async def refuse(self, writer, reason, **fields):
        self.refused.append(reason)
        self.log(side="bridge", event="refused", reason=reason, **fields)
        writer.write(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
        try:
            await writer.drain()
        finally:
            writer.close()

    async def handle(self, reader, writer):
        host, client_port = writer.get_extra_info("peername")[:2]
        if host != "127.0.0.1":
            return await self.refuse(writer, "not-loopback")
        started = time.perf_counter()
        uid = await self.loop.run_in_executor(None, peer_uid, client_port, self.port)
        self.lookup_ms.append((time.perf_counter() - started) * 1000)
        if uid is None:
            return await self.refuse(writer, "lookup-failed")
        if uid != self.expected_uid:
            return await self.refuse(writer, "uid-mismatch", peer_uid=uid)
        if self.used and not self.allow_reconnect:
            return await self.refuse(writer, "second-connection", peer_uid=uid)
        self.used = True
        self.accepted += 1
        self.log(side="bridge", event="accepted", peer_uid=uid)
        up_r, up_w = await asyncio.open_connection("127.0.0.1", self.upstream_port)
        self.live.append((writer, up_w))

        async def pipe(src, dst, downstream):
            try:
                while chunk := await src.read(65536):
                    if downstream and self.capture:
                        with open(self.capture, "ab") as f:
                            f.write(chunk)
                    dst.write(chunk)
                    await dst.drain()
            except Exception:  # noqa: BLE001 - 任一侧断开都结束这条转发
                pass
            finally:
                dst.close()

        await asyncio.gather(pipe(reader, up_w, False), pipe(up_r, writer, True))
        self.log(side="bridge", event="closed")

    def drop_all(self):
        def close():
            for a, b in self.live:
                a.close()
                b.close()
            self.live.clear()
        self.loop.call_soon_threadsafe(close)
        self.log(side="bridge", event="dropped")
        time.sleep(0.5)


SCENARIOS = {
    "ok": {"expect": "self", "plan": [("call", f"echo {base.PROBE}; pwd"), ("say", "done")]},
    # 查询工具坏了（换成 /usr/bin/false）：一律拒绝，不能放行。
    "lookup-broken": {"expect": "self", "helper": "/usr/bin/false",
                      "plan": [("call", f"echo {base.PROBE}"), ("say", "done")]},
    # 网桥只认 root：Codex 以当前用户连进来，应被拒。
    "mismatch": {"expect": 0, "plan": [("call", f"echo {base.PROBE}"), ("say", "done")]},
    # 断线后不放行第二条连接（ccnm v1 不 resume）。
    "drop": {"expect": "self", "drop_before_step": 1, "plan": [
        ("call", f"echo {base.PROBE}"), ("call", "echo AFTER_DROP"), ("say", "done")]},
    # 断线后放行重连，看 Codex 会不会自己 resume 并把第二条命令跑成。
    "drop-resume": {"expect": "self", "allow_reconnect": True, "drop_before_step": 1, "plan": [
        ("call", f"echo {base.PROBE}"), ("call", "echo AFTER_DROP"), ("say", "done")]},
}


def run_codex(name, out):
    scenario = SCENARIOS[name]
    out.mkdir(parents=True, exist_ok=False)
    log = base.Log(out / "harness.jsonl")
    home, work, server_home = out / "home", out / "work", out / "server-home"
    for d in (home / ".codex", work, server_home / ".codex"):
        d.mkdir(parents=True)

    server_port = base.free_port()
    server_env = {"HOME": str(server_home), "CODEX_HOME": str(server_home / ".codex"),
                  "PATH": os.environ["PATH"]}
    server = subprocess.Popen([base.CODEX, "exec-server", "--listen", f"ws://127.0.0.1:{server_port}"],
                              env=server_env, stdin=subprocess.DEVNULL,
                              stdout=open(out / "exec-server.stdout", "wb"),
                              stderr=open(out / "exec-server.stderr", "wb"))
    for _ in range(100):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{server_port}/readyz", timeout=1):
                break
        except Exception:  # noqa: BLE001 - 还没起来
            time.sleep(0.1)
    else:
        raise SystemExit("exec-server not ready")

    expected = os.getuid() if scenario["expect"] == "self" else scenario["expect"]
    if "helper" in scenario:
        import peer  # noqa: PLC0415
        peer.HELPER = Path(scenario["helper"])
    bridge = PeerBridge(expected, server_port, log, scenario.get("allow_reconnect", False))
    bridge.capture = out / "bridge-down.bin"
    hooks = {scenario["drop_before_step"]: bridge.drop_all} if "drop_before_step" in scenario else {}
    model = base.MockModel(out, scenario["plan"], log, hooks)

    profile = out / "no-egress.sb"
    profile.write_text(base.SANDBOX_PROFILE)
    url = f"ws://127.0.0.1:{bridge.port}"
    env = {"HOME": str(home), "CODEX_HOME": str(home / ".codex"), "PATH": os.environ["PATH"],
           "CODEX_EXEC_SERVER_URL": url}
    p = "g05mock"
    cmd = ["sandbox-exec", "-f", str(profile), base.CODEX, "exec", "--ignore-user-config",
           "--skip-git-repo-check", "--json", "-s", "workspace-write",
           "-c", f'model_provider="{p}"', "-c", f'model_providers.{p}.name="{p}"',
           "-c", f'model_providers.{p}.base_url="http://127.0.0.1:{model.port}/v1"',
           "-c", f'model_providers.{p}.wire_api="responses"',
           "-c", f"model_providers.{p}.requires_openai_auth=false",
           "-m", "gpt-5.1-codex", "run the probe"]
    started = time.time()
    try:
        proc = subprocess.run(cmd, cwd=work, env=env, stdin=subprocess.DEVNULL,
                              capture_output=True, timeout=180)
        rc, stdout, stderr = proc.returncode, proc.stdout, proc.stderr
    except subprocess.TimeoutExpired as exc:
        rc, stdout, stderr = "timeout", exc.stdout or b"", exc.stderr or b""
    (out / "codex.stdout").write_bytes(stdout)
    (out / "codex.stderr").write_bytes(stderr)
    seconds = round(time.time() - started, 2)
    log(side="harness", event="codex-exited", rc=rc, seconds=seconds)
    refused_during_run = list(bridge.refused)

    post = {"after_run_same_user": base.raw_upgrade(bridge.port, "/")}
    server.terminate()
    try:
        server.wait(10)
    except subprocess.TimeoutExpired:
        server.kill()

    requests = sorted(out.glob("model-request-*.json"))
    outputs = []
    for r in requests:
        for item in json.loads(r.read_bytes()).get("input", []):
            if item.get("type") == "function_call_output":
                outputs.append(item["output"])
    summary = {
        "scenario": name, "expected_uid": expected, "my_uid": os.getuid(), "codex_rc": rc,
        "codex_seconds": seconds, "url_has_no_secret": url.count("/") == 2,
        "bridge_accepted": bridge.accepted,
        "refused_during_run": {r: refused_during_run.count(r) for r in sorted(set(refused_during_run))},
        "lookup_ms_p50": round(statistics.median(bridge.lookup_ms), 2) if bridge.lookup_ms else None,
        "lookup_ms_max": round(max(bridge.lookup_ms), 2) if bridge.lookup_ms else None,
        "model_requests": model.step, "shell_tool_offered": model.shell_offered,
        "probe_via_exec_server": base.probe_in_downstream(out / "bridge-down.bin"),
        "after_drop_executed": any("AFTER_DROP\n" in o for o in outputs),
        "last_tool_output": outputs[-1][:400] if outputs else None,
        "post_checks": post,
    }
    (out / "summary.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False))
    print(json.dumps(summary, indent=2, ensure_ascii=False))


def bench(out, n=200):
    """同一用户连 n 次，只测放行判定的耗时（含 macOS 上起查询进程）。"""
    out.mkdir(parents=True, exist_ok=False)
    log = base.Log(out / "harness.jsonl")
    sink = socket.socket()
    sink.bind(("127.0.0.1", 0))
    sink.listen(n)
    threading.Thread(target=lambda: [sink.accept() for _ in range(n)], daemon=True).start()
    bridge = PeerBridge(os.getuid(), sink.getsockname()[1], log, allow_reconnect=True)
    conns = []
    for _ in range(n):
        c = socket.create_connection(("127.0.0.1", bridge.port))
        conns.append(c)
        time.sleep(0.005)
    deadline = time.time() + 30
    while len(bridge.lookup_ms) < n and time.time() < deadline:
        time.sleep(0.05)
    ms = sorted(bridge.lookup_ms)
    summary = {"scenario": "bench", "platform": sys.platform, "connections": n,
               "accepted": bridge.accepted, "refused": len(bridge.refused),
               "lookup_ms_p50": round(statistics.median(ms), 2),
               "lookup_ms_p99": round(ms[int(len(ms) * 0.99) - 1], 2), "lookup_ms_max": round(ms[-1], 2)}
    (out / "summary.json").write_text(json.dumps(summary, indent=2))
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    scenario, target = sys.argv[1], Path(sys.argv[2]).resolve()
    bench(target) if scenario == "bench" else run_codex(scenario, target)
