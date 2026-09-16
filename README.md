# toexec

给 AI 编程 Agent 用的共享 Rust 工作区执行库，供 gld 本地工具、ccnm 的直接 MCP 执行路径及后续项目复用。gld hub 通过 ccnm 公共接口访问远端 Runtime。Codex 保留原生 exec-server 路线；Claude 是否复用其分块读取、进程管理和沙箱，单独做无模型收益验证，不作为默认依赖。

A shared Rust workspace-execution library. Local gld tools do not depend on Codex; Claude delegation to exec-server remains a separate cost-benefit experiment.

**当前状态：第一个共享 crate 已经在用。** `crates/toexec` 提供有界行读取（`next_line`），gld 和 ccnm 都按 tag 链接它——两边的 `read_file` 契约不一样、**不会统一**，共用的只有「读一行但不把整行读进内存」这一个原语。哪些东西重复、哪些只是看起来重复，逐项对照在 [重复度盘点](evidence/v2-k/duplication-audit.md)。

产品这样引用它：

```toml
toexec = { git = "https://github.com/xwfe/toexec.git", tag = "v0.1.0" }
```

按 tag 固定，不跟 `main` 走：改了共享库不会在某次 `cargo update` 之后突然改变产品行为，升级是显式的一步——这边发新 tag，那边改那一行。本地要同时改两边时临时换成 `path` 依赖，**别提交**，提交了两边 CI 就拉不到了。三个仓库的 `rust-version` 统一在 1.89。

方案入口是 [v2 实施方案](docs/plan/implementation-plan-v2.md)：共享库与 hub 独立推进；保留 V2-P1，验证 Claude 经 exec-server 的额外约束是否值得部署、协议与性能代价。[v1 原文](docs/plan/implementation-plan.md)保持不变，供历史对照；其中的实施与额度声明不自动成为 v2 授权。产品进度仍由各自仓库维护。

许可证 MIT（见 [LICENSE](LICENSE)）。`toexec` 没有任何依赖，所以暂时没有第三方来源要记。
