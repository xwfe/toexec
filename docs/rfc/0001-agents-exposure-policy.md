# RFC-0001：用 `~/.agents/mcp.json` 细粒度管 gld / ccnm 暴露的工具和 skills

状态：已接受（2026-09-22，范围由用户选定）\
Owner：xwfe\
关联：gld、ccnm；共享解析在本仓 `toexec-agents`

## 怎么用（先看这个）

在跑 gld 的机器上、或 ccnm 的 Runtime 上（执行账号的 HOME 下）写 `~/.agents/mcp.json`：

```json
{
  "mcpServers": {
    "gld": {
      "disabledTools": ["exec_command"],
      "skillOverrides": { "deploy": "user-invocable-only" }
    },
    "ccnm": {
      "enabledTools": ["workspace_info", "read_file", "list_files", "search_text", "load_skill"]
    }
  },
  "skillOverrides": {
    "cheat-pass": "off",
    "microsoft-foundry": "off",
    "vue-best-practices": "name-only"
  }
}
```

- `mcpServers.gld` 只管 gld，`mcpServers.ccnm` 只管 ccnm。条目名固定是产品名，和你在客户端配置里给它起的名字无关。
- `enabledTools`：只给这几个工具（白名单）；`disabledTools`：这几个不给。两个都写时先按白名单取、再去掉黑名单——和 Codex 的 `enabled_tools` / `disabled_tools` 一个意思。
- `skillOverrides`：按 skill 名字给一档，和 Claude Code 设置里的 `skillOverrides` 同一套四档。写在顶层对所有产品生效；写在某个产品的条目里，对这个产品覆盖顶层。

| 档 | 模型看得到 | 模型能自己加载 | 用户点名能用 |
| --- | --- | --- | --- |
| `on`（不写就是） | 名字 + 描述 | 能 | 能 |
| `name-only` | 只有名字，省上下文 | 能 | 能 |
| `user-invocable-only` | 看不到 | 不能 | 能（和 frontmatter 写 `disable-model-invocation: true` 一样） |
| `off` | 看不到 | 不能 | 不能，按名字找也说"没有这个 skill" |

**只能收窄，不能放宽**：产品自己的设置（gld 的 `tool-profile`、ccnm 的 `external_mcp = "read"`、skill 自己 frontmatter 里的 `disable-model-invocation`）先决定上限，这个文件只能在上限里再关掉一些。写 `"on"` 不会把一个 frontmatter 说只给用户的 skill 放给模型。

**改完就生效**：gld 每次列工具、调工具都重读一遍（文件只有几百字节），不用重启；ccnm 每个会话开始时读一次，下一个会话生效。已经连着的客户端可能缓存了旧的工具表，但被关掉的工具调了也会被拒。

**写错会怎样**：

- 文件不存在：什么都不收窄，和以前一样。
- JSON 写坏了、字段类型不对、档位拼错（比如 `"hidden"`）：**拒绝服务并指出哪一处**。gld 一个工具都不给，`tools/list` 以错误返回原因（形如 `~/.agents/mcp.json: mcpServers.gld.disabledTools: expected an array of tool names`）；ccnm 会话打不开，报 `CCNM_E_CONFIG`。这时退回"什么都不收窄"等于把你想关的又打开了，所以宁可停。
- 工具名写错（`"exec_comand"`）：服务照常，`gld doctor` / `ccnm doctor` 的"暴露规则"一行报失败，ccnm 的 `workspace_info` 也会列出来。工具名随版本增减，旧名字让整个服务停掉代价太大；但写错的那个工具**照样暴露着**，所以要当失败报。
- `mcpServers` 里别的条目（比如 `context7`）的 `command` / `url`：这一版不读、不启动。条目本身必须是对象（`{...}`），否则按写坏处理。

## 背景与动机

`~/.agents/skills` 已经是约定：Codex 的用户级 skills 在这里，vercel-labs 的 `skills` CLI 把 skill 装到这里再分发给各家 Agent（作者机器上 66 个，35 个软链进了 `~/.claude/skills`）。装得多了，每个会话的 skill 目录里就有一堆和当前工作无关的，占上下文，还可能被误用。

gld 现在只能按来源整批开关 skills（`skill-sources`），工具只有 `tool-profile` 这种粗粒度的档；ccnm 的 11 个工具和项目 skills 没有任何开关。用户要的是**一个地方、按单个工具和单个 skill** 管这些。

`~/.agents/mcp.json` 不是现成标准（2026-09-22 查过：各家各用各的路径，`~/.cursor/mcp.json`、`~/.copilot/mcp-config.json`、项目里的 `.mcp.json`），本机也不存在，所以格式由我们定。定的原则是**不自造语义**：外壳是大家都用的 `{"mcpServers": {...}}`，工具开关照 Codex，skill 四档照 Claude Code 2.1.278 的 `skillOverrides`（它的原文："Per-skill listing overrides keyed by skill name. "name-only" lists the skill without its description; "user-invocable-only" hides it from the model but keeps /name; "off" hides it from both. Absent = on."）。

## 目标

- gld、ccnm 读同一个文件，同一套语义，由 `toexec-agents` 一处实现。
- 工具按名字开关，skill 按名字四档；全局和按产品两层。
- 被关的工具不只是不列出来，调用也拒绝。
- 坏配置不静默放行。

## 非目标

这次用户明确只选了第一层（2026-09-22），下面两层**不做**：

- gld 读 `mcpServers` 里别的 server、代为启动并转发（聚合网关）。
- ccnm 受管会话接入别的 MCP server。它会松动"项目只能经 ccnm 碰到"的保证，要另立项并由 Runtime 逐个授权。

也不做：按项目的规则（见"未来可能"）、按客户端区分（gld 现在一把凭据进全部项目，分不出是谁在调）、ccnm 新增发现 `~/.agents/skills`（见"未解决问题"）。

## 方案设计

**共享解析 `toexec-agents` 0.1.0**：`parse(text)` / `load(path)` 读出策略；`for_server("gld")` 给出这个产品的视图，提供 `tool_allowed(name)`、`skill_level(name)`、`unknown_tools(known)`。报错只给分类和位置（JSON 路径，或语法错误的行列），措辞由产品定——和 `toexec-skill` 同一个做法。依赖只有 `serde_json`：两个产品本来就链接同一版本，手写 JSON 解析器反而多一份要维护的代码。

**文件位置**：`$HOME/.agents/mcp.json`，没有别的环境变量。ccnm 的工具跑在 Runtime 上、以执行账号的身份，所以读的是**执行账号**的 HOME——专用执行账号（比如 `ccrun`）要在它自己的 HOME 下写。

**gld**：hub 的 `tools/list`、`tools/call`、`workspace_context`、`gld tool list` 都过同一个判断；skill 目录、`list_skills`、`get_skill` 按档处理。`user-invocable-only` 沿用 gld 对 `disable-model-invocation` 的做法（不进目录，点名时照样能 `get_skill`，正文前注明）。不缓存，每次用到都重读。

**ccnm**：`internal mcp-serve` 打开会话时读一次，和 `external_mcp` 的读写模式叠加：工具不列、调用拒；`load_skill` 的目录、`load_skill` 本身、MCP prompts 按档处理。`user-invocable-only` 沿用 ccnm 对 `disable-model-invocation` 的做法（模型调用 `load_skill` 被拒，prompt 可用）。受管会话 Agent 一侧的放行清单不用改：Runtime 没列的工具，放行了也调不到。

## 缺点

- 多了一个要找的地方：工具没了，原因可能在 `tool-profile`、`external_mcp`，也可能在这个文件。所以两边的 doctor 都有一行"暴露规则"列出这个文件关掉的工具，按名字硬调被关的工具时也会指出是哪条规则；`gld tool list` 只是不列它们，不单独说明。
- 条目名固定成 `gld` / `ccnm`：如果你的客户端配置里恰好也用这个名字定义了别的东西，这里会被当成 gld / ccnm 的规则。
- 工具名写错时不停服务，写错的工具照样暴露——只能靠 doctor 提醒。

## 备选方案

- **每个产品各自一个配置项**（gld 设置里加 `disabledTools`，ccnm 的 config.toml 加一行）：用户要在两处用两种格式写同一件事，也和 `~/.agents/skills` 这个跨工具的位置脱节。不选。
- **规则放在单独的顶层键**（比如 `"exposure": {...}`）而不是 `mcpServers.<产品>`：能对自己的键做严格校验，但以后做聚合（第二层）时，别的 server 的工具开关必然写在 `mcpServers.<名字>` 里，两套写法并存更乱。不选。
- **坏配置时退回不收窄**：服务不会停，但把用户想关的工具又打开了，而且用户多半不会发现。不选。
- **工具名写错也停服务**：最安全，但升级后一个被改名的旧工具名就能让整个服务起不来。不选，交给 doctor。

## 已有实践

- Codex：`[mcp_servers.<name>] enabled_tools / disabled_tools`（ccnm 受管会话一直在用 `enabled_tools`）；`[[skills.config]]` 按 `name` 或 `path` 选中单个 skill 开关。本文的工具开关语义直接照搬。
- Claude Code 2.1.278：`skillOverrides` 四档（上面引了原文）；MCP 按单个工具管要写 permissions 的 `mcp__<server>__<tool>`；另有 `allowedMcpServers` / `deniedMcpServers` 管整个 server。四档照搬，名字也照搬。
- [1mcp-app/agent](https://github.com/1mcp-app/agent)：把多个 MCP server 聚合成一个并按标签过滤——是第二层的参照，这次不做。

## 影响分析

- 性能：gld 每次列工具、调工具多读几次一个几百字节的文件；ccnm 每个会话读一次。
- 安全：只收窄。坏配置拒绝服务而不是放行。ccnm 读的是执行账号 HOME 下的文件，执行账号本身能改它——和它能改自己的 `~/.config/ccnm/config.toml` 是同一个信任面，不比现在更差；但也意味着这个文件**不是**对模型的约束：模型经 `exec_command` 能改它，下一个会话就生效。要约束模型，用 ccnm 的 `external_mcp` / 执行门或 gld 的 `tool-profile`。
- 兼容性：文件不存在时行为和以前逐字节相同。ccnm 冻结的 Remote Workspace MCP 契约里的工具表是"不收窄时"的样子，收窄等同于 `read` 模式那种按 Runtime 配置少给工具，协议文档注明。
- 运维：doctor 各加一项。
- 开发体验：gld 的集成测试要把 HOME 指到临时目录，否则会读到开发者真实的 `~/.agents/mcp.json`。

## 未解决问题

- **ccnm 要不要也发现 `~/.agents/skills`**（Runtime 上执行账号的）。原生 CLI 会读用户级 skills，但 ccnm 的 skill 目录挤在 2048 个 UTF-16 码元的工具描述里，66 个用户级 skill 放进去会把项目自己的挤掉；专用执行账号的 HOME 下通常也没有个人 skills。这一版不做，等真有需要再定默认值和排序。

## 未来可能

- 项目里的 `.agents/mcp.json`：只允许**再收窄**（项目文件模型能写，不能让它放开用户关掉的东西）。
- 第二、三层：`mcpServers` 里其他条目的定义已经在同一个文件里，开关语义（`enabledTools` / `disabledTools`）不用改。

## 落地记录（2026-09-22）

| 仓库 | 提交 | 内容 |
| --- | --- | --- |
| toexec | `3c1b478`（tag `toexec-agents-v0.1.0`） | 共享解析，8 条单元测试 |
| ccnm | `b5915b9`（P47） | 工具在 `call_tool` 总入口拦、skills 四档作用在目录 / 列表 / `load_skill` / prompts；`workspace_info` 列出关掉的和写错的；doctor 新行"暴露规则"（敲命令的不是执行账号时跳过并说该去哪看）；7 条新测试，其中 2 条经真实二进制 |
| gld | `3bd2384`、`572f04c` | 收在 `exposed_tool_names` 一处，hub 自有工具另过一遍；skills 收在 `current_skill_scan`；doctor 新行；CLI 测试给守护进程设临时 HOME；6 条新测试，其中 2 条经真实服务（不重启就生效、写坏时 `tools/list` 报原因） |

**没验的**：真实 AI 客户端连上来之后的表现（缓存了旧工具表的客户端调被关的工具，只有服务端的拒绝兜底，客户端怎么显示没看）；Linux 上没跑。
