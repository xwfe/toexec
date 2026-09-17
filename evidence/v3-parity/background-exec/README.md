# 两个 Host 怎么跑后台命令、等 MCP 调用多久（原始记录）

对应 [v3 方案](../../../docs/plan/implementation-plan-v3-native-parity.md) 第 4.2 节第 7 项，以及 ccnm 的 P41.1。结论怎么决定了 ccnm 的设计写在 ccnm 的研究记录里，这里只放脚本、结果和打包代码摘录。**全程零模型额度**，只在本机（macOS arm64）跑。

## 要回答的问题

ccnm 的 `exec_command` 只有前台（最长 600 秒，到点杀掉）。要给它加后台运行，得先知道两件事：原生 CLI 的后台命令是什么形状（怎么起、怎么看、怎么停、会话结束时怎样）；Host 对一次 MCP 调用最多等多久（决定"等命令结束"这种阻塞调用能等多长）。

## 怎么测的

| 文件 | 做什么 |
| --- | --- |
| `run_codex.py` | Codex 连本机假模型，三个场景：`tools` 抓 `exec_command` / `write_stdin` 的定义；`lifecycle` 起一个 2 秒的命令（`yield_time_ms` 500）→ 空 `write_stdin` 等它结束 → 再起一个 `sleep 60` → 结束这一轮，codex 退出后看 sleep 还在不在；`timeout` 调一个睡 75 秒的 MCP 工具 |
| `slow_server.py` | `timeout` 场景用的探针 MCP server：睡够秒数再答，收到的每条请求和通知（含 `notifications/cancelled`）记时刻 |
| `runs/codex-0.154.0.json` | 三个场景的汇总。原始请求不入库 |

隔离和 [media-surface](../media-surface/README.md) 一样：`sandbox-exec` 禁非本机出站，`HOME` / `CODEX_HOME` 是临时空目录，模型接口是本机假服务。`lifecycle` 用 `-s danger-full-access`：Codex 自己的 seatbelt 套不进外层 `sandbox-exec`（第一次跑命令全部 `exit 71`，`sandbox_apply: Operation not permitted`），出站仍由外层挡住。

## 结果：Codex 0.154.0

**工具定义**（`tools` 场景，原文）：

- `exec_command`：`Runs a command in a PTY, returning output or a session ID for ongoing interaction.` 参数 `cmd`（一行 shell）、`workdir`、`shell`、`login`、`tty`（`false or omitted uses plain pipes`）、`yield_time_ms`（`Defaults to 10000 ms; effective range is 250-30000 ms.`）、`max_output_tokens`、以及沙箱提权的 `sandbox_permissions` / `justification` / `prefix_rule`。
- `write_stdin`：`Writes characters to an existing unified exec session and returns recent output.` 参数 `session_id`、`chars`（`Defaults to empty, which polls without writing.`）、`yield_time_ms`（`Non-empty writes default to 250 ms and cap at 30000 ms; empty polls wait 5000-300000 ms by default.`）、`max_output_tokens`。
- 没有单独的"停"工具。二进制里的报错：`stdin is closed for this session; rerun exec_command with tty=true to keep stdin open`——不开 tty 时 stdin 是关的。

**生命周期**（`lifecycle` 场景）：

| 调用 | 模型拿到的 |
| --- | --- |
| `exec_command` `echo started; sleep 2; echo finished`，yield 500 | `Wall time: 0.5026 seconds` / `Process running with session ID 36412` / `Output: started` |
| `write_stdin` 空字符串，yield 10000 | `Wall time: 1.3444 seconds` / `Process exited with code 0` / `Output: finished`——**进程结束就提前返回，只给上次之后的新输出** |
| `exec_command` `echo $$ > orphan.pid; exec sleep 60`，yield 500 | `Process running with session ID …`，随后这一轮结束 |
| codex 退出 1 秒后 | 那个 `sleep` 的 pid 已经不在：**会话结束时还在跑的后台进程被杀** |

**MCP 调用超时**（`timeout` 场景）：工具睡 75 秒，Codex 等满 75.1 秒，模型拿到 `Wall time: 75.0317 seconds` / `slept 75.0 s`；server 没收到 `notifications/cancelled`。默认配置下至少 75 秒内不会超时（更长的没测）。

## 结果：Claude Code 2.1.273（静态证据）

`strings` 二进制后读打包 JS，变量名是打包后的。

**Bash 的后台**：参数 `run_in_background: "Set to true to run this command in the background."`。结果文字（原文，函数 `Een`）有四种开头：

```js
r ? `Command was manually backgrounded by user with ID: ${e}. Output is being written to: ${n}.`
: s ? `Command was moved to the background (ID: ${e}) so that a message that arrived while it was running can reach you; it was not interrupted. …`
: d !== void 0 ? `Command did not complete within its ${…}s timeout and was moved to the background (ID: ${e}). …`
: `Command running in background with ID: ${e}. Output is being written to: ${n}.`
// 之后："You will be notified when it completes." / "To check interim output, use ${Read} on that file path."
// reapedAtFinalResponse 时改说：它在你给出最终回答时被终止，之后不会有通知
```

即：前台命令超时**转后台**而不是杀掉；命令结束时 Host 推通知把模型叫醒；中途输出让模型用 Read 读输出文件。

**看输出、停**：

- `TaskOutput`（别名 `BashOutput`）：`task_id`、`block`（默认 true）、`timeout`（`min(0).max(600000).default(30000)`）；轮询间隔 100 ms，任务不再是 running / pending 就返回。说明开头是 `DEPRECATED: Background tasks return their output file path in the tool result, and you receive a <task-notification> …`。
- `TaskStop`（别名 `KillShell`、`KillBash`）：`task_id`（`shell_id` 已弃用）。

**MCP 调用本身**：

```js
function ue(e,{isNonInteractiveSession:n=!1}={}){ if(W.has(e?.type??""))return 0; if(xc())return 0;
  if(n&&!a.CLAUDE_AUTO_BACKGROUND_TASKS)return 0; let s=a.CLAUDE_CODE_MCP_AUTO_BACKGROUND_MS;
  if(s!==void 0)return Math.min(Math.max(0,s),Jh); return P("tengu_mcp_auto_background",!0)?V:0 }   // V=120000
function Lo(e){ let r=(e?.timeout>=1000?e.timeout:void 0)??a.MCP_TOOL_TIMEOUT??Or; … }            // Or=1e8
function Jr(e){ … let r=a.CLAUDE_CODE_MCP_TOOL_IDLE_TIMEOUT??(n==="stdio"?Wr:Kr); … }              // Wr=1800000, Kr=300000
```

- 交互会话里一次 MCP 调用超过 **120 秒**，Claude Code 把它转成后台任务（开关默认开，非交互会话默认不转）。
- 总超时默认 1e8 ms；**空闲超时**（server 既不回答也不发进度）stdio 默认 30 分钟、其他传输 5 分钟，报 `sent no response or progress for N s; aborting`。
- MCP 标准的长任务（SEP-2663，`tasks/get` 等）客户端代码在，但入口判断 `function CL(){return!1}`——这个版本关着。
- MCP server 主动推消息进会话（`notifications/claude/channel`）要组织开关 `channelsEnabled`，默认没有。

## 对设计的直接影响

1. MCP 没有现成的办法让 server 在命令结束时叫醒模型，所以"看、等"必须是模型主动调的工具，并且要能阻塞等待（对应 TaskOutput 的 `block` / Codex 的空轮询），省得模型空转轮询。
2. 阻塞最长可以到 10 分钟：Claude Code 的 stdio 空闲超时 30 分钟、Codex 默认配置等满 75 秒没超时；超过 120 秒 Claude Code 会把这次调用转后台，结果照样回来。
3. 会话结束时停掉后台进程，两边原生都是这样（Codex 实测，Claude Code 的 `reapedAtFinalResponse` 分支）。
4. 停命令要有单独的工具（Claude Code 有 TaskStop；Codex 靠 tty 里发 Ctrl-C，ccnm 不做 tty）。
5. stdin 不是必需的：Codex 不开 tty 时 stdin 也是关的。

## 范围

只测了这两个版本。Claude Code 没有模型请求可看，全部来自打包代码。Codex 的"最多同时开几个后台进程"二进制里有提示文字、具体数没测；MCP 调用更长时间（比如 10 分钟）会不会超时没测。
