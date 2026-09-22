# 受管会话能开哪些 Agent 功能（原始记录）

对应 [v3 方案](../../../docs/plan/implementation-plan-v3-native-parity.md) 第 4.3 节和第 5 节第 6 步，以及 ccnm 的 P46。ccnm 按这些结果怎么设计写在 ccnm 的研究记录里，这里只放脚本和结果。**全程零模型额度**，只在本机（macOS arm64）跑。

## 要回答的问题

ccnm 的远端受管会话原先把原生工具关光：Claude 传 `--tools ""`，Codex 传 `web_search="disabled"`、`agents.enabled=false`。用户定了"默认开 WebSearch，其他 Agent 功能做成开关"，所以要先确认：

1. `--tools` 认哪些名字，写错的会怎样；
2. 开了子代理，子代理拿到的工具是不是也受限（能不能绕过去碰 Agent 机器的磁盘）；
3. 白名单一旦不再是空的，MCP 工具会不会被放进延迟加载池（P15 的前提是"`--tools ""` 时 ToolSearch 不可用"）；
4. print 模式没人答权限提示，哪些工具要写进 settings 的 allow 才真能用；
5. Codex 的 `web_search` 各取值实际往请求里加了什么，Code Mode 会不会把它排除掉。

## 怎么测的

| 文件 | 做什么 |
| --- | --- |
| `run_claude.py` | Claude Code 连本机假模型接口（`ANTHROPIC_BASE_URL` + 假 key），参数照 ccnm 的 `launch_cmd` 拼，只换 `--tools`。七个场景见脚本开头 |
| `run_codex.py` | Codex 连本机假接口，参数照 ccnm 的 `build_launch_cmd`（print 模式）拼，`web_search` 取 disabled / cached / live，Code Mode 开关各一遍 |
| `tiny_server.py` | 冒充 ccnm 的 MCP server：`read_file`（只读）和 `apply_patch` 两个工具 |
| `runs/claude-2.1.278.json` | Claude 七个场景的汇总 |
| `runs/codex-0.154.0-default-model.json` | Codex 不传 `--model`（ccnm 的默认做法） |
| `runs/codex-0.154.0-gpt-5.1-codex.json` | Codex 指定 `gpt-5.1-codex` |

隔离和 [media-surface](../media-surface/README.md) 一样：`sandbox-exec` 禁非本机出站，`HOME` 是临时空目录。Codex 用的是 GitHub release 的 0.154.0（ccnm 钉的版本），**要连同 `codex-code-mode-host` 一起放在同一目录**——只放主程序的话，默认模型自带的 Code Mode 起不来，工具面整个 fail closed，看到的是空表。

## 结果：Claude Code 2.1.278

**`--tools` 认的名字**（`names` 场景，把 22 个候选名一起传进去）：`WebSearch`、`WebFetch`、`Agent`、`Skill`、`TaskCreate`、`TaskUpdate`、`TaskList`、`TaskGet`、`TaskStop`、`NotebookEdit`。不认的**直接被忽略、不报错**：`TodoWrite`、`Task`（旧名）、`AskUserQuestion`、`EnterPlanMode`、`ExitPlanMode`、`ToolSearch`、`TaskOutput`、`KillShell`、`BashOutput`、MCP 资源两件套等。所以写错名字的后果是"那个功能悄悄没有"，不是启动失败。

**子代理**（`subagent`）：`--tools "Agent,WebSearch"` 时，子代理那次请求（系统提示的计费头带 `cc_is_subagent=true`）的工具表和主会话**完全一样**：`Agent`、`WebSearch` 加两个 MCP 工具，没有 Read / Bash。子代理不是绕过去的路。

**deny 是第二道锁**（`deny`）：`--tools "WebSearch,WebFetch"`，settings 里再 deny `WebFetch`，`WebFetch` 就不在工具表里了。

**ToolSearch 不能进白名单**（`toolsearch`，用 `ENABLE_TOOL_SEARCH=true` 强开工具搜索）：

| `--tools` | 模型拿到的 |
| --- | --- |
| `""` | 两个 MCP 工具全量，外加一个 `DeferredToolPlaceholder` |
| `WebSearch` | `WebSearch` + 两个 MCP 工具全量 + 占位 |
| `WebSearch,ToolSearch` | **只有** `ToolSearch` + 占位：MCP 工具和 WebSearch 全进了延迟加载池 |

即只要白名单里没有 `ToolSearch`，P15 的结论照旧成立。

**print 模式下哪些要写进 allow**（`calls`，假模型依次真调四个工具）：

| 工具 | allow 只有 MCP 工具（ccnm 原来的写法） | allow 再加上这四个 |
| --- | --- | --- |
| `WebSearch` | 被拒："requires approval, and this session has no approval surface" | 执行了（向模型接口发了带服务端搜索工具的请求） |
| `WebFetch` | 被拒（同上） | 过了权限；卡在 Claude Code 去 claude.ai 做的域名安全预检上（本机沙箱挡了出站，真实环境下这一步会发出去） |
| `Agent` | 执行了，拿到子代理的交回 | 同左 |
| `TaskCreate` | 执行了："Task #1 created successfully" | 同左 |

## 结果：Codex 0.154.0

**指定 `gpt-5.1-codex`**（工具在请求顶层的 `tools`）：

| `web_search` | 多出来的工具 |
| --- | --- |
| `disabled` | 无 |
| `cached` | `{"type": "web_search", "external_web_access": false}` |
| `live` | `{"type": "web_search", "external_web_access": true}` |

Code Mode 开着也一样：它是顶层的托管工具，不在 `excluded_tool_namespaces` 排除的 `functions` / `collaboration` 里。

**不传 `--model`**（CLI 默认 `gpt-6-astra`，工具在 `input` 里 `type: additional_tools` 那一项）：三种取值的请求除随机 ID 外逐字相同，**哪一种都没有搜索工具**。排除过的原因：不是自定义 provider 造成的（改走内置 openai provider、`openai_base_url` 指到假接口，结果一样）；不是 0.154.0 独有（本机 0.155.1 一样）；模型目录里这个模型写的是 `web_search_tool_type: "text_and_image"`、声明支持。没查明的：是这个形态下托管工具本来就不发，还是要别的条件。

另一个观察：默认模型下 ccnm 的 MCP 工具也不在请求里列出来，`exec` 的说明写着 "Some deferred nested tools may be omitted … listed in `ALL_TOOLS`"，模型要在 JS 里自己找。
