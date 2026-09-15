# workspace-kernel

给 AI 编程 Agent 用的共享 Rust 工作区执行库，供 gld 本地工具、ccnm 的直接 MCP 执行路径及后续项目复用。gld hub 通过 ccnm 公共接口访问远端 Runtime。Codex 保留原生 exec-server 路线；Claude 是否复用其分块读取、进程管理和沙箱，单独做无模型收益验证，不作为默认依赖。

A shared Rust workspace-execution library. Local gld tools do not depend on Codex; Claude delegation to exec-server remains a separate cost-benefit experiment.

**当前状态：只有方案，还没有代码。** 当前讨论入口是 [v2 实施方案（待评审）](docs/plan/implementation-plan-v2.md)：共享库与 hub 独立推进；保留 V2-P1，验证 Claude 经 exec-server 的额外约束是否值得部署、协议与性能代价。[v1 原文](docs/plan/implementation-plan.md)保持不变，供历史对照；其中的实施与额度声明不自动成为 v2 授权。产品进度仍由各自仓库维护。

拟采用许可证：Apache-2.0；实际代码落地时补充 LICENSE 和第三方来源记录。
