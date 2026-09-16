"""查本机回环 TCP 连接的客户端 socket 属于哪个 uid。查不到返回 None，由调用方按拒绝处理。

- Linux：读 /proc/net/tcp 和 /proc/net/tcp6 的 uid 列（world-readable，含其他用户的 socket）。
  产品里应换成 sock_diag netlink，连接多时不用整表扫文本。
- macOS：调同目录编好的 peeruid_macos（读 net.inet.tcp.pcblist64），见 peeruid_macos.c。
"""

import os
import subprocess
import sys
from pathlib import Path

HELPER = Path(os.environ.get("G05_PEERUID", Path(__file__).with_name("peeruid_macos")))


def peer_uid(client_port, server_port):
    # 任何异常都当作查不到：查询坏了只能拒绝，不能放行，也不能让连接挂着。
    try:
        if sys.platform == "darwin":
            result = subprocess.run([str(HELPER), str(client_port), str(server_port)],
                                    capture_output=True, text=True, timeout=5)
            return int(result.stdout) if result.returncode == 0 else None
        return _linux_peer_uid(client_port, server_port)
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def _linux_peer_uid(client_port, server_port):
    # /proc/net/tcp 里 IPv4 地址是按主机字节序打印的十六进制：127.0.0.1 在小端机上是 0100007F。
    v4 = "0100007F" if sys.byteorder == "little" else "7F000001"
    local = f"{v4}:{client_port:04X}"
    remote = f"{v4}:{server_port:04X}"
    mapped_prefix = "0000000000000000FFFF0000"  # ::ffff:127.0.0.1，同样按字节序打印
    local6 = f"{mapped_prefix}{v4}:{client_port:04X}"
    remote6 = f"{mapped_prefix}{v4}:{server_port:04X}"
    uids = set()
    for table, (want_local, want_remote) in (("/proc/net/tcp", (local, remote)),
                                             ("/proc/net/tcp6", (local6, remote6))):
        try:
            lines = Path(table).read_text().splitlines()[1:]
        except OSError:
            continue
        for line in lines:
            fields = line.split()
            # sl local rem st tx:rx tr:when retrnsmt uid timeout inode
            if len(fields) >= 8 and fields[1] == want_local and fields[2] == want_remote:
                uids.add(int(fields[7]))
    return uids.pop() if len(uids) == 1 else None
