# workspace-kernel

给 AI 编程 Agent 用的工作区执行内核：在项目机器上有界地读文件、原子地改代码、可靠地跑命令，并以省 token 的工具集暴露出去。供 gld、ccnm 以及以后需要同类能力的项目共用。

A shared Rust workspace-execution kernel for AI coding agents: bounded reads, atomic edits, reliable process sessions, and token-lean tool profiles.

**当前状态：只有计划，还没有代码。** 实施计划、已定决策、阶段与验收都在 [docs/plan/implementation-plan.md](docs/plan/implementation-plan.md)，那是唯一的计划来源。

许可证：Apache-2.0（LICENSE 文件在 P0.3 补齐）。
