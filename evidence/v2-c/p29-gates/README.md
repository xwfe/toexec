# ccnm P29：原生链补测同会话并发、在途请求与资源上限（原始记录）

结论、适用性表和发现的缺陷只写在 ccnm 的 [`docs/research/p29-native-gates-2026-09-17.md`](https://github.com/xwfe/ccnm/blob/main/docs/research/p29-native-gates-2026-09-17.md)。这里放脚本、每个场景的结果文件和复跑方法。

2026-09-17，本机 macOS arm64，Codex 0.154.0（Homebrew），ccnm 为 P29 认领提交 `d5a5b76` 的 release 构建。**全程零模型额度**：没有 Codex TUI，也没有模型接口；客户端是一个不 import ccnm 或 Codex 的 JSON-RPC 客户端，发的请求是 ccnm P21 录下的 Codex 原始请求（`ccnm/tests/fixtures/codex-0.154.0/exec-server/`）。

## 拓扑

```text
脚本（中立客户端，同时扮演 ssh 交给 exec-serve 的 stdin/stdout）
  └─ ccnm internal exec-serve --payload <protocol 6>   （真实 Runtime 配置、真实写锁）
       └─ codex exec-server --listen stdio              （真实执行端，macOS Seatbelt 沙箱）
            ├─ 命令（每条自己一个进程组，带会话标记）
            └─ codex --codex-run-as-fs-helper           （带沙箱的文件读写，环境被清空）
```

没有 ssh 那一跳：P24 已在真机上测过 ssh 与 sshd 的故障，这里要看的是 `exec-serve` 与执行端之间的事。

## 文件

| 文件 | 做什么 |
| --- | --- |
| `common.py` | 起 Runtime 配置和 `exec-serve`、按 Codex 的方式回探活、按 `ps` 采样 RSS、按 `ps -E` 找带会话标记的进程 |
| `concurrency.py` | P29.2：`pipelined`（不等回包连发 280 个放行/被拒请求，同时一条命令刷 16 MiB 输出）、`same-path`（两个 `fs/writeFile` 同时写同一路径），各 20 轮 |
| `inflight.py` | P29.3：`client-leaves`、`helper-close`、`helper-crash` 各 20 轮，`terminate` 5 轮 |
| `resources.py` | P29.4：`output`（200 MiB 输出，中途 60 秒不读）、`readfile`（200 MiB 文件、超过 512 MiB 的稀疏文件）、`diskfull`（16 MiB 磁盘映像）、`expired`（保留期前后读、关闭句柄后读）各 5 轮，`writelimit` 1 轮 |
| `runs/` | 每个场景一个 `<脚本>-<场景>.json`，含版本、每轮结果 |
| `runs/p30-after-fix/` | ccnm P30 修掉 fs helper 活过放锁之后，用修复后的构建重跑的 `helper-crash`、`helper-close`、`client-leaves`、`terminate`。结果文件里的 `ccnm_commit` 是跑的时候的 HEAD `c80b0b1`，二进制多带了随后提交为 `6437528` 的改动 |

工作目录在 `work/`（不提交）。不放系统临时目录：exec-server 拒绝在那里建辅助链接（ccnm P21），macOS 的 `/tmp` 又是 ccnm 凭据审计判为 unknown 的符号链接。

## 复跑

```bash
python3 concurrency.py pipelined        # 约 1 分钟
python3 concurrency.py same-path
python3 inflight.py client-leaves
python3 inflight.py helper-close
python3 inflight.py helper-crash        # 会留下被挂到 pid 1 的 fs helper，脚本随后打开 FIFO 读端让它写完退出
python3 inflight.py terminate
python3 resources.py output             # 约 6 分钟
python3 resources.py readfile           # 每轮在 work/ 下写一个 200 MiB 文件，读完删掉
python3 resources.py diskfull           # hdiutil 建 16 MiB 映像、以当前用户挂载，结束时卸载删除
python3 resources.py expired            # 约 3 分钟
python3 resources.py writelimit
```

`P29_CCNM` 指定 ccnm 二进制（默认 `../../../../ccnm/target/release/ccnm`），`P29_CODEX` 指定 Codex。**场景之间不能并行跑**，也不要同时跑 ccnm 的 `cargo test`：残留检查按"任何带 `CCNM_EXEC_SESSION=` 的进程"找，别的会话的进程会被算进来。

## 看原始结果时要知道的事

- `helper-crash` 里 `helper_wrote_after_release` 是脚本在锁已经 `released` 之后打开 FIFO 读端收到的内容——那就是缺陷本身，不是脚本写的。`runs/` 里是修复前（P29），`runs/p30-after-fix/` 里是修复后，同一个脚本。
- `same-path` 第一批 20 轮没有记录回包顺序，补了 `reply_order` 之后重跑，`runs/` 里是重跑的结果。
- `readfile` 的 RSS 按 50 毫秒采样，一次读 1–2 秒只有十来个样本，峰值是下限。
