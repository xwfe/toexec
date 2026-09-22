#!/usr/bin/env python3
"""Claude Code 在 print 模式结束时怎么收掉它起的 stdio MCP server（零额度）。

起因：真实受管会话结束后，ccnm_agent 给 curl 建的私有目录还留在 $TMPDIR 里。它的清理挂在
"客户端关 stdin"上，这里量客户端实际做了什么。

做法：假模型一句话结束（和 ../../v3-parity/machine-skills/run_probe.py 同一套），唯一的 MCP
server 是本文件自己（`--serve <日志>`）：对 SIGINT、SIGTERM、SIGHUP 只记不退，stdin 关了也再撑
5 秒，其间每 50 ms 记一次心跳。日志的最后一行之后就是它被强杀的时刻（它接得住的信号都接了）。

用法：[PROBE_CLAUDE=<claude>] python3 claude_exit_signals.py <新输出目录>
"""

import json
import os
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path


def serve(log_path):
    """读 stdin 在后台线程里；主线程接信号，第一件事（信号或 stdin 关闭）之后打心跳。"""
    import threading

    log = open(log_path, "a", buffering=1)
    t0 = time.monotonic()
    first = threading.Event()

    def note(event):
        log.write(f"{time.monotonic() - t0:7.3f} {event}\n")

    def on_signal(number, _frame):
        note(f"signal {signal.Signals(number).name}")
        first.set()

    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, on_signal)

    def reader():
        for raw in sys.stdin:
            message = json.loads(raw)
            if "id" not in message:
                continue
            method = message.get("method")
            if method == "initialize":
                result = {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}},
                          "serverInfo": {"name": "exit-probe", "version": "1"}}
            elif method == "tools/list":
                result = {"tools": [{"name": "noop", "description": "Does nothing.",
                                     "inputSchema": {"type": "object"}}]}
            else:
                result = {"content": [{"type": "text", "text": "ok"}]}
            sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": message["id"],
                                         "result": result}) + "\n")
            sys.stdout.flush()
        note("stdin closed")
        first.set()

    note("started")
    threading.Thread(target=reader, daemon=True).start()
    while not first.wait(0.05):
        pass
    end = time.monotonic() + 5
    while time.monotonic() < end:
        time.sleep(0.05)
        note("alive")
    note("exiting on its own after 5 s")


def main():
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "v3-parity" / "machine-skills"))
    from run_probe import CLAUDE, SANDBOX_PROFILE, Model

    out = Path(sys.argv[1]).resolve()
    out.mkdir(parents=True)
    home, state = out / "home", out / "state"
    home.mkdir()
    state.mkdir()
    log = out / "server.log"
    (state / "mcp.json").write_text(json.dumps({"mcpServers": {"probe": {
        "type": "stdio", "command": sys.executable, "args": [__file__, "--serve", str(log)]}}}))
    (state / "settings.json").write_text(json.dumps({"permissions": {"allow": ["mcp__probe__noop"]}}))
    (out / "no-egress.sb").write_text(SANDBOX_PROFILE)
    model = Model(out, lambda n, req: ([{"type": "text", "text": "done"}], "end_turn"))
    cmd = ["sandbox-exec", "-f", str(out / "no-egress.sb"), CLAUDE, "--tools", "",
           "--mcp-config", str(state / "mcp.json"), "--strict-mcp-config",
           "--settings", str(state / "settings.json"), "--permission-mode", "acceptEdits",
           "--session-id", str(uuid.uuid4()), "--print", "--output-format", "json",
           "--permission-prompts", "none", "--no-session-persistence"]
    env = {"HOME": str(home), "PATH": os.environ["PATH"],
           "ANTHROPIC_BASE_URL": f"http://127.0.0.1:{model.port}",
           "ANTHROPIC_API_KEY": "sk-ant-probe-not-a-real-key", "DISABLE_TELEMETRY": "1",
           "DISABLE_ERROR_REPORTING": "1", "DISABLE_AUTOUPDATER": "1",
           "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1"}
    started = time.monotonic()
    proc = subprocess.run(cmd, cwd=state, env=env, input=b"hi", capture_output=True, timeout=120)
    took = round(time.monotonic() - started, 2)
    time.sleep(6)
    lines = log.read_text().splitlines()
    events = [line for line in lines if not line.endswith(" alive")]
    alive = [line for line in lines if line.endswith(" alive")]
    record = {
        "claude": subprocess.run([CLAUDE, "--version"], capture_output=True, text=True).stdout.strip(),
        "rc": proc.returncode, "claude_seconds": took, "events": events,
        "heartbeats": len(alive), "last_heartbeat": alive[-1].split()[0] if alive else None,
        "exited_on_its_own": any("exiting on its own" in line for line in lines),
    }
    (out / "summary.json").write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(record, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--serve":
        serve(sys.argv[2])
    else:
        main()
