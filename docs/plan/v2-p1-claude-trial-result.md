# V2-P1：Claude 经 exec-server 的收益验证结果

日期：2026-09-17。按 [V2-P0 冻结文档](v2-p0-claude-trial-baseline.md)执行，判据一条没改。脚本和结果文件在 `evidence/v2-p/p1/`（`trial.py`、`exec_client.py`，结果 `runs/macos-1/p1.json`）。**模型额度 0 次。**

## 1. 结论：维持直接执行

- **A-proc（进程委派给 exec-server）不继续。**它确实挡住了 4 类直接路径挡不住的操作，但**不经 RPC 的 `codex sandbox` 用同一个权限对象挡住的是完全相同的 4 类**，按冻结判据第 9.2 节第 2 条，exec-server 那一跳没有增量。另外它让合法的 `git commit` 失败。
- **A-read（分块读委派）不继续。**读取范围和直接路径一模一样（都由 ccnm 规则表决定），没有新增约束；分页快，但服务端峰值内存高出约 51 MiB（多出来的是 exec-server 进程本身），不满足第 9.3 节第 2 条。
- **附一条建议，交用户决定**：如果想给 Claude 的 `exec_command` 加 OS 层沙箱，直接用 `codex sandbox` 包住命令就能拿到同样的约束，不需要 exec-server。代价和没解决的问题见第 8 节。这是 ccnm 自己的产品阶段，不属于"Claude 复用 exec-server"，**Claude 实验线在这里结束，V2-P2–P4 不进入**。

## 2. 跑的是什么

与 P0 同一台机器（macOS 26.6.2 arm64，10 核，账号 `bing`），同一个 ccnm release 二进制（sha256 `97a93d0b…e76440`，脚本开跑前核对）、同一个 Codex 0.154.0（sha256 `4f859826…95afcc`）。夹具由 P0 的 `baseline.py` 重新生成，路径不在临时目录下。

| 代号 | 实际怎么跑 |
| --- | --- |
| D | 中立 MCP 客户端 → `ccnm internal mcp-serve`（coding）→ `exec_command` / `read_file` / `read_output` |
| A | 中立 exec-server 客户端 → `ccnm internal exec-serve` → `codex exec-server`；`process/start` 带 P21 录下的 workspace-write 沙箱，`env` 为空（调用方是 ccnm 而不是模型）；读是每页一次 `fs/open` + `fs/readBlock`（64 KiB）+ `fs/close` |
| S | `codex sandbox --sandbox-state-json '{"permissionProfile": <录下的权限对象>, "sandboxCwd": …, "workspaceRoots": […]}' -- <同一 argv>`。**等价依据**：权限对象与 A 发给 exec-serve 的逐字节相同；这个 JSON 形状是按 `codex sandbox` 自己的报错（缺 `permissionProfile`、缺 `sandboxCwd`）一步步补出来的。S 在本机直接起，不经 ccnm |
| N | 裸 `codex exec-server`，`sandbox: null`，不经 ccnm，只测耗时 |
| L | 同一 argv 直接 `subprocess` 起，不包任何东西，作为地板 |

探针每轮按 D、A、S 交替；耗时按 D、A、S、N、L 交替，5 轮，每块前后记负载。负载平均值在 29–72 之间。

## 3. 探针

| 探针 | D | A | S | A、S 被挡时命令自己的报错 |
| --- | --- | --- | --- | --- |
| W1 工作区内写 | 5/5 | 5/5 | 5/5 | — |
| W2 工作区外写 | 5/5 | **0/5** | **0/5** | `…/outside/w2-A1.txt: Operation not permitted` |
| W3 HOME 写 | 5/5 | **0/5** | **0/5** | `…/runtime/home/w3-A1.txt: Operation not permitted` |
| W4 临时目录写 | 5/5 | 5/5 | 5/5 | — |
| W5 直接写 `.git/` | 5/5 | **0/5** | **0/5** | `…/workspace/.git/w5-A1: Operation not permitted` |
| R1 读工作区外 canary | 5/5 | 5/5 | 5/5 | — |
| R2 读 HOME canary | 5/5 | 5/5 | 5/5 | — |
| N1 连本机端口 | 5/5 | **0/5** | **0/5** | `nc` 失败时不打印原因；补跑同一连接用 `python3`：D `connected`，A、S 都是 `error 1 Operation not permitted` |
| L1 跑项目代码 | 5/5 | 5/5 | 5/5 | — |
| L2 `git add` + `git commit` | 5/5 | **0/5** | **0/5** | `fatal: Unable to create '…/.git/index.lock': Operation not permitted` |
| L3 读工作区文件 | 5/5 | 5/5 | 5/5 | — |

沙箱只管写和网络，**不管读**：录下的权限对象里根目录整体是 `read`，所以工作区外、HOME 里的文件命令照样读得到。`.git` 在工作区里被单独设成只读，这正是 L2 失败的原因。

## 4. 文件 RPC 读取（A-read）

| 请求 | D（`read_file`） | A（`fs/open`） |
| --- | --- | --- |
| 工作区外绝对路径 | 拒绝 `CCNM_E_POLICY` | 拒绝 `-32600 path is outside the workspace` |
| 含 `..` | 拒绝 | 拒绝 `-32600 path is not a plain file: URI` |
| symlink 指到外面 | 拒绝（symlink escape） | 拒绝（symlink escape，同一句） |
| `.git/config` | 允许 | 允许 |
| 工作区内文件 | 允许 | 允许 |

放行和拒绝的集合完全相同，拒绝都来自 ccnm 规则表，属于参数校验。G03 样本（空文件、BOM、CRLF、无尾换行、非法 UTF-8、跨块三字节字符）A 读回的字节与磁盘逐字节相同；D 按 `read_file` 契约渲染（去 BOM、非法字节换成 U+FFFD、超长行截断并注明），两者本来就不是同一种输出。

## 5. 环境与收尾（安全否决项）

- 子进程环境：A 比 D 多 `CCNM_EXEC_SESSION`、`CODEX_HOME`、`__CF_USER_TEXT_ENCODING`；S 比 D 多 `CODEX_HOME`、`CODEX_SANDBOX`、`CODEX_SANDBOX_NETWORK_DISABLED`、`__CF_USER_TEXT_ENCODING`。**没有凭据类变量**，D 本身也没有。
- 会话结束后：A 没有带会话标记的残留进程，两条路径的写锁都是 `released`，两个服务端退出码都是 0。
- 第 9.1 节四条否决项一条都没触发。

## 6. 开销（交替测量）

| 项 | D | A | S | N | L |
| --- | --- | --- | --- | --- | --- |
| M1 起进程 p50（5 轮中位数） | 3.11 ms | 35.66 ms | 32.86 ms | 3.11 ms | 2.80 ms |
| M1 p95 | 3.55 ms | 37.22 ms | 35.84 ms | 3.67 ms | 3.32 ms |

**多出来的 30 毫秒是沙箱，不是 RPC**：N（有 exec-server、没沙箱）和 D、L 几乎一样，A 和 S 都多约 30 毫秒。

| M2：一条命令输出 64 MiB | 墙钟（5 轮） | 传到客户端 | 服务端峰值 RSS |
| --- | --- | --- | --- |
| D，模型看到预览 | 2.65–2.68 s | 约 4.6 KB | 约 13 MiB |
| D，把输出全部取回（2048 次调用） | 0.38–0.41 s | 64.3 MiB | 约 72 MiB |
| A，适配层收完全部输出 | 2.70–2.72 s | 86.2 MiB（base64 + JSON） | 51–65 MiB |
| S / L，本机直接收 | 2.66–2.69 s / 2.87–2.89 s | — | — |

| M3：分页读 | D | A |
| --- | --- | --- |
| 8 MiB 多行，墙钟 | 0.40–0.45 s，127 次调用 | 0.11–0.12 s，129 页 × 3 次 RPC |
| 每页 | 约 3.2 ms | 约 0.9 ms |
| 服务端峰值 RSS | 约 9 MiB | 约 60 MiB |
| 8 MiB 单行 | 2 次调用就结束，只拿到前 64 KiB | 129 页读完全部 8 MiB |
| 128 MiB 多行（1 轮） | 20.3 s 读到第 1012 页后被拒（按行号定位超过 64 MiB 扫描上限） | 2.3 s 读完全部 2049 页 |

## 7. 逐条判据

| 判据 | 实测 | 结果 |
| --- | --- | --- |
| 9.1 安全否决 | 见第 5 节 | 未触发 |
| 9.2.1 至少一条新增约束 | W2、W3、W5、N1，均 D 5/5、A 0/5，且有沙箱报错 | 满足 |
| 9.2.2 S 给不出同样约束 | S 挡住的集合与 A 完全相同 | **不满足 → A-proc 不继续** |
| 9.2.3 合法操作不退化 | W1、L1、L3 5/5；**L2 git 提交 0/5** | 记为退化：不能替代现有 `exec_command` |
| 9.2.4 运行开销 | p50 +32.6 ms（≤ 100）、p95 +33.7 ms（≤ 300）；M2 墙钟 2.71 s vs D 2.66 s（≤ 2×+1 s）；RSS 比 D 的预览多 38–52 MiB（≤ 128） | 满足 |
| 9.2.5 部署与维护 | 同一个锁定版本；Codex 在 macOS 上装好是 277 MiB（`codex` 223 MiB + `codex-code-mode-host` 63 MiB，P0 文档写的"约 100 MB"是 Linux 发行包的下载大小，已按实测更正在此）；适配层要把 `exec_command` 的超时、预览、输出保留映射到 `process/*` 通知上，并把 exec-server 的监督放进 `mcp-serve`；因 9.2.2 已否决，没有细算代码量 | 未评估 |
| 9.3.1 新增读取约束 | 放行、拒绝集合与 D 相同 | 不满足 |
| 9.3.2 更快且内存不高 | 每页约 0.9 ms vs 3.2 ms（≤ 70%），但峰值 RSS 约 60 MiB vs 9 MiB；输出形态也不同于 D | **不满足 → A-read 不继续** |

## 8. 给用户决定的建议：`codex sandbox` 包住 `exec_command`

**能得到什么**：本机实测，工作区外写、HOME 写、`.git` 直接写、网络连接都被 OS 挡住，报 `Operation not permitted`。这正是直接路径现在完全没有的一层（`exec_command` 的安全今天只靠专用执行账号）。

**代价和没解决的事**：

1. **`git commit` 会失败。**Codex 的 workspace-write 权限对象把 `.git` 设为只读。要么接受"模型不能在沙箱里提交"，要么在权限对象里放开 `.git`——那就和 Codex 自己验过的对象不一样了，需要另测。
2. **每条命令多约 30 毫秒。**相对一次模型回合可以忽略。
3. **Runtime 要装锁定版本的 Codex**：macOS 装好 277 MiB；Linux 还要 bubblewrap 和 user namespace（原生链的前提，P21 实测）。没开原生链的 Runtime 是新增依赖。
4. **只在 macOS 上测过。**Linux 的 bwrap 路径完全没跑，而冻结文档要求进入任何后续阶段前补 Linux。
5. **读不受限**：沙箱不挡读，读的边界仍然只靠执行账号的文件权限。
6. **每次换 Codex 版本都要重跑这组探针**：`--sandbox-state-json` 的形状是按报错摸出来的，不是公开契约。

它要不要做、做成默认还是 opt-in，是 ccnm 的产品决定，应当在 ccnm 里另立阶段。

## 9. 顺带发现（不在本阶段处理）

**按行号分页读大文件，直接路径是越读越慢的。**`read_file` 每页从文件头扫到起始行，128 MiB 文件读到一半就花了 20 秒并被 64 MiB 扫描上限拦下；A 按字节偏移读，2.3 秒读完。这个差距来自"按行号定位"本身，不需要 exec-server：共享库记住行偏移就能拿到大部分收益。要不要做、是否改变 `read_file` 的上限语义，另作决定。

## 10. 没测到的

- Linux（bwrap）上的 A 和 S。冻结文档要求的是"进入 V2-P2 前补 Linux"，本次是否决、不进入 P2，所以不影响结论；但如果按第 8 节另立 S 的阶段，Linux 必须在那个阶段里先测。
- 真实 Claude 模型回合：按第 10.1 节，只有收益得到支持才测，本次否决，不测。
- S 经过 ccnm 产品路径的耗时：本次 S 是本机直接起，产品里要再加上 D 那一跳（M1 里约 3 毫秒）。
