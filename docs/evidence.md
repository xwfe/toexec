# `evidence/` 里是什么

只想用那两个 crate 的话，这一篇可以不看。

这个仓库除了代码，还存着 gld / ccnm / toexec 跨仓方案（[v2 实施方案](plan/implementation-plan-v2.md)）的**实测脚本和结果文件**。放在这里是因为它们同时牵涉两个产品，放进任何一个产品仓库都不合适。

分工是固定的：**结论、判据和限制写在 `docs/plan/` 或产品仓库里；`evidence/` 只放能重跑的脚本和跑出来的结果。** 每个子目录自己的 `README.md` 说明怎么跑、跑出了什么。

## 按主线找

方案有四条主线（V2-K、V2-H、V2-Q、V2-C）和一条实验线（V2-P）。V2-H（gld hub 接 ccnm 远端）的记录在 gld 仓库的 `docs/rfc/0002-shared-kernel-and-ccnm-hub.md`，这里没有。

| 目录 | 对应 | 回答的问题 |
| --- | --- | --- |
| [`v2-k/`](../evidence/v2-k/duplication-audit.md) | V2-K 共享库 | gld 和 ccnm 到底哪些代码真重复。结论：只有"有界读一行"和"原子替换"两处，这就是现在的两个 crate |
| [`v2-q1/`](../evidence/v2-q1/README.md) | V2-Q 工具面 | Claude Code 会把 MCP `instructions` 截到多长 |
| [`v2-q2/`](../evidence/v2-q2/README.md) | V2-Q 工具面 | `alwaysLoad` 要不要开。整个方案里唯一花了模型额度的实验（20 次） |
| [`v2-c/`](../evidence/v2-c/) | V2-C Codex 原生链 | 见下一节 |
| [`v2-p/`](../evidence/v2-p/README.md) | V2-P Claude 实验线 | Claude 经 exec-server 执行有没有净收益。结论：没有，维持直接执行（[结果](plan/v2-p1-claude-trial-result.md)） |
| `v2-p/p33-sandbox/` | ccnm P33 | 用 `codex sandbox` 包住日常项目操作（构建、测试、`git commit`、下依赖）之后哪些还能做、慢多少，macOS 和 Linux 容器各一份 |
| [`v3-parity/skills-surface/`](../evidence/v3-parity/skills-surface/README.md) | [v3 方案](plan/implementation-plan-v3-native-parity.md)、ccnm P36 | 把项目 skills 交给模型有三条通道（工具 description、MCP prompts、MCP 官方 skills 扩展），Claude Code 和 Codex 各自真的支持哪条。零额度 |
| [`v3-parity/machine-skills/`](../evidence/v3-parity/machine-skills/README.md) | [v4 方案](plan/implementation-plan-v4-machine-skills-mcp.md)第 1 步、ccnm P48 | ccnm 受管会话里原生会不会列出 Agent 机器上装的 skills、放开原生 `Skill` 会漏出什么、同名谁赢；再拿真实 ccnm 接真实 Claude Code / Codex 验一遍。零额度 |
| [`v4-mcp/machine-mcp/`](../evidence/v4-mcp/machine-mcp/README.md) | v4 方案第 2 步、gld RFC-0006 | 本机装好的 MCP server 说哪个协议版本、工具表多大、一次结果能有多大（deepwiki 839 KB），以及经 gld 转过去之后分段读不读得全。真起 server、真连远端，不用模型额度 |
| [`x08-skill-frontmatter/`](../evidence/x08-skill-frontmatter/README.md) | 跨仓评审 X08、`toexec-skill` 0.2.0 | 同一个 SKILL.md，`toexec-skill` 和 Claude Code 自己的解析器（借它内嵌的 Bun 1.4.3）读得一样吗。结论：6 个公开仓库 + 本机共 1686 个文件、两批各 4000 个生成的畸形输入，修完后说不清的分歧为 0，剩下的归到 9 个有意不跟的原因。零额度 |
| [`x01-windows-replace/`](../evidence/x01-windows-replace/README.md) | 跨仓评审 X01 | `toexec-fs::replace` 在 Windows 上替换失败时旧文件还在不在。结论：老实现的先删后 rename 真会丢文件，现在一次 rename；只读目标替换不了（两种情况都不动旧文件）。零额度 |

## `v2-c/`：Codex 原生 exec-server 链

**这条线 2026-09-17 已封存**：ccnm 里的实现保留为 opt-in、只认 Codex 0.154.0、不再维护。原因和解封条件只写在 ccnm 的 `docs/plan/runtime-surfaces.md` 第 12.0 节。下面这些目录因此是历史记录，不会再随 Codex 新版本重跑。

| 目录 | 内容 |
| --- | --- |
| `g01/` | exec-server 协议到底接受什么、拒绝什么 |
| `g05/`、`g05-peer/` | 连接身份：URL 里放一次性令牌（否决）、按连接对端的 OS 用户放行（通过，现只作对照） |
| `g06/` | exec-server 的权限上限由谁决定（结论：它完全信客户端给的 sandbox） |
| `native-surface/` | Codex 连"项目只在对面"的 exec-server 时实际发什么请求（ccnm P21 的方法表） |
| `p23-stdio/` | Codex 按 `CODEX_HOME/environments.toml` 自己起 stdio 子进程当传输，不需要 WebSocket 网桥 |
| `p24-real/` | macOS Agent + Debian 13 Runtime 真机验收 |
| `p26-liveness/` | Agent 静默离网时 Runtime 侧的探活与空闲超时 |
| `p29-gates/` | 同会话并发、在途请求、资源上限的门禁补测（查出的缺陷由 ccnm P30 修复） |

## 重跑之前要知道

- 这些脚本大多要本机有 **ccnm 源码构建出的二进制**和 **Codex CLI 0.154.0**，路径、版本要求写在各目录的 README 里。
- **不要在登录状态的机器上直接跑 `codex exec`**——会消耗订阅额度。需要 Codex 参与的脚本都自带临时 `HOME` / `CODEX_HOME` 和本机假模型服务，照 README 给的命令跑。
- 按"任何带会话标记的进程"查残留的脚本（`v2-c/p29-gates/`）不能和 ccnm 的 `cargo test` 同时跑，会互相误报。
- 原始日志不提交，只提交汇总后的结果文件（各目录 `runs/` 下的 JSON）。
