# workspace-kernel

给 AI 编程 Agent 用的共享工作区执行底座：优先复用官方 Codex exec-server，让 Codex 原生协议和 Claude MCP 共用文件与进程执行能力；按验证出的缺口补充适配和编辑提交机制。供 gld、ccnm 以及以后需要同类能力的项目共用。

A shared Rust workspace-execution foundation for AI coding agents, with native Codex and Claude MCP entry points and an upstream-first execution backend.

**当前状态：只有方案，还没有代码。** 当前讨论入口是 [v2 实施方案（待评审）](docs/plan/implementation-plan-v2.md)：先无模型验证双入口，再决定是否扩展执行端。[v1 原文](docs/plan/implementation-plan.md)保持不变，供历史对照；其中的实施与额度声明不自动成为 v2 授权。产品进度仍由各自仓库维护。

拟采用许可证：Apache-2.0；实际代码落地时补充 LICENSE 和第三方来源记录。
