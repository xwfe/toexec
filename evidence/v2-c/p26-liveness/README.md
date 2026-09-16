# ccnm P26：原生链的 Runtime 侧探活（原始记录）

结论、设计取舍和限制只写在 ccnm 的 [`docs/research/p26-native-liveness-2026-09-17.md`](https://github.com/xwfe/ccnm/blob/main/docs/research/p26-native-liveness-2026-09-17.md)。这里放脚本、每轮的结果文件和复跑方法。

2026-09-17，本机 macOS arm64，Codex 0.154.0（Homebrew），ccnm 为 P26 提交的 debug 构建。**全程零模型额度**：模型接口是本机假服务（`native-surface/probe.py` 的 `MockModel`），Codex 外面套 `sandbox-exec` 禁非本机出站，`HOME`/`CODEX_HOME` 是临时目录。

## 拓扑

沿用 P23 的本机拓扑（见 [`../p23-stdio/README.md`](../p23-stdio/README.md)），`exec-serve` 起在沙箱外：

```text
tmux → sandbox-exec → codex TUI（CODEX_HOME = 生成的 session home）
         └─ environments.toml 的 program：传输脚本 ── TCP 127.0.0.1 ──> harness
harness（沙箱外）→ ccnm internal exec-serve → codex exec-server --listen stdio → 命令
```

## 文件

| 文件 | 做什么 |
| --- | --- |
| `liveness_relay.py` | P26.1 的传输：P23 的中继，外加每隔 N 秒往 Codex 的 stdin 写一个 `ccnm/liveness` 请求，并把 Codex 对这些 id 的回答截下来记日志、不往后转 |
| `probe.py` | P26.1：真实 Codex TUI 三轮对话——第一轮跑命令、空闲一段、第二轮跑 `sleep 12`（探活落在命令执行中）、第三轮收尾；统计探活回答、断线、重连和 TUI 上有没有出现探活相关的字 |
| `silent_client.py` | P26.3：真实 `exec-serve`（默认 30 秒 / 10 分钟）+ 真实 `codex exec-server`，客户端握手、起一条长 `sleep` 后一个字节都不再发；记录收到的每行、退出码、stderr、锁标记、残留进程 |
| `frozen_codex.py` | P26.3：P23 原样链路（不注入任何东西），第一轮留下长 `sleep`，然后给 TUI 整棵进程树发 SIGSTOP（从 Runtime 看就是笔记本睡着了）；记录 `exec-serve` 何时放弃、留下什么，SIGCONT 后第二轮用户看到什么。带第二个参数时只冻那么多秒 |
| `runs/` | 每轮一个目录，只提交 `summary.json`（和 P23 一样）；原始日志、TUI 画面和临时 home 留在跑的那台机器上 |

## 复跑

```bash
python3 probe.py runs/<新目录> 5 40        # 每 5 秒探活一次，中间空闲 40 秒
python3 probe.py runs/<新目录> 0.25 10     # 高频
python3 silent_client.py runs/<新目录>      # 约 11 分钟
python3 frozen_codex.py runs/<新目录>       # 约 12 分钟
python3 frozen_codex.py runs/<新目录> 120   # 冻 2 分钟再恢复
```

`P23_CCNM` 指定 ccnm 二进制（默认 `../../../../ccnm/target/debug/ccnm`），`P23_CODEX` 指定 Codex。`silent_client.py` 和 `frozen_codex.py` 各用一个不常见的 `sleep` 时长找残留进程，开跑前确认进程表里没有同样的。

## 结果

见 ccnm 研究记录；每轮的数字在对应的 `runs/*/summary.json`。

两件看原始记录时要知道的事：

- 这些轮次跑的时候阶段还叫 P25、目录叫 `p25-liveness`，所以 `runs/` 里的绝对路径、tmux 套接字名和 `P25-AGENTS-MARKER` 都是旧名字。改号原因见 ccnm ROADMAP 的 P26 一节。
- `silent-defaults-start-rejected` 那轮的 `process/start` 被真实执行端以 `-32602` 拒了（录下的 `threadId` 是占位符），它只证明放弃和放锁，不证明无残留；`silent-defaults` 是去掉 `metadata` 之后的补跑。补跑的 `sleep_running_after` 里那个 pid 不是残留，是一个命令行里含 `sleep 3593` 字样的等待 shell，残留检查现已改成精确匹配。
