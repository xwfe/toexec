# 本机装好的 MCP server 长什么样、回多大（原始记录）

对应 [v4 方案](../../../docs/plan/implementation-plan-v4-machine-skills-mcp.md)第 2 步：gld 把本机装好的 MCP server 经它的服务转给 ChatGPT 这类 Web AI。gld 按这些结果怎么设计写在 gld 的 [RFC-0006](https://github.com/xwfe/gld/blob/main/docs/rfc/0006-machine-mcp.md)，这里只放脚本和结果。

2026-09-22，macOS arm64，开发机真实的 `~/.claude.json` 和 `~/.codex/config.toml`（只读）。**不用模型额度**，但会真的起这些 server、真的连 context7、deepwiki、exa（只调了免费、只读的工具；exa 只列了工具，没搜）。

## 脚本

| 脚本 | 做什么 | 结果 |
| --- | --- | --- |
| `probe_servers.py <名字…>` | 照两份配置起 server，握手、翻完 `tools/list`；记协议版本、启动耗时、工具数、工具表字节数、最大的一条、说明（`instructions`）多大、stdout 有没有混进不是 JSON 的行。不记参数、环境变量、请求头 | `runs/installed-servers.jsonl` |
| `call_sizes.py` | 调 context7 和 deepwiki 几个只读工具，只记结果多大 | `runs/call-sizes.jsonl` |
| `e2e_relay.py <地址> <令牌>` | 像客户端一样经 gld 的服务调 `list_mcp_tools` / `call_mcp_tool` / `read_mcp_result`，只打大小和错误码 | `runs/gld-relay-e2e.txt` |

`e2e_relay.py` 跑的是隔离的 gld（`GLD_HOME` 指到临时目录、端口 28990、bearer 认证），开了 context7、deepwiki、exa-search、mcp-time、Filesystem 五个。

## 结果

**这台机器装了 29 个**（`~/.claude.json` 21 个、`~/.codex/config.toml` 23 个，同名 15 个，其中 13 个两边内容一样）：25 个是本机进程（`npx` / `bunx` / `uvx` / `node` / `python3` 起的），2 个是本机地址（`127.0.0.1`），2 个是远端地址（exa、deepwiki）；HTTP 的 4 个都是 streamable HTTP，没有老的 SSE 传输。

`probe_servers.py` 起了其中 12 个名字（两份配置里各有一份的都起了，共 19 次）：

| 看什么 | 结果 | 对 gld 的影响 |
| --- | --- | --- |
| 协议版本 | 12 个里 **2 个还是 2024-11-05**（`server-github`、`server-puppeteer`），其余 2025-06-18 | 客户端不能只认一个版本（gld 连 ccnm 的那个就只认一个） |
| 启动耗时（包已缓存） | 0.3–1.5 秒 | 握手默认等 30 秒够冷启动下载 |
| 工具表 | 1 个到 26 个工具；**playwright 21 KB、github 17 KB、Filesystem 13.6 KB** | 不把它们平铺进 gld 的工具表，由模型按需去问 |
| 带说明的 | Context7 632 字节、DeepWiki 3 KB | 列工具时一起给 |
| 有 `outputSchema` 的 | memory 9 个、Filesystem 14 个、deepwiki 3 个 | 结果里会带 `structuredContent` |
| stdout 混日志 | 这 19 次一次都没有 | 客户端照样跳过不是 JSON 的行（官方 SDK 也这么做） |
| HTTP 回复格式 | deepwiki、exa 都用 SSE（`text/event-stream`） | 要能读 SSE |
| exa 不带 User-Agent | **Cloudflare 回 403**（1010），带任何一个就是 200 | gld 总带 `gld/<版本>` |
| 本机开着 `HTTP_PROXY` 连 `127.0.0.1` | 被送进代理，回 502 | 本机地址一律不走代理 |
| 结果大小 | context7 的两个 1.6 KB、7.2 KB；**deepwiki `read_wiki_contents` 839 KB：407 KB 正文 + 420 KB 内容相同的 `structuredContent`** | 有文字时不带那份重复的；超过 64 KiB 分段，剩下的留着接着读 |

经 gld 转（`runs/gld-relay-e2e.txt`）：五个 server 都起得来、调得通；deepwiki 那次回给客户端 67.9 KB（第一段 65,520 字节加一句说明），再调两次 `read_mcp_result` 拿回全部 406,840 字节，一个字节不少；没开的 server、没有的工具各报各的错误码。停服务后 server 进程连同它们下面的 `node` / `python` 一个不剩（看的是进程组）。

## 没测到的

- 真实模型（ChatGPT）会不会先 `list_mcp_tools` 再调、会不会照说明去读后面的段：要额度和公网入口。
- Linux、Windows（Windows 上没有进程组，关 server 靠杀进程树）。
- 要 OAuth 登录的远端 server：这台机器上没有，只有测试里的假 401。
