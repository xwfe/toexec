# V2-G05：URL 路径里放一次性令牌，挡不挡得住、会不会漏

对应 [v2 方案](../../../docs/plan/implementation-plan-v2.md) 第 5.2 节的 V2-G05 候选方案。Codex 0.154.0，macOS arm64，2026-09-16。

## 结论先行

**按方案里事先冻结的判据，这个候选方案否决。**判据原话是"验证时必须检查日志与遥测是否出现该串；出现则该方案否决或需额外脱敏"。实测它会出现，而且脱敏只能改 Codex，官方 Agent 不许改。

分开看：

- **挡人这一半成立。**令牌错、重放、空路径一律 404，5 次重复结果一致。经 exec-server 跑的命令读不到 `CODEX_EXEC_SERVER_URL`。一切正常时，所有产物里都找不到令牌。
- **不漏这一半不成立。**只要会话中途断线一次，完整 URL（含令牌）就会出现在三个地方：发给模型的工具输出（真实环境里就是发给了 OpenAI）、`CODEX_HOME/sessions` 下的会话记录、`codex exec` 的 stderr。5 次重复，5 次都出现。
- 漏出去的是**已经用掉的**令牌：网桥在第一次升级成功时就把它作废了，断线后 227–229 次重连全部被拒。所以按"其他 OS 用户能不能连进来"这个威胁模型，它仍然挡得住；不满足的是 V2-G05 的"无原始 token 日志"这一条。要不要为"已作废令牌可以出现在日志里"改判据，是用户的决定，本轮不改。

**顺带确认的两件事**，不论用哪种认证方案都得知道：

1. **Codex 一启动就连 exec-server**，不是等到第一次工具调用。首次连接失败时，模型拿到的工具列表里根本没有 `exec_command`，命令也不会退回本地执行。
2. **断线后，客户端每 100 ms 用同一个 URL 重连一次，最多 25 秒**，实测 227–229 次。之后每次工具调用再连一次，没有退避。任何一次性令牌方案都注定断线后恢复不了——这和 ccnm v1 "不 resume"的语义一致，但网桥必须扛得住这 25 秒、每秒约 9 次的重连。

## 怎么测的

脚本 [`harness.py`](harness.py)，只用 Python 标准库。用法：`python3 harness.py <ok|wrong|drop|env> <新输出目录>`。每次运行生成一个 256 位随机令牌，拉起下面几样东西：

| 部件 | 做什么 |
| --- | --- |
| 真实 `codex exec-server --listen ws://127.0.0.1:<端口>` | 被保护的执行端 |
| 本机网桥 | 只放行请求路径与 `/<令牌>` 完全一致的**一次**升级，随即作废；转发时把路径改回 `/`，exec-server 自己看不到令牌 |
| 假模型接口 | 本机 HTTP，回 Responses 流式事件；按请求里实际提供的工具发起一次 `exec_command` 调用，并把每次请求原样存盘 |
| `codex exec` | `--ignore-user-config`，临时 `HOME`/`CODEX_HOME`（没有凭据），`CODEX_EXEC_SERVER_URL=ws://127.0.0.1:<网桥端口>/<令牌>`，外面套 `sandbox-exec` 禁掉所有非本机出站 |

**没有发出任何真实模型请求**：模型地址是本机假服务，非本机出站被沙箱拦掉，临时目录里没有凭据。本机 `~/.codex` 没读也没碰。

跑完做三件事：

1. 手工补发三次升级请求：网桥令牌、另一个随机串、空路径。
2. 在输出目录的**所有文件**里找令牌，全长和前 16 个字符各找一遍。范围包括会话记录、`CODEX_HOME` 里的 sqlite 及其 WAL、stdout/stderr、存下来的每一次模型请求、exec-server 的输出。
3. 在网桥录下的下行字节里，把 base64 解码后找探针输出，确认命令确实是 exec-server 执行的：服务端发往客户端的 WebSocket 帧不加掩码，exec-server 把进程输出按 base64 发回。

`runs/` 里只入库每次运行的 `summary.json`：令牌命中只记文件名和次数，不记令牌本身。入库前逐份核对过，汇总里不含令牌。会话记录、模型请求这些原始产物留在本机临时目录，没入库，因为断线场景的原始产物里有令牌。

## 结果（每个场景 5 次，结果一致）

下表的计数只算 `codex exec` 退出之前网桥看到的连接，跑完后手工补发的检查请求不算在内。

| 场景 | 做法 | 网桥放行 / 拒绝 | 命令经 exec-server 执行 | 令牌出现在 | 跑完后三次补发 |
| --- | --- | --- | --- | --- | --- |
| `ok` | 令牌正确，跑一条命令 | 1 / 0 | 是（base64 下行里解出探针） | 无 | 令牌 404（已作废）、随机串 404、空路径 404 |
| `wrong` | Codex 拿到的令牌不对（相当于网桥重启过） | 0 / 3 | 否，模型拿到的工具里没有 `exec_command` | 无 | 网桥令牌 101（从没被用过，本该能连）、随机串 404、空路径 404 |
| `drop` | 第一条命令成功后，网桥把两侧连接都断掉；模型再要求跑第二条 | 1 / 227–229（约 25.0 s 内） | 第一条是；第二条没执行，本地也没执行 | 发给模型的第 3 次请求、`sessions/…/rollout-*.jsonl`、stderr | 全部 404 |
| `env`（1 次） | 命令里检查 `$CODEX_EXEC_SERVER_URL` | 1 / 0 | 是 | 无；命令输出 `URL_ENV_ABSENT` | 全部 404 |

`drop` 场景里模型收到的工具输出原文（令牌打码）：

```text
exec_command failed: CreateProcess { message: "Rejected(\"Failed to create unified exec process: exec-server transport disconnected: failed to read websocket JSON-RPC message from exec-server websocket ws://127.0.0.1:51477/<令牌>: WebSocket protocol error: Connection reset without closing handshake; failed to resume exec-server session: recovery timed out after 25s\")" }
```

## 源码依据（rust-v0.154.0）

泄露的来路：连接标签 `exec-server websocket {url}`（`exec-server/src/client_transport.rs:554`）被拼进读失败的原因（`exec-server/src/connection.rs:535-546`），一路包进 `exec_command failed: {err:?}`（`core/src/tools/handlers/unified_exec/exec_command.rs:476`），再作为工具输出发给模型，并随会话记录落盘。连接失败的错误类型 `WebSocketConnect` / `WebSocketConnectTimeout` 本身也带 `{url}`（`exec-server/src/client.rs:639-646`）。

重连：`SESSION_RECOVERY_TIMEOUT` 25 秒、`SESSION_RECOVERY_RETRY_INTERVAL` 100 ms（`exec-server/src/client_recovery.rs:63-64`），`WebSocketConnect` 算可重试（同文件 `:838-848`）。

URL 只做 trim，特殊值只有 `none`，不校验路径（`exec-server/src/environment_provider.rs:107-113`）。

不漏的地方：`codex exec` 会在 `CODEX_HOME` 建 `logs_2.sqlite`，但每次扫描（含 WAL）都没有令牌；按源码，exec 不往里写日志，终端只输出 error 级别，而带 URL 的日志都是 warn/debug。TUI 和 app-server 会把 TRACE 级日志写进 `logs_2.sqlite`，那里会有 URL——本轮只测了 `codex exec`。OTel 这一块**只读了源码、没实测**：日志和 trace 导出默认关闭，指标默认上报 Statsig，但标签里只有 method、result 这类固定值。实测时非本机出站被沙箱拦掉，所以指标实际发了什么没看到。

## 没测到的

- 只测了 `codex exec`，没测 TUI 和 app-server；按源码，它们的 `logs_2.sqlite` 会带 URL。
- `env` 场景只证明**经 exec-server 起的**命令读不到这个变量。Codex 在本地起的子进程（hooks、stdio MCP server）会不会继承它，没测。它不在禁止继承的名单里（`protocol/src/shell_environment.rs:14-20`）。
- 只在 macOS 上测了，没在 Linux 上测。
- 没接 ccnm。网桥是测试用的 Python 版，不是产品实现。

## 下一步要用户决定的

计划写的是"未通过前 Codex 原生链只做合成数据实验"，现在就停在这里。可以往下走的方向（都**没验证**）：

1. **改判据**：接受"已作废的一次性令牌可以出现在日志和模型上下文里"。安全性质不变，但会话记录和发给 OpenAI 的文本里会留下本机端口和一段已作废的随机串。
2. **换认证方式，不靠秘密串**：网桥按连接对端的 OS 用户放行。Linux 可以用 sock_diag 查回环 TCP 连接属于哪个 uid；macOS 没有现成的 TCP 对端凭据接口，得按进程反查。什么都不传，就没东西可漏；代价是平台相关、要处理查询和连接之间的竞态。
3. **放弃环境变量，改走 HTTP 头**：源码里头部不会进错误文本，但只能通过 `environments.toml` 或内部 API 设置，和 `--ignore-user-config` 冲突。
