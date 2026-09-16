#!/usr/bin/env python3
"""Linux 上的反例：另一个 OS 用户连进来会不会被拒。在一次性容器里以 root 运行。

建 alice、bob 两个用户；网桥以 alice 身份运行、只认 alice 的 uid，后面接一个回固定响应的
假上游；然后分别以 alice、bob、root 各连 5 次，看网桥的判定和客户端收到的第一行。
用法（宿主机上）：
  docker run --rm -v <g05 目录>:/ev/g05:ro -v <本目录>:/ev/g05-peer python:3.12-slim \
      python3 /ev/g05-peer/linux_users.py /ev/g05-peer/runs/linux-users
"""

import json
import os
import pwd
import socket
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent


def client(port):
    with socket.create_connection(("127.0.0.1", port), timeout=5) as s:
        s.sendall(b"GET / HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n")
        return s.recv(64).split(b"\r\n", 1)[0].decode() or "(closed)"


def serve_as_alice(port_file, log_file):
    """以 alice 身份跑：假上游 + 只认 alice 的网桥。"""
    import threading

    sys.path.insert(0, str(HERE))
    import harness  # noqa: PLC0415

    upstream = socket.socket()
    upstream.bind(("127.0.0.1", 0))
    upstream.listen(64)

    def answer():
        while True:
            conn, _ = upstream.accept()
            conn.recv(1024)
            conn.sendall(b"HTTP/1.1 200 UPSTREAM\r\nContent-Length: 0\r\n\r\n")
            conn.close()

    threading.Thread(target=answer, daemon=True).start()
    bridge = harness.PeerBridge(os.getuid(), upstream.getsockname()[1],
                                harness.base.Log(Path(log_file)), allow_reconnect=True)
    Path(port_file).write_text(str(bridge.port))
    while True:
        time.sleep(1)


def main():
    if sys.argv[1] == "--serve":
        return serve_as_alice(sys.argv[2], sys.argv[3])
    out = Path(sys.argv[1])
    out.mkdir(parents=True, exist_ok=False)
    for name in ("alice", "bob"):
        subprocess.run(["useradd", "-m", name], check=True)
    os.chmod(out, 0o777)
    port_file, log_file = out / "port", out / "bridge.jsonl"
    server = subprocess.Popen(["runuser", "-u", "alice", "--", sys.executable, __file__,
                               "--serve", str(port_file), str(log_file)])
    for _ in range(100):
        if port_file.exists() and port_file.read_text():
            break
        time.sleep(0.1)
    port = int(port_file.read_text())

    results = {}
    for user in ("alice", "bob", "root"):
        answers = []
        for _ in range(5):
            if user == "root":
                answers.append(client(port))
            else:
                r = subprocess.run(["runuser", "-u", user, "--", sys.executable, "-c",
                                    f"import sys; sys.path.insert(0, {str(HERE)!r}); "
                                    f"from linux_users import client; print(client({port}))"],
                                   capture_output=True, text=True)
                answers.append(r.stdout.strip() or r.stderr.strip()[-200:])
        results[user] = {"uid": pwd.getpwnam(user).pw_uid, "answers": answers}
    server.terminate()

    events = [json.loads(line) for line in log_file.read_text().splitlines()]
    decisions = [{k: e.get(k) for k in ("event", "reason", "peer_uid")} for e in events
                 if e.get("event") in ("accepted", "refused")]
    summary = {"scenario": "linux-users", "kernel": os.uname().release, "arch": os.uname().machine,
               "bridge_runs_as": {"user": "alice", "uid": pwd.getpwnam("alice").pw_uid},
               "clients": results, "bridge_decisions": decisions}
    (out / "summary.json").write_text(json.dumps(summary, indent=2))
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
