# 真实客户端经 ccnm 调到项目里声明的 MCP server（原始记录）

对应 [v4 方案](../../../docs/plan/implementation-plan-v4-machine-skills-mcp.md)第 3 步：ccnm 在项目那台机器上把 MCP server（项目的 `.mcp.json`、执行账号装的）经 `call_mcp_tool` 转给会话。ccnm 怎么设计写在 ccnm 的 P49 记录里，这里只放脚本和结果。

2026-09-22，macOS arm64，**零模型额度**。

## 怎么测的

`run_ccnm_relay.py`（`CCNM_BIN=<ccnm> python3 run_ccnm_relay.py <输出目录>`）：

- 假模型接口、临时 HOME、`sandbox-exec` 禁非本机出站，和 [`../../v3-parity/machine-skills/run_probe.py`](../../v3-parity/machine-skills/README.md) 同一套（直接 import 它的助手）；
- ccnm 那一半是**真实的 ccnm 二进制**跑 `internal mcp-serve`，用 external coding 的 payload（受管会话要一份核验过的绑定，本机没有 SSH 那一跳），外面套 `env -i`——和真部署里 SSH 那头的干净环境一样。不套的话 Claude 的 `ANTHROPIC_API_KEY` 会一路带进 ccnm，ccnm 的执行门会（正确地）拒绝起任何程序；
- 项目根下的 `.mcp.json` 声明本目录的 `fake_mcp_server.py`（和 ccnm 仓 `tests/fixtures/` 那份一样：说 2024-11-05，四个工具，`big` 回 52 000 字节）；
- Claude Code 的允许表照 ccnm 生成的写（`session::MCP_TOOLS` 全部，P49 起多一个 `mcp__ccnm__call_mcp_tool`）；Codex 的 `enabled_tools` 同样。

## 结果

`runs/claude-2.1.278-codex-0.155.1.json`（临时路径已换掉）。ccnm 是 P49 的开发构建（版本号仍报 0.8.0）。

| 客户端 | 结果 |
| --- | --- |
| Claude Code 2.1.278（print 模式） | 工具表里有 `mcp__ccnm__call_mcp_tool`，描述末尾是 `Servers here: fake.`；允许表放行，五次调用都没被权限拦：不带参数得到 server 清单，带 `server` 得到工具表和 server 自己的说明，调 `echo` 时嵌套的 `arguments`（`{"q":1,"nested":{"a":[1,2]}}`）原样到达，调 `big` 先拿到前 32 KiB 和一句"用 read_output 从哪接着读"，照着调 `mcp__ccnm__read_output` 从第 630 行接上 |
| Codex 0.155.1（`gpt-5.1-codex`） | `mcp__ccnm` 命名空间里有 `call_mcp_tool`；假模型发 `namespace=mcp__ccnm, name=call_mcp_tool` 的调用，嵌套参数原样回来 |

## 没测到的

- 受管会话那条路（Claude Code 经 SSH 起 `ccnm internal mcp-serve`、`requiresUserInteraction` 让有人值守的会话每次都问）：本机没有 SSH 那一跳。每次都问这件事在 ccnm 的单元测试里钉住了，真实 Host 会不会照做没跑。
- Codex 0.154.0（ccnm 受管会话钉的版本）：这台机器上的 `codex` 是 0.155.1。
- 真实模型会不会主动用这个工具。
