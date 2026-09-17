# toexec

给 AI 编程 Agent 用的共享 Rust 工作区执行库，供 gld 本地工具、ccnm 的直接 MCP 执行路径及后续项目复用。gld hub 通过 ccnm 公共接口访问远端 Runtime。Claude Code、Codex、Web AI 三种客户端都走同一条 MCP + 共享库路径：Codex 原生 exec-server 链做完验收后于 2026-09-17 封存，Claude 经 exec-server 的收益验证已否决。最终目标是三种客户端 × macOS / Linux / Windows，现状表在 v2 计划第 0.1 节。

A shared Rust workspace-execution library. All three clients (Claude Code, Codex CLI, Web AI via gld hub) use one MCP + shared-library path; the Codex exec-server chain is frozen after acceptance, and Claude delegation to exec-server was measured and rejected.

**当前状态：两个共享 crate 都在用。**

| crate | 管什么 | 当前 tag |
| --- | --- | --- |
| `toexec-text` | 有界行读取（`next_line`）——读一行但不把整行读进内存 | `toexec-text-v0.1.0` |
| `toexec-fs` | 原子文件替换（`write_durable` / `replace`）——落盘、带权限、一次 rename | `toexec-fs-v0.2.0` |

gld 和 ccnm 都按 tag 链接它们。**共用的只是这些纯机制**：两边的 `read_file` 契约不一样、回滚编排也不一样，都**不会统一**。哪些东西真重复、哪些只是看起来重复，逐项对照在 [重复度盘点](evidence/v2-k/duplication-audit.md)。

产品这样引用：

```toml
toexec-text = { git = "https://github.com/xwfe/toexec.git", tag = "toexec-text-v0.1.0" }
toexec-fs   = { git = "https://github.com/xwfe/toexec.git", tag = "toexec-fs-v0.2.0" }
```

按 tag 固定，不跟 `main` 走：改了共享库不会在某次 `cargo update` 之后突然改变产品行为，升级是显式的一步——这边发新 tag，那边改那一行。本地要同时改两边时临时换成 `path` 依赖，**别提交**，提交了两边 CI 就拉不到了。三个仓库的 `rust-version` 统一在 1.89。

Codex 原生 exec-server 那条线（V2-C）的实测证据在 [`evidence/v2-c/`](evidence/v2-c/)：连接身份、协议、权限三道门禁，P21 的方法表，以及 P23 的 stdio 传输（Codex 按 `CODEX_HOME/environments.toml` 自己起子进程，不需要 WebSocket 网桥）；产品实现在 ccnm 的 P21–P24，2026-09-16 已在 macOS Agent + Debian 13 Runtime 上真机验收（`evidence/v2-c/p24-real/`）；封存前最后两个阶段是 P29（并发、在途请求、资源上限的门禁补测，`evidence/v2-c/p29-gates/`）和 P30（修它查出的 fs helper 活过放锁）。V2-P1 量出来的那点收益——命令的 OS 沙箱——由 ccnm P33 搬到默认的 MCP 路径上（`evidence/v2-p/p33-sandbox/`）。**2026-09-17 封存**（ccnm P32）：opt-in 保留、钉 0.154.0、不再维护，原因在 ccnm `docs/plan/runtime-surfaces.md` 第 12.0 节。计划执行到哪、和原文哪里不一样，见 v2 计划第 11 节的对齐检查。

方案入口是 [v2 实施方案](docs/plan/implementation-plan-v2.md)：四条主线都已完成；V2-P1 的结论是维持直接执行（[结果](docs/plan/v2-p1-claude-trial-result.md)）；第 0.1 节是最终目标和当前覆盖表。[v1 原文](docs/plan/implementation-plan.md)保持不变，供历史对照；其中的实施与额度声明不自动成为 v2 授权。产品进度仍由各自仓库维护。

许可证 MIT（见 [LICENSE](LICENSE)）。两个 crate 都没有任何依赖，所以暂时没有第三方来源要记。

**一个 crate 管一件事**，名字是仓库名加职责，版本也各管各的——共享一个版本号会让没改过的 crate 跟着别人涨，tag 和代码就对不上了。tag 带 crate 名，各自独立发版。

抽东西进来的一条硬规矩：**错误的分类和措辞是产品的对外契约，不是可以顺手统一的实现细节。**`toexec-fs` 0.1.0 把四步失败合成一个 `io::Error`，接进 ccnm 时才发现那会把刷盘失败从「内部错误」悄悄变成「参数错误」，于是 0.2.0 改成报出是哪一步失败，由调用方自己归类。
