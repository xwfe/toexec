# V2-Q1：Claude Code 会把 MCP instructions 截到多长

对应 [v2 方案](../../docs/plan/implementation-plan-v2.md) 第 8 节 V2-Q 线 Q1、第 13 节 ccnm 最后一项。

## 实验单（运行前冻结）

| 项 | 值 |
| --- | --- |
| 授权引用 | `user-consent-2026-09-15` |
| `max_runs` | 2：正式 1 次；只有连接/认证/CLI 报错这类没进到模型判断的失败才补 1 次，补跑计入 |
| 本实验前累计 | 0 / 145 |
| deadline | 单次 5 分钟 |
| 停止条件 | 拿到一次模型实际作答即停；两次都失败则记失败并停止，不再追加 |
| 客户端 | Claude Code 2.1.269（Homebrew cask `claude-code@latest`），macOS arm64，`--model sonnet` |
| 隔离 | `--strict-mcp-config` 只挂探针；`--tools ""` 且禁用探针唯一工具，模型只能凭上下文作答；标记值每次随机生成、只在进程环境里 |

### 探针版面

`python3 probe_server.py --layout` 的输出：说明共 3018 个 UTF-16 码元、8114 个 UTF-8 字节（中文填充，每字 3 字节）。五个标记：

| 标记 | UTF-16 起点 | UTF-16 终点 | UTF-8 字节起点 |
| --- | --- | --- | --- |
| Z | 300 | 317 | 820 |
| A | 900 | 917 | 2430 |
| B | 2000 | 2017 | 5410 |
| C | 2100 | 2117 | 5660 |
| D | 3000 | 3017 | 8090 |

### 判据（运行前写定）

| 模型实际看到 | 结论 |
| --- | --- |
| Z、A、B，看不到 C、D，末尾有 `[truncated]` | 按 2048 个 UTF-16 码元截断（与静态证据一致） |
| 只有 Z | 按 2048 字节截断 |
| Z、A，看不到 B | 按约 2000 字符截断 |
| 五个都在 | 这条路径不截断 |
| 其他组合或列出不存在的标记 | 结果无效，不下结论（计入次数，不补跑） |

## 静态证据（不耗额度）

从本机二进制 `strings` 抽出的打包 JS（`GIT_SHA d0733697`，`BUILD_TIME 2026-09-11T17:33:46Z`）：

```js
var FT=2048
function Ro(e,n){if(!e)return e;return Ao(e,"Server instructions",n)}
function Ao(e,n,r){if(e.length<=FT)return e;
  if(r!==void 0)J(r,`${n} truncated from ${e.length} to ${FT} chars`);
  return ne(e,FT)+"… [truncated]"}
```

连接建立后 `Ro(getInstructions(), serverName)` 处理一次再存入连接对象。`e.length` 是 JS 字符串长度，即 UTF-16 码元数；`ne` 截断时避开半个代理对。所以"2KB"准确说是 **2048 个 UTF-16 码元**：纯 ASCII 约 2 KiB，中文约 6 KiB。

## 结果（2026-09-15）

### 结论

- **客户端层已确认**：Claude Code 2.1.269 把 MCP instructions 截到 **2048 个 UTF-16 码元**，后面接 `… [truncated]`。不是 2KB 字节；中文每字算 1 个，所以约 6 KiB 中文才会被截。
- **模型侧判据未执行**：唯一一次尝试在认证阶段失败，没有发出模型请求。上面判据表没有被评估，不能写成"模型看到了 Z、A、B"。
- 同一个常量 `FT=2048` 也出现在工具描述的超限统计里（`descriptionOverLimitCount: R>FT`）；描述进模型前是否同样截断，本轮没追到调用点，不下结论。

### 尝试记录

| 次 | 时间 (UTC) | 结果 | 模型请求 | 计入 `max_runs` |
| --- | --- | --- | --- | --- |
| 1 | 2026-09-15T14:40:45Z | CLI 返回 `Not logged in · Please run /login`，耗时 62 ms | 无（`Could not resolve authentication method`，请求未发出） | 是（1/2） |

累计模型运行：0 / 145（这次没有真正调用模型）。

同一次尝试的 debug 日志里，模型请求之前客户端已经连上探针并截断（原样摘录，已删与本题无关的行）：

```text
MCP server "q1probe": Successfully connected (transport: stdio) in 79ms
MCP server "q1probe": Server instructions truncated from 3018 to 2048 chars
MCP server "q1probe": Connection established with capabilities: {"hasTools":true,...}
```

进模型的路径（同一二进制静态追踪）：连接时 `instructions: Ro(getInstructions(), name)` 存入连接对象 → `mcp_instructions_delta` 用这个字段拼 `## <server>\n<instructions>` → 以 `# MCP Server Instructions` 附件进入上下文。中间没有再读原始值。

### 为什么认证失败、怎么补

本会话运行在 Claude 桌面端里，子进程继承了宿主的 `ANTHROPIC_BASE_URL` 等环境，但拿不到宿主的凭据；干净环境下 `claude auth status` 显示本机 CLI 自己也没登录（`loggedIn: false`）。按不可变前提，不把宿主凭据转给子进程。

要补模型侧确认：用户在自己的终端里 `claude auth login` 后跑 `evidence/v2-q1/run.sh <仓库外目录>`，剩 1 次额度；判据仍按上表。

### 对 ccnm 的影响

ccnm 的 instructions 顺序是：基础说明（约 400 字符）→ 项目说明文件正文 → 其他说明文件清单 → `[project instructions: …]` 标记行，上限按 16 KiB 字节算。只要总长超过 2048 个 UTF-16 码元，Claude Code 先截掉的就是末尾的**清单和标记行**——而标记行正是告诉模型"少了多少、怎么读剩下的"那一行，模型只会看到一个没头没尾的 `… [truncated]`。

实际量级：ccnm 自己的 `AGENTS.md` 是 3806 个 UTF-16 码元（6306 字节）。外部模式先找 `AGENTS.md`，若把它带进握手，Claude Code 下将丢掉约一半正文加清单和标记行。

建议（需在 ccnm 立阶段后实施）：面向 Claude Code 时按 2048 个 UTF-16 码元而不是 16 KiB 字节做预算，并把清单和标记行放到正文**之前**，正文超出时由 ccnm 自己按行截并写明怎么读全文。
