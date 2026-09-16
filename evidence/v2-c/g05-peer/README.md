# V2-G05 方向 2：按连接对端的 OS 用户放行

接上一轮 [`../g05`](../g05/README.md)：URL 里放一次性令牌的方案，断线后令牌会漏进发给模型的文本，按判据否决。2026-09-16 用户选了方向 2：网桥不认任何秘密串，只认"连进来的 socket 属于哪个 OS 用户"。Codex 0.154.0；macOS 26.6.2 arm64；Linux 的反例在 OrbStack 容器里跑（内核 7.0，aarch64）。

## 结论先行

**连接身份这一项通过。**

- **别的用户连不进来。**Linux 上网桥以 alice（uid 1000）运行：alice 连 5 次，5 次放行；bob（1001）和 root（0）各连 5 次，全部 403，网桥日志里记下的对端 uid 正是 1001 和 0。
- **同一个用户能用。**macOS 上 Codex 经网桥连 exec-server，命令确实由 exec-server 执行，5/5。
- **查不到就拒绝。**把查询工具换成 `/usr/bin/false`，5/5 拒绝；Codex 拿不到 `exec_command`，也不会退回本地执行。
- **没有秘密可漏。**URL 就是 `ws://127.0.0.1:<端口>`。断线后发给模型的错误里只有这个地址。
- **查询够快。**macOS 上连续 200 次查询，p50 5.2 ms、p99 7.0 ms、最大 8.4 ms，含每次起一个查询进程；产品里直接调 sysctl，不用起进程。

**顺带确认了一件必须由网桥守住的事：不 resume。**网桥放行重连时，Codex 断线后会自己 resume，把第二条命令跑成（5/5，每次放行 3 条连接）。ccnm v1 的语义是"断了就是断了"，所以网桥必须规定每个 Codex 会话只放行一条连接。这样配置时，断线后的重连在约 25 秒内被拒 195–207 次，第二条命令哪里都没执行。

**V2-G05 在这里只过了"未授权连接"和"无原始 token 日志"两项。**门禁里另外几项——跨主体/工作区/配置代次的句柄、合成凭据不可访问——属于接进 ccnm 之后的会话绑定，这个实验测不到，没有做。

## 怎么查"这条连接是谁的"

网桥 accept 之后，拿"客户端端口 + 网桥端口"去内核的 TCP 连接表里找客户端那一端的 socket，读出创建它的 uid，等于允许的 uid 才转发。不需要特权，也不需要客户端配合。

| 平台 | 读哪里 | 实现 |
| --- | --- | --- |
| macOS | sysctl `net.inet.tcp.pcblist64`（netstat 用的同一张表），`xsocket64.so_uid` | [`peeruid_macos.c`](peeruid_macos.c)，只用 SDK 公开头文件，结构体偏移由编译器定，不手算 |
| Linux | `/proc/net/tcp`、`/proc/net/tcp6` 的 uid 列 | [`peer.py`](peer.py)；产品里应换成 sock_diag netlink，连接多时不必扫整张表 |

**普通用户能看到别人的 socket，这点在 macOS 上单独验过。**以 uid 501 读这张表，170 个 IPv4 TCP socket 里有 97 个属于 uid 0。挑一条真实的回环连接（一端是 root 跑的本机代理，端口 7890；另一端是用户进程）两头各查一次，分别得到 0 和 501。所以别的用户连进来时，网桥查到的是"对方的 uid"，不是"查不到"。macOS 上没有 sudo，没能让另一个用户真的连进网桥；这个反例放在 Linux 容器里做了。

## 怎么测的

- [`harness.py`](harness.py)：Codex 端到端。沿用 [`../g05/harness.py`](../g05/harness.py) 的假模型接口、`sandbox-exec` 禁非本机出站和临时 `CODEX_HOME`，**零真实模型请求**。只把网桥换成按 uid 放行。用法：`python3 harness.py <ok|mismatch|lookup-broken|drop|drop-resume|bench> <新输出目录>`，macOS 上先 `cc -O2 -o peeruid_macos peeruid_macos.c`。
- [`linux_users.py`](linux_users.py)：Linux 反例，不起 Codex。在一次性容器里建 alice 和 bob；网桥以 alice 身份运行，后面接一个回固定响应的假上游；三个身份各连 5 次。用法见文件头。
- `runs/`：每次运行只入库 `summary.json`。

## 结果

| 场景 | 平台 | 次数 | 结果 |
| --- | --- | --- | --- |
| `ok`：同一用户 | macOS | 5 | 全部放行 1 条连接；命令经 exec-server 执行（下行 base64 里解出探针）；跑完后同一用户再连被"只放行一条"拒绝，403 |
| `mismatch`：网桥只认 uid 0 | macOS | 5 | 全部拒绝，每次 3 次尝试，日志记对端 uid 501；模型拿到的工具里没有 `exec_command` |
| `lookup-broken`：查询工具换成 `/usr/bin/false` | macOS | 5 | 全部 `lookup-failed` 拒绝；同上，没有 `exec_command`，没有本地回退 |
| `drop`：第一条命令后断线，只放行一条连接 | macOS | 5 | 重连被拒 195–207 次，codex 共 26.05–26.39 s 结束；第二条命令没执行；模型收到的错误里只有 `ws://127.0.0.1:<端口>` |
| `drop-resume`：断线后放行重连 | macOS | 5 | 每次放行 3 条连接，第二条命令 `AFTER_DROP` 执行成功——Codex 自己 resume 了 |
| `bench`：200 次查询 | macOS | 1 | 全部放行；p50 5.17 ms、p99 7.0 ms、最大 8.38 ms |
| `linux-users`：alice / bob / root 各 5 次 | Linux 容器 | 15 | alice 5/5 到达上游；bob 5/5、root 5/5 为 403，记下的对端 uid 分别是 1001、0 |

Codex 端到端场景里，放行判定的单次查询中位数在 5.7–10.5 ms 之间。

## 边界和没测到的

- **同一账号的其他进程能连进来。**这是计划里写明不防的：它本来就能读 Codex 的登录。
- **root 能绕过一切。**Linux 上 root 被拒，只是 uid 不相等的结果，不是一条安全性质。
- **网桥和 Codex 必须在同一个网络命名空间。**`/proc/net/tcp` 按命名空间隔离，容器化部署要注意。
- **竞态（推理，未实测）。**客户端在查询前就关掉连接时，它那一端要么进入 TIME_WAIT，要么已经不在表里；查不到就拒绝，所以只会误拒，不会误放。回环连接的四元组在两端都还没关时是唯一的，别的用户不可能复用同一个。
- 没在 Linux 上跑 Codex 端到端，Linux 只验了查询机制和反例。
- 只测了 `127.0.0.1`，没测 IPv6 和 `localhost`；URL 由 ccnm 生成，固定写 `127.0.0.1` 即可。没测 Windows。
- 没接进 ccnm。网桥是测试用的 Python 版。每个会话只放行一条连接、网桥随会话起停、只允许 Codex 所在账号的 uid，这些产品规则要在 ccnm 立阶段实现。
