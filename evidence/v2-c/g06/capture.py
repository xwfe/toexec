#!/usr/bin/env python3
"""V2-G06 第一步：录下真实 Codex 发给 exec-server 的 JSON-RPC 请求（含 sandbox 参数）。

复用 ../g05-peer 的按 uid 放行网桥和 ../g05 的假模型接口、禁出站沙箱；
网桥在转发的同时解 WebSocket 帧（客户端帧带掩码，要先解掉），把两个方向的
JSON 消息逐条写进 messages.jsonl。仍然不发任何真实模型请求。

用法：capture.py <sandbox 模式 read-only|workspace-write> <新输出目录>
"""

import asyncio
import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "g05-peer"))
import harness as peer_harness  # noqa: E402

base = peer_harness.base


class FrameTap:
    """从一个方向的字节流里切出 WebSocket 文本消息。握手头之前的字节跳过。"""

    def __init__(self, direction, sink):
        self.direction, self.sink = direction, sink
        self.buf = b""
        self.in_handshake = True
        self.fragments = b""

    def feed(self, data):
        self.buf += data
        if self.in_handshake:
            end = self.buf.find(b"\r\n\r\n")
            if end < 0:
                return
            self.buf = self.buf[end + 4:]
            self.in_handshake = False
        while True:
            if len(self.buf) < 2:
                return
            b0, b1 = self.buf[0], self.buf[1]
            fin, opcode, masked, length = b0 & 0x80, b0 & 0x0F, b1 & 0x80, b1 & 0x7F
            pos = 2
            if length == 126:
                if len(self.buf) < 4:
                    return
                length = int.from_bytes(self.buf[2:4], "big")
                pos = 4
            elif length == 127:
                if len(self.buf) < 10:
                    return
                length = int.from_bytes(self.buf[2:10], "big")
                pos = 10
            mask = b""
            if masked:
                if len(self.buf) < pos + 4:
                    return
                mask = self.buf[pos:pos + 4]
                pos += 4
            if len(self.buf) < pos + length:
                return
            payload = self.buf[pos:pos + length]
            self.buf = self.buf[pos + length:]
            if masked:
                payload = bytes(c ^ mask[i % 4] for i, c in enumerate(payload))
            if opcode in (0x1, 0x2, 0x0):
                self.fragments += payload
                if fin:
                    try:
                        msg = json.loads(self.fragments)
                    except ValueError:
                        msg = {"undecodable_bytes": len(self.fragments)}
                    self.sink(self.direction, msg)
                    self.fragments = b""


class TappedBridge(peer_harness.PeerBridge):
    def __init__(self, *args, record=None, **kwargs):
        self.record = record
        super().__init__(*args, **kwargs)

    async def handle(self, reader, writer):
        host, client_port = writer.get_extra_info("peername")[:2]
        uid = await self.loop.run_in_executor(None, peer_harness.peer_uid, client_port, self.port)
        if host != "127.0.0.1" or uid != self.expected_uid or self.used:
            return await self.refuse(writer, "refused", peer_uid=uid)
        self.used = True
        self.accepted += 1
        up_r, up_w = await asyncio.open_connection("127.0.0.1", self.upstream_port)
        taps = {"c2s": FrameTap("client->server", self.record), "s2c": FrameTap("server->client", self.record)}

        async def pipe(src, dst, tap):
            try:
                while chunk := await src.read(65536):
                    tap.feed(chunk)
                    dst.write(chunk)
                    await dst.drain()
            except Exception:  # noqa: BLE001
                pass
            finally:
                dst.close()

        await asyncio.gather(pipe(reader, up_w, taps["c2s"]), pipe(up_r, writer, taps["s2c"]))


def main():
    mode, out = sys.argv[1], Path(sys.argv[2]).resolve()
    out.mkdir(parents=True, exist_ok=False)
    log = base.Log(out / "harness.jsonl")
    home, work, server_home = out / "home", out / "work", out / "server-home"
    # "工作区外"不能放在 /tmp 或 TMPDIR 下：workspace-write 默认允许写那两处。放到 ~/.cache，测完删。
    outside = Path.home() / ".cache" / f"g06-probe-{os.getpid()}"
    for d in (home / ".codex", work, server_home / ".codex", outside):
        d.mkdir(parents=True)

    messages = open(out / "messages.jsonl", "w", encoding="utf-8")

    def record(direction, msg):
        messages.write(json.dumps({"dir": direction, "msg": msg}, ensure_ascii=False) + "\n")
        messages.flush()

    server_port = base.free_port()
    server_env = {"HOME": str(server_home), "CODEX_HOME": str(server_home / ".codex"),
                  "PATH": os.environ["PATH"]}
    server = subprocess.Popen([base.CODEX, "exec-server", "--listen", f"ws://127.0.0.1:{server_port}"],
                              env=server_env, stdin=subprocess.DEVNULL,
                              stdout=subprocess.DEVNULL, stderr=open(out / "exec-server.stderr", "wb"))
    for _ in range(100):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{server_port}/readyz", timeout=1)
            break
        except Exception:  # noqa: BLE001
            time.sleep(0.1)

    bridge = TappedBridge(os.getuid(), server_port, log, record=record)
    plan = [("call", f"echo inside > inside.txt; echo outside > {outside}/written-by-codex.txt; "
                     f"echo rc=$?"), ("say", "done")]
    model = base.MockModel(out, plan, log)
    profile = out / "no-egress.sb"
    profile.write_text(base.SANDBOX_PROFILE)
    p = "g06mock"
    cmd = ["sandbox-exec", "-f", str(profile), base.CODEX, "exec", "--ignore-user-config",
           "--skip-git-repo-check", "--json", "-s", mode,
           "-c", f'model_provider="{p}"', "-c", f'model_providers.{p}.name="{p}"',
           "-c", f'model_providers.{p}.base_url="http://127.0.0.1:{model.port}/v1"',
           "-c", f'model_providers.{p}.wire_api="responses"',
           "-c", f"model_providers.{p}.requires_openai_auth=false",
           "-m", "gpt-5.1-codex", "run the probe"]
    env = {"HOME": str(home), "CODEX_HOME": str(home / ".codex"), "PATH": os.environ["PATH"],
           "CODEX_EXEC_SERVER_URL": f"ws://127.0.0.1:{bridge.port}"}
    proc = subprocess.run(cmd, cwd=work, env=env, stdin=subprocess.DEVNULL, capture_output=True, timeout=180)
    (out / "codex.stdout").write_bytes(proc.stdout)
    (out / "codex.stderr").write_bytes(proc.stderr)
    server.terminate()
    server.wait(10)
    messages.close()

    starts = [json.loads(line)["msg"] for line in open(out / "messages.jsonl")
              if json.loads(line)["msg"].get("method") == "process/start"]
    outputs = []
    for r in sorted(out.glob("model-request-*.json")):
        for item in json.loads(r.read_bytes()).get("input", []):
            if item.get("type") == "function_call_output":
                outputs.append(item["output"])
    summary = {
        "mode": mode, "codex_rc": proc.returncode,
        "methods_sent": sorted({json.loads(line)["msg"].get("method") for line in open(out / "messages.jsonl")
                                if json.loads(line)["dir"] == "client->server"} - {None}),
        "process_start_count": len(starts),
        "process_start_has_sandbox": [s["params"].get("sandbox") is not None for s in starts],
        "inside_written": (work / "inside.txt").exists(),
        "outside_written": (outside / "written-by-codex.txt").exists(),
        "tool_output": outputs[-1][:500] if outputs else None,
    }
    if starts:
        (out / "process-start.json").write_text(json.dumps(starts[0], indent=2, ensure_ascii=False))
    import shutil  # noqa: PLC0415
    shutil.rmtree(outside, ignore_errors=True)
    summary["outside_dir_removed"] = not outside.exists()
    (out / "summary.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False))
    print(json.dumps(summary, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
