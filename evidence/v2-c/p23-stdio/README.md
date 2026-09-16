# P23：Codex 自带的 stdio 传输接 ccnm 的 exec-serve（ccnm P23.4 原始记录）

结论和实现只写在 ccnm 的 [`docs/research/p23-stdio-transport-2026-09-16.md`](https://github.com/xwfe/ccnm/blob/main/docs/research/p23-stdio-transport-2026-09-16.md)。这里放脚本、每个场景做了什么、3 轮的结果，以及怎么复跑。

Codex 0.154.0，macOS 26.6.2 arm64，2026-09-16。**零模型额度**：模型接口是本机假服务（复用 [`../native-surface/probe.py`](../native-surface/probe.py) 的 `MockModel`），Codex 外面套 `sandbox-exec` 禁非本机出站，`HOME`/`CODEX_HOME` 是临时目录。没在任何真实 Runtime 上装东西。

## 两批脚本

**第一批：开工前的 spike**（`spike.py` + `spike_relay.py`）。问的是"Codex 会不会按 `environments.toml` 里的 `program` 起一个 stdio 子进程当 exec-server 传输"。`program` 是一个 Python relay，连到沙箱外单独起的 `codex exec-server --listen ws://127.0.0.1:0`。场景 `basic` / `drop` / `basic-notrust` / `basic-configtrust` / `login`，结果表在 ccnm 那份记录的第 2 节。用法：`python3 spike.py <场景> <新输出目录>`。

**第二批：P23.4 端到端**（`harness.py` + `relay_client.py` + `exec_logged.py`）。真实 Codex TUI → `environments.toml` 指定的 `program` → 真实 `ccnm internal exec-serve`（P22 的 Runtime 半边，带规则表和写锁）→ 真实 `codex exec-server`：

```text
Codex（TUI，禁出站沙箱，临时 HOME，每会话 CODEX_HOME）
  └─ spawn program = relay_client.py ── TCP 127.0.0.1 ──> harness.py（沙箱外）
                                                          └─ ccnm internal exec-serve --payload <protocol 6>
                                                               └─ codex exec-server --listen stdio
```

**ssh 那一跳用本机 TCP 管道代替**，两个原因：这台机器没有第二台 Runtime，也没有给自己账号装 sshd 公钥（装了就是系统变更，要单独授权）；而且 exec-server 起在 Codex 的 Seatbelt 里装不上自己的第二层沙箱（spike 里命令全部 `sandbox_apply: Operation not permitted`，rc 71），所以 Runtime 半边必须在沙箱外。除此之外都是产品路径：ccnm 写的 `environments.toml` 形状（由 ccnm 单元测试钉住）、ccnm 传给 Codex 的启动参数（同一张表）、P22 的 `exec-serve`、官方执行端。`unreachable` 场景用的是**真的** `ccnm internal exec-transport`（经 `exec_logged.py` 记下 spawn 和 stderr），它 exec 成 ssh、ssh 解析不了别名，看 Codex 怎么办。

## 复跑

```bash
cd ccnm && cargo build                      # harness 默认用 ../ccnm/target/debug/ccnm，或设 P23_CCNM
python3 harness.py <basic|refused|drop|unreachable> <新输出目录>
sh batch.sh                                 # 三轮，写 runs/r1..r3
python3 compare.py runs/compare.json runs   # 逐项比
```

Runtime 侧配置和 ccnm 的 `exec_serve` 集成测试一样：临时 HOME（没有凭据，审计才能过）、`allow_unconfined_exec = true`、`codex_exec_server = true`、`codex_bin` 指向本机 Codex。入库的只有每次运行的 `summary.json` 和 `compare.json`，路径换成了 `<here>` / `<home>`。

## 场景与结果（3 轮，`compare.py` 逐项一致）

比的是：模型步数、计划是否走完、审批/信任提示次数、Runtime 侧连接数、`exec-serve` 退出码、Agent 侧传输程序被 spawn 的次数、Codex 发出的方法集合、每条 `process/start`（命令、sandbox 是否为 null）、被拒请求（方法、错误码、sandbox 是否为 null）、工作区/工作区外/Agent 本机目录里的文件、Codex 进程树的监听端口、写锁最终状态、模型收到的错误文本类型、auth.json symlink 是否还在、profile 目录有没有多出文件。不比耗时、pid、session id、pane 文本，`-32004`（根以上 `.git` 的回答）只比有无——上溯遍数随时序变（P21 也是）。

| 场景 | 做什么 | 结果 |
| --- | --- | --- |
| `basic` | 读 `AGENTS.md`、写文件、`apply_patch` 新增文件、再跑一条命令 | 传输程序 spawn 1 次，Runtime 侧 1 条连接，`exec-serve` 退出 0，写锁 `released`；`inside.txt` / `patched.txt` / `after.txt` 都在工作区里，工作区外和 Agent 本机目录为空；模型拿到 Runtime 那份 `AGENTS.md` 的标记和 `rc=0`；没有 `-32600`；会话中两次采样 Codex 进程树（4 个进程）**没有任何监听端口**；没有信任提示；profile 目录只有 `auth.json`，symlink 还在 |
| `refused` | 模型申请提权命令往工作区外写，再 patch 工作区外的文件，人都批准 | 7 条 `-32600`：`process/start`（sandbox null）1 条；越界 patch 的 `fs/getMetadata` / `readFile` / `writeFile` 3 条，Codex 随即自动以 sandbox null 重试的同 3 条。工作区外**零文件**。模型收到 `exec-server rejected request (-32600): ccnm refused process/start: a sandbox is required` 和 `Failed to write file …`；会话没死，第三条命令照常在工作区里写了 `alive.txt`；写锁 `released` |
| `drop` | 第一条命令的 `process/exited` 一到，传输程序自杀 | Codex **没有**再 spawn 第二个传输程序（1 次），Runtime 侧只有 1 条连接；第二条命令报 `exec-server transport disconnected`，`after.txt` 哪里都没有；`exec-serve` 见到 EOF 退出 0，写锁 `released`；模型仍能收尾（第 3 步 `done`） |
| `unreachable` | `program` 是真的 `ccnm internal exec-transport`，会话记录里的 Runtime 别名解析不了 | 传输程序被 spawn 1 次，它的 stderr 是 `ssh: Could not resolve hostname never-connect.invalid`；Codex 起来了，但模型调工具时报 `TypeError: tools.exec_command is not a function`——远端环境不可用、`include_local = false` 又没有本地环境，工具表是空的；Runtime 侧 0 连接，工作区和 Agent 本机目录都没有新文件。**没有退回本机执行** |

开发过程中另看到一条（没入 3 轮）：断线时 Codex 手里若有一个没写完的 patch，它会弹"command failed; retry without sandbox?"；答"是"也到不了 Runtime，到了也会被规则表拒（`sandbox: null` 一律拒）。产品文档的排错条目写了这一点。

## 没测到的

- ssh 那一跳（真实 `exec-transport` → `/usr/bin/ssh` → 远端 `exec-serve`）只有 `unreachable` 这一个失败方向；成功方向要两台机器，属 ccnm P24。
- Runtime 只在 macOS 上接过真 exec-server；Linux 的沙箱前提（bubblewrap + user namespace）沿用 P21 的容器结论。
- 只有默认模型；没有真实模型回合；没有并发会话争锁（P22 用假执行端测过写锁互斥，本轮没重复）。
- 每会话 CODEX_HOME 里 auth.json symlink 的**写穿透**只在 spike 的 `login` 场景用 `codex login --with-api-key` 验过；真实 ChatGPT 登录的 token 刷新走的是同一个 `save`，但没跑过。
