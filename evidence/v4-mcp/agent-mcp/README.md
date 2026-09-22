# Agent 机器上装好的 MCP server 交给受管会话（原始记录）

对应 [v4 方案](../../../docs/plan/implementation-plan-v4-machine-skills-mcp.md)第 4 步：ccnm 受管会话接 Agent 机器上装好的 MCP server。ccnm 怎么设计写在 ccnm 的 P50 记录里，这里只放脚本和结果。

2026-09-22，macOS arm64，**零模型额度**。Claude Code 2.1.278；Codex 0.154.0（ccnm 受管会话钉的版本，从 release 下到临时目录、没装进系统）和 0.155.1（本机装的）。

## 怎么测的

`run_agent_mcp.py`（`[CCNM_BIN=<ccnm>] [PROBE_CODEX=<codex>] python3 run_agent_mcp.py <输出目录> [场景…]`）：假模型接口、临时 HOME、`sandbox-exec` 禁非本机出站，和 [`../../v3-parity/machine-skills/run_probe.py`](../../v3-parity/machine-skills/README.md) 同一套（直接 import 它的助手）。"装好的 server"是本目录的 `fake_agent_server.py`：一份走 stdio，一份走 streamable HTTP（127.0.0.1 随机端口），能回任意大小的文字。

- `native_*` 那几组：**先不经 ccnm**，把 server 直接写进 Claude Code 的 `--mcp-config` / Codex 的 `-c mcp_servers.*`，看原生客户端怎么对待它们（这是"技术上最简单"的做法，要先知道它行不行）。
- `ccnm_*` 那几组：照 ccnm 生成的配置，由**真实 ccnm 二进制**的 `ccnm internal agent-skills` 转（P50 的 `call_mcp_tool` / `read_mcp_result`）。

`real_remote.py`（`CCNM_BIN=<ccnm> python3 real_remote.py <输出文件>`）：不经模型，直接当 MCP 客户端，让真实 ccnm 经 `curl` 连**真实的 DeepWiki**（公开、免费、不用 key，第 2 步调过的同一个地址和两个只读工具）。只记大小和页数。

## 结果

### 直接写进原生客户端的配置：大结果会丢

| 客户端 | 结果 | 记录 |
| --- | --- | --- |
| Claude Code 2.1.278 | print 模式下没进允许表的 MCP 工具一律拒（"no approval surface"），`settings` 里写 server 级的 `mcp__<名字>` 就放行；HTTP 的也能直接连。**MCP 结果超过约 50 000 字符不交给模型**：存到 `~/.claude/projects/…/tool-results/`，模型只看到 2 KB 预览和路径，要它用 `Read` 去读——受管会话关了 `Read`，后面的就丢了。50 000 字节原样到达，52 000 字节就只剩预览。工具定义里带 `_meta["anthropic/maxResultSizeChars"] = 200000` 后 150 000 字节原样到达 | `runs/native-claude-2.1.278.json` |
| Codex 0.154.0 / 0.155.1，`gpt-5.1-codex`（工具在请求顶层） | `-c mcp_servers.<名字>={…}` 整张内联表认；`approval_policy="never"` 下不写 `default_tools_approval_mode="approve"` 每次都被拒。**任何工具结果超过约 12 KB 就只留头尾各 6 KB**，中间写一句 `…N tokens truncated…`（数字对不上，实际砍掉的多得多）。加 `-c tool_output_token_limit=20000` 后 81 000 字节原样到达 | `runs/native-codex-0.154.0.json`、`runs/native-codex-0.155.1.json` |
| Codex 同上，不写模型（CLI 默认模型，自带 Code Mode） | 32 KiB 原样；64 KiB 截到约 40 KB，开头写 `Warning: truncated output (original token count: 16384)`。`tool_output_token_limit` 对它不起作用 | 同上 |

所以 Agent 上的 server 不直接交给原生客户端，而由 ccnm 转：结果它自己切页（一次 32 KiB，其余留在内存里），每个 server 的全部工具也不进每一次请求（开发机上 playwright 一家 21 KB）。**附带查出的两处 ccnm 已有问题**（不止第 4 步）：ccnm 的 `read_file` / `load_skill` 一页最多 64 KiB，在 Claude 里会撞上 5 万字符那条线；ccnm 所有工具一页 16–32 KiB，在指定了模型的 Codex 会话里会被砍到 12 KB。ccnm 分别用上面那个 `_meta` 键和 `tool_output_token_limit=20000` 修了。

### 经真实 ccnm 转

| 客户端 | 结果 | 记录 |
| --- | --- | --- |
| Claude Code 2.1.278（print 模式） | 工具表里有 `mcp__ccnm_agent__call_mcp_tool`、`read_mcp_result`、`load_skill`，说明末尾是 `Servers here: fake, web.`；允许表放行，0 次拒绝。不带参数得到清单，带 `server` 得到工具表，stdio 的 echo 嵌套参数原样到达，HTTP 的 echo 经 `curl` 到达，52 000 字节的结果先交 32 760 字节（断在换行后面）和一句"用 read_mcp_result 从哪接着读"，照着调一次读完，**拼回来和原文一字不差** | `runs/ccnm-claude-2.1.278.json` |
| Codex 0.154.0，`gpt-5.1-codex`，照 ccnm 带 `tool_output_token_limit=20000` | `mcp__ccnm_agent` 命名空间里三个工具都在；嵌套参数原样；第一段 32 760 字节和末尾的说明**完整到了模型面前** | `runs/ccnm-codex-0.154.0.json` |
| 同上，不带那个键（对照） | 第一段只剩约 12 KB，**末尾那句"接着读"的说明整条被丢掉**（`[omitted 1 text items ...]`）——模型连"后面还有"都不知道 | 同上 |
| Codex 0.154.0，不写模型（Code Mode） | 模型在 exec 的 JS 里调 `tools.mcp__ccnm_agent__call_mcp_tool(...)` 再 `text()` 出来：嵌套参数原样，第一段和说明完整 | 同上 |
| Codex 0.155.1 | 和 0.154.0 一样 | `runs/ccnm-codex-0.155.1.json` |

### 真实远端：DeepWiki 经 ccnm 和 curl

`runs/real-deepwiki.json`。本机开着 HTTPS 代理（环境变量照常交给 ccnm，它再交给 `curl`）。列工具 1.8 秒（`DeepWiki 2.14.3`，3 个工具），`read_wiki_structure` 0.9 秒；`read_wiki_contents`（`modelcontextprotocol/rust-sdk`）原始回复 839 KB，ccnm 去掉和正文重复的 `structuredContent` 后是 406 840 字节，第一段 32 732 字节，**分 13 段读完，拼回来 406 840 字节**。和第 2 步经 gld 读到的是同一个大小。跑完临时目录（`curl` 的请求文件、测试 HOME）都不在了。

## 没测到的

- 真实模型会不会主动用这两个工具。
- 受管会话经 SSH 的那条路（这里 ccnm 自己那一半是冒充的，Agent 端服务是真的）。
- 要 OAuth 登录的远端 server：这台机器上没有，只有 ccnm 单元测试里的假 401。
- Linux 上的 `curl`。
