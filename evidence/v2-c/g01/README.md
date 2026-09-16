# V2-G01：exec-server 协议到底接受什么、拒绝什么

对应 [v2 方案](../../../docs/plan/implementation-plan-v2.md) 第 9 节 V2-G01：以固定版本实测为准，确认真正支持的初始化和 RPC 字段，以及对未知版本、未知方法、超大帧的处理；README 和源码不一致时，以实测为准。Codex 0.154.0，macOS 26.6.2 arm64，2026-09-16。

测的是 `codex exec-server --listen stdio`：ccnm 以后经 SSH 连 Runtime，走的就是 stdio。WebSocket 只补测了 Origin 头这一项。

## 结论先行

**协议能用，但有四件事决定了 ccnm 网桥不能只转发字节：**

1. **没有版本协商。**`initialize` 里塞一个假的 `"protocolVersion": "999.0"` 照样成功，服务端不看这个字段。能核对版本的只有返回的 `environmentInfo.executorVersion`（实测 `0.154.0`）和 `providerId`。ccnm 必须把 Codex 客户端和 exec-server 锁在同一个构建上，并在握手后自己核对。
2. **危险方法都开着。**`http/request`、`fs/writeFile`、`fs/remove`、`fs/copy`、`process/start` 全部注册。计划第 5.3 节要求 `http/request` 默认不放行、只读会话不能写，服务端自己不提供按会话关闭的开关，所以只能由网桥解析 JSON-RPC、按方法白名单过滤。
3. **一个未知通知就会断开整个连接，而且不回任何错误。**README 写的是"回一个 id 为 -1 的错误"，那句已经过时。源码 `exec-server/src/server/request_dispatcher.rs:118-123` 只打一条 warn 就关连接。网桥不能把自己不认识的通知转给服务端。
4. **超过 64 MiB 的帧直接断开连接，同样不回错误。**63 MiB 的合法请求 0.32–0.37 s 处理完；超过上限的那一行一写进去，连接就关了。上限来自 `exec-server/src/connection.rs:38` 的 `MAX_STDIO_JSONRPC_MESSAGE_LEN`。

**对 ccnm 有利的两点：**

- **握手顺序是服务端强制的。**没 `initialize` 就调方法、没发 `initialized` 就调方法、`initialize` 发第二次，都回 `-32600`，连接保持。
- **stdin 一关，服务端就把自己起的进程都清掉。**起一个 `sleep 300` 后关 stdin：服务端退出码 0，2 秒后进程已经不在。SSH 断开时就是这个效果，不会留下孤儿进程。

## 怎么测的

[`probe.py`](probe.py)：中立客户端，不 import 任何 Codex 代码，只用标准库，不起 Agent、不发模型请求。用法：`python3 probe.py <新输出目录>`。每种异常输入单独起一个服务端实例，记下服务端回了什么，再调一次 `environment/info` 看连接还在不在。整个脚本跑了 3 遍（[`runs/`](runs)），21 项结果逐项相同，只有耗时不同。

## 结果

| 输入 | 服务端回什么 | 之后连接 |
| --- | --- | --- |
| 握手前调 `environment/info` | `-32600` client must call initialize before using environment info methods | 保持 |
| `initialize` 带未知字段和假 `protocolVersion` | 成功，返回 `sessionId`、`environmentInfo` | 保持 |
| `initialize` 之后、`initialized` 之前调方法 | `-32600` client must send initialized before … | 保持 |
| 第二次 `initialize` | `-32600` initialize may only be sent once per connection | 保持 |
| 未知通知 `bogus/notify` | **什么都不回** | **断开** |
| 未知方法 `bogus/method` | `-32601` exec-server stub does not implement `bogus/method` yet | 保持 |
| 坏 JSON `{not json` | id `-1`，`-32600` failed to parse JSON-RPC message … | 保持 |
| 缺 `"jsonrpc"` 字段 | 正常处理 | 保持 |
| 字符串 id `"s-1"` | 正常处理，原样带回字符串 id | 保持 |
| `null` id | id `-1`，`-32600` … untagged enum RequestId | 保持 |
| 参数里带未知字段 | 忽略，按已知字段处理 | 保持 |
| 63 MiB 的合法请求 | 正常处理（0.32–0.37 s） | 保持 |
| 超过 64 MiB 的一行 | **什么都不回** | **断开**，服务端退出码 0 |
| 有进程在跑时关 stdin | 服务端退出码 0；进程 2 秒内被清掉 | — |
| WebSocket 升级不带 Origin | `101` | — |
| WebSocket 升级带 `Origin` | `403` | — |

`initialize` 返回的 `environmentInfo` 字段：`capabilities`、`cwd`、`executorVersion`、`platformOs`、`providerId`、`shell`、`tempDir`、`temporaryDirectories`、`userHomeDir`。README 的示例里只列了其中 4 个。

**方法表**：握手后用空参数把每个方法调一遍，只看回的是"方法不存在"还是别的错误。

| 结果 | 方法 |
| --- | --- |
| 直接成功 | `environment/info`、`environment/status` |
| `-32602` 参数错误（说明方法存在） | `http/request`、`process/start`、`process/read`、`process/write`、`process/signal`、`process/terminate`、`environmentConfig/read`、`capabilityRoots/discoverV1`、`fs/readFile`、`fs/open`、`fs/readBlock`、`fs/close`、`fs/writeFile`、`fs/createDirectory`、`fs/getMetadata`、`fs/canonicalize`、`fs/readDirectory`、`fs/walk`、`fs/remove`、`fs/copy` |
| `-32601` 方法不存在 | `process/output`、`process/exited`（它们是服务端发出的通知）、`network/policyRequest`、`shutdown`、`exec` |

加上 `initialize`，客户端能调的请求方法一共 23 个，另有 1 个客户端通知 `initialized`。和源码 `exec-server/src/server/registry.rs` 注册的完全一致；README 的 API 小节漏了 `environmentConfig/read`、`environment/status`、`capabilityRoots/discoverV1`、`http/request`。

## 没测到的

- 只测了"方法在不在、握手和帧"，没测每个方法的语义。进程、分块读、权限、沙箱分别是 V2-G04、G03、G06 的事。
- `--concurrent-requests` 大于 1 时的行为没测，默认是串行。
- 只在 macOS 上测了。
- 方法白名单、版本核对、不转发未知通知这些规则只给出了依据，没有实现；写进了 ccnm P22 的验收，放在 Runtime 侧的受管入口里做，不在 Agent 侧网桥上。
