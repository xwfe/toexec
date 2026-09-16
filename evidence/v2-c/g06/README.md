# V2-G06：exec-server 的权限上限由谁决定

对应 [v2 方案](../../../docs/plan/implementation-plan-v2.md) 第 9 节 V2-G06：file / process / config / http 分别验收，缺 sandbox 不能默认放行。Codex 0.154.0，macOS 26.6.2 arm64，2026-09-16。不起 Agent，不发模型请求。

## 结论先行

**exec-server 自己不设上限，它完全信客户端传来的 sandbox 参数。**所以权限上限只能放在 exec-server 外面：一是运行它的 OS 身份，二是 ccnm 网桥。具体有五条：

1. **sandbox 为 `null` 等于不受限。**同一条"往工作区外写文件"的命令：带 Codex 原样发来的 sandbox 时被拒（Operation not permitted）；把 sandbox 改成 `null`，写进去了。`fs/writeFile` 同样如此。3 遍结果一致。
2. **沙箱边界由客户端自己填。**sandbox 里的 `workspaceRoots` 是客户端给的。把工作区外的目录加进去，服务端照做，写进去了。
3. **Codex 自己就会发 `sandbox: null`。**录下的一次普通会话里，34 次 `fs/getMetadata` 全是 `sandbox: null`，从工作区一路往上查到 `/`（找 `.git`、`AGENTS.md`）。所以网桥不能一刀切拒绝 null，得按方法区分。
4. **`http/request` 没有任何限制参数。**它照请求打到了本机测试服务，拿回 200 和响应体。本轮只打本机，但请求地址由客户端决定。录下的会话里 Codex 没调它。
5. **`environmentConfig/read` 会把服务端配置里的凭据原样返回。**在服务端 `CODEX_HOME/config.toml` 的 MCP 配置里放一个合成凭据，按 Codex 实际发的参数（`configPaths: [["mcp_servers"]]`）读一次，凭据在响应里。响应还带服务端的主目录、`CODEX_HOME` 路径和主机名，本机实测返回的是 Tailscale MagicDNS 全名。**Codex 每次启动都会调它**，不能直接挡掉。

**好的一面：Codex 给进程发的 sandbox 是有效的。**workspace-write 模式下工作区内能写、工作区外不能写；read-only 模式下两处都不能写。前提是 sandbox 参数原样到达服务端。

另外两件事：
- workspace-write 的策略里文件系统根目录是可读的（`root: read`）。带着 Codex 的 sandbox 读工作区外的文件照样成功。ccnm 现有 MCP 契约只允许读工作区内、拒绝 `..`，这里比它宽。计划要求"原生和 MCP 同样约束"，这个差距要在网桥上补，或者由用户明确接受。
- Codex 发给进程的环境策略是 `inherit: all`：进程继承 **exec-server 自己**的全部环境变量。exec-server 的环境必须按计划第 5.3 节做白名单，不能从登录 shell 继承。

## 对 ccnm 网桥意味着什么（设计依据，未实现）

| 方法 | 网桥规则 |
| --- | --- |
| `process/start` | sandbox 必须存在；`cwd`、`workspaceRoots` 必须等于 ccnm workspace 的根，不能多也不能在外面；文件系统条目不得宽于会话模式（只读会话不允许 write）；否则拒绝 |
| `fs/writeFile`、`fs/remove`、`fs/copy`、`fs/createDirectory` | 只读会话一律拒；coding 会话要求 sandbox 存在且根同上 |
| `fs/getMetadata`、`fs/readFile`、`fs/open`/`readBlock`、`fs/readDirectory`、`fs/walk` | Codex 会以 `sandbox: null` 调用。要么按 ccnm 读契约校验路径（只许工作区内，加上 Codex 往上找 `.git`/`AGENTS.md` 那几级的元数据查询），要么由用户决定接受"可读范围 = 执行账号可读范围" |
| `http/request` | 默认拒绝（计划 5.3） |
| `environmentConfig/read` | 放行（Codex 启动要用），但 Runtime 上 exec-server 的 `CODEX_HOME` 必须由 ccnm 生成、不含任何凭据；主机名等字段是否要在网桥上抹掉待定 |
| 所有方法 | 握手后核对 `executorVersion`/`providerId`；不转发未知通知（见 [G01](../g01/README.md)） |

**最后一道兜底还是 OS 身份**：exec-server 应以 ccnm 的受限执行账号（ccrun）运行。这样即使网桥漏过一条 `sandbox: null`，也只能摸到那个账号能摸到的东西。

## 怎么测的

1. [`capture.py`](capture.py)：真实 Codex 经 [G05 方向 2](../g05-peer/README.md) 的按 uid 放行网桥连 exec-server，网桥同时解 WebSocket 帧，把两个方向的 JSON-RPC 消息记下来。模型接口是本机假服务，外面套禁出站沙箱。模型发起一条命令：往工作区内写一个文件，再往 `~/.cache/g06-probe-<pid>/` 写一个。工作区外的目录放 `~/.cache` 而不放 `/tmp`，因为 workspace-write 默认允许写 `/tmp` 和 `TMPDIR`，放那里测不出来。测完删除。workspace-write、read-only 各跑一次。
2. [`replay.py`](replay.py)：取第 1 步录下的真实 `process/start` 参数，在 `--listen stdio` 的服务端上重放各种变体，外加 `fs/writeFile`、`fs/readFile`、`http/request`（只打本机 HTTP 服务）、`environmentConfig/read`（服务端配置里放合成凭据 `G06-SYNTHETIC-NOT-A-REAL-TOKEN`）。脚本跑 3 遍，结果逐项一致。工作区外的目录每次都删干净了。

## 结果

录制（`runs/capture-*`）：

| 模式 | Codex 发出的方法（次数） | 工作区内写 | `~/.cache` 下写 |
| --- | --- | --- | --- |
| workspace-write | `fs/getMetadata` 34 次（sandbox 全为 null）、`initialize`、`initialized`、`environmentConfig/read`、`process/start`（带 sandbox）、`process/terminate` 各 1 次 | 成功 | 被拒 |
| read-only | 同上 | 被拒 | 被拒 |

重放（`runs/replay-*`，3 遍一致）：

| 用例 | 结果 |
| --- | --- |
| `process/start`，Codex 原样的 sandbox，写工作区外 | 被拒，文件不存在 |
| 同上，sandbox 改成 `null` | **写入成功** |
| 同上，sandbox 的 `workspaceRoots` 由客户端加上工作区外目录 | **写入成功** |
| `fs/writeFile` 工作区外，带 sandbox | `-32600` Operation not permitted |
| `fs/writeFile` 工作区外，sandbox 为 `null` | **写入成功** |
| `fs/readFile` 工作区外，带 sandbox / 为 `null` | 两者都读到内容 |
| `http/request` GET 本机测试服务 | 服务端收到请求；返回 200 和响应体 |
| `environmentConfig/read`（`mcp_servers`） | 返回 `codexHomeDir`、`config`、`hostname`、`requirements`、`userHomeDir`；**合成凭据在响应里** |

`runs/capture-workspace-write/process-start.json` 是录下的原始请求，可以直接对照 sandbox 的结构。环境变量里只有会话 id 和终端设置，入库前检查过，没有凭据。

## 没测到的

- 只在 macOS 上测，沙箱是 Seatbelt。Linux 的 Landlock/bubblewrap 路径没测。
- 没测网络限制：sandbox 里 `network: restricted` 对进程是否生效、`enforceManagedNetwork` 怎么用。`http/request` 只打了本机，没验证外网。
- 没测 symlink、`..`、原始父路径这些路径边界（G06 的另一半）。
- 网桥的规则表只是设计依据，没有实现，也没接进 ccnm。
