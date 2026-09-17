# 实施方案 v3：gld / ccnm 的工具面对齐原生能力

日期：2026-09-17。状态：**方案，刚立项**。第 3 节的差距表是实测和读代码得来的事实；第 4 节往后是设计目标，没做完的阶段不代表功能已经存在。产品进度仍由各自仓库记录（ccnm 的 `docs/plan/status.json`、gld 的 RFC）。

承接 [v2 方案](implementation-plan-v2.md)：v2 的结论是三种客户端（Claude Code、Codex CLI、Web AI 经 gld hub）都走"MCP 工具 + 共享库"这一条执行路径。v3 回答下一个问题——**这条路径上的工具，能力要不低于在项目机器上直接跑官方 CLI**。

## 0. 一页纸结论

**用户决定（2026-09-17）**：不引入第三方 harness（pi、claude-code-sdk 的各种 Rust 包装），也不在 Runtime 上登录 AI 账号；但 gld / ccnm 自己的工具要达到原生工具的全部能力——不只是读、改、跑命令、搜索，还包括 **Runtime 上项目自带的 skills**。

"原生能力"不是一件事，是三类，做法完全不同：

| 类别 | 是什么 | 怎么对齐 |
| --- | --- | --- |
| **执行面** | 碰项目文件和进程的：读（文本/图片/PDF/notebook）、写、精确替换、glob、grep、跑命令（含后台、stdin） | 必须在放项目的机器上做，所以只能由 ccnm / gld 的工具补齐 |
| **Agent 面** | 不碰项目文件的：联网搜索、抓网页、子代理、todo、向用户提问、计划模式 | 官方 CLI 本来就有，**不需要重写**——ccnm 现在是用 `--tools ""` 把它们和文件工具一起关掉了，要做的是只关该关的 |
| **项目资产** | 住在项目目录里、原生 CLI 靠"当前目录"发现的：skills、斜杠命令、子代理定义、hooks、项目级 MCP server | CLI 的当前目录不在项目机器上，所以发现不了；要由 Runtime 一侧发现，再经 MCP 交给模型 |

顺序：**先做 skills**（用户点名要的，且现状最弱），再补执行面，最后放开 Agent 面（那一步要真实 CLI 实测，还有一个要用户定的出网问题）。

## 1. 不变的前提

v2 的前提全部保留，这里只重复会限制设计的几条：

- AI 凭据只在 Agent 一侧；Agent 只能是未修改的官方 CLI + 用户订阅。不用 API key、不 fork harness、不自建模型循环。
- **项目里任何"可执行"的东西，永远不在持有 AI 凭据的那台机器上执行。** skill 里的脚本、hooks、项目级 MCP server 都属于这一类。它们要么在 Runtime 上以执行账号的身份跑，要么不跑。
- 冻结的契约只做加法：`ccnm.workspace-mcp/1`（2026-09-11 冻结）明确允许"加工具、加字段、加错误原因"；删工具、改权限语义要升 `/2`。gld 的 compact 工具集是"稳定聚合 API"，同样只加不改。
- 官方 CLI 的参数、工具名、截断长度以**实测版本**为准，不按文档或记忆猜。

## 2. 三类能力的边界

判断一个原生工具属于哪一类，只问一句：**它碰的是哪台机器上的东西？**

| 原生工具 | 碰什么 | 类别 |
| --- | --- | --- |
| Read / Write / Edit / NotebookEdit / Glob / Grep | 项目文件 | 执行面 |
| Bash（含 `run_in_background`）、Monitor、读后台输出、停后台任务 | 项目机器上的进程 | 执行面 |
| LSP（要装插件） | 项目机器上的语言服务器 | 执行面（本轮不做，见第 6 节） |
| WebSearch | 模型厂商的服务端 | Agent 面 |
| WebFetch | 从跑 CLI 的那台机器出网 | Agent 面（**有出网问题**，见第 7 节） |
| Agent（子代理）、TodoWrite、AskUserQuestion、计划模式、ToolSearch | CLI 自己的状态和界面 | Agent 面 |
| Skill | 用户级 skills 在跑 CLI 的机器上；**项目 skills 在项目机器上** | 两边都有 |

Web AI（ChatGPT 等经 gld 接入）的 Agent 面由它自己的产品提供，gld 不管；gld 只对齐执行面和项目资产。

## 3. 差距表（2026-09-17 的事实）

出处：ccnm 的 `docs/protocol/fixtures-mcp/tools-list-coding.json` 和 `crates/ccnm-core/src/mcp/`；gld 的 `crates/core/src/tools/registry.rs`、`agent_context.rs`；官方文档 [tools-reference](https://code.claude.com/docs/en/tools-reference)、[skills](https://code.claude.com/docs/en/skills)、[mcp](https://code.claude.com/docs/en/mcp)。

### 3.1 执行面

| 能力 | 原生 Claude Code | ccnm 七工具 | gld（compact 档） |
| --- | --- | --- | --- |
| 读文本 | `offset`/`limit` | `read_file`：默认 200 行 / 32 KiB，单次上限 64 KiB，带版本号 | `read_file`：默认 32 KiB，上限 1 MiB |
| 读图片 | Read 直接看图 | **没有**（二进制被拒） | `view_image`：PNG/JPEG/GIF/WebP，自动缩放 |
| 读 PDF | Read 按页 | **没有** | **没有** |
| 读 notebook | Read 按 cell 渲染 | 当成 JSON 文本读 | 同左 |
| 新建文件 | Write | `apply_patch` 的 `add`（只能建不存在的文件） | `apply_patch` 的 `Add File` |
| 整文件覆盖 | Write | **没有**（要先 delete 再 add） | 走 patch |
| 精确替换 | Edit + `replace_all` | `apply_patch` 的 `update`：有 `replace_all`，多文件原子提交，带版本校验——这一项比原生强 | `apply_patch` 信封格式 |
| 改 notebook | NotebookEdit | **没有** | **没有** |
| glob | Glob | `list_files` 的 `glob` | `list_files` 的 `patterns` |
| 搜索 | 正则、三种输出模式（内容 / 只列文件 / 计数）、上下文行、跨行匹配、按文件类型过滤 | `search_text`：正则开关、glob、大小写、上下文行。**没有**只列文件 / 计数 / 跨行 / 类型过滤；dotfile 永远不搜 | 同类，多 include/exclude globs |
| 跑命令 | Bash：一行 shell，默认 2 分钟、上限 10 分钟，工作目录跨调用保持 | `exec_command`：只收 argv（要管道得自己写 `sh -c`），默认 120 秒、上限 600 秒，`cwd` 每次传 | `exec_command`：收一行命令，默认 30 秒、上限 600 秒 |
| 后台进程 | `run_in_background` + 读输出 + 停 | **没有**（文档里列为"延后"） | **有**：超过 `yield_time_ms` 自动转后台，`write_stdin`、`kill_session`、`read_output` |
| 给进程喂 stdin | 没有 | 没有 | 有（`write_stdin`、`tty`） |
| Git | 走 Bash | 走 `exec_command` | 五个只读 git 工具 |

### 3.2 Agent 面（只涉及 ccnm 的受管会话）

ccnm 用 `--tools ""` 启动 Claude Code，实测结果是会话里**只剩**七个 `mcp__ccnm__*`：没有 WebSearch、WebFetch、子代理、TodoWrite、AskUserQuestion、Skill（`crates/ccnm-core/src/provider/claude/mod.rs` 的 `launch_cmd` 注释）。Codex 一侧 `web_search="disabled"`，并用 `--disable` 关掉了十几个特性，其中属于 Agent 面的有 `multi_agent`、`skill_search`、`goals`、`memories`，属于执行面的有 `shell_tool`、`unified_exec`、`view_image`（`crates/ccnm-core/src/provider/codex/mod.rs` 的 `DISABLED`）。

用户自己的 CLI 经 `ccnm mcp bridge` 接入时不受影响——那种用法下 CLI 的内置工具都在，只是文件类工具碰的是本机而不是远端项目。

### 3.3 项目资产

| 资产 | 原生怎么发现 | ccnm | gld |
| --- | --- | --- | --- |
| 根目录 `CLAUDE.md` / `AGENTS.md` | 当前目录 | 已经投影进 MCP 握手的 `instructions`（上限 2048 个 UTF-16 码元，实测） | 注入 `instructions`，compact 档只留一份，**没有大小上限** |
| 嵌套 `CLAUDE.md`、`.claude/rules/` | 当前目录 | 只点名路径，模型自己 `read_file` | 按 provider 收集 |
| **skills** | `.claude/skills/<名>/SKILL.md`；`description` 常驻上下文（截到 1536 字符），正文在调用时才加载；用户可以敲 `/名字` | **只在握手里点了 `SKILL.md` 的路径**，没有 name / description，模型不知道什么时候该用；和规则文件共用 40 个名额、768 码元预算；**Codex 会话完全没有** | 有目录 + `list_skills` / `get_skill`，但**默认的 compact 档把它们全砍了**；frontmatter 解析只认单行 `key: value`，`description: >` 这种多行写法会被读成一个 `>`；`~/.claude/skills` 下的附件在默认读限制下读不到 |
| 斜杠命令 `.claude/commands/*.md` | 当前目录（官方文档：和 skill 是同一套机制） | 没有 | 没有 |
| 子代理定义 `.claude/agents/*.md` | 当前目录 | 没有 | 不适用 |
| hooks | settings | **按设计不带**：它们会在持凭据的机器上执行 | 没有 |
| 项目级 MCP server（`.mcp.json`） | 当前目录 | **按设计不带**（`--strict-mcp-config`） | 没有 |

gld hub 接 ccnm 远端成员时，透传的是一份**静态白名单**（`remote_*` 七个加 `remote_coding_begin/end`），ccnm 新增工具不会自动出现，要 gld 逐个评审加入。

## 4. 做法

### 4.1 项目 skills（第一批）

**在 Runtime 上发现，经 MCP 交给模型；skill 里的东西在 Runtime 上跑。**

交给模型有三条通道，哪条在哪个 Host 上真的通，2026-09-17 零额度实测过（[原始记录](../../evidence/v3-parity/skills-surface/README.md)）：

| 通道 | Claude Code 2.1.273 | Codex 0.154.0 | 结论 |
| --- | --- | --- | --- |
| 目录放进一个工具的 description | 通；每个工具的 description 单独截到 2048 个 UTF-16 码元 | 通；不截断 | **现在的主通道**，三种客户端都能用 |
| MCP `prompts` | 通；变成 `/mcp__<server>__<名字>`，参数按空白切分 | **不通**：Codex 连上之后只调 `tools/list` | 只给 Claude Code 的用户手动调用用 |
| MCP 官方 skills 扩展（[SEP-2640](https://github.com/modelcontextprotocol/ext-skills)，2026-09-13 定稿：`skills/list`、`skills/get`、`skill://` 资源） | 客户端代码已经在 CLI 里，skill 会以 `<server>:<skill>` 出现在原生 Skill 机制中；但挂在特性开关 `tengu_mcp_skills` 后面，**默认关**，实测没有调 `skills/list` | 不通 | **标准路径，第二步做**：实现成本不高，开关打开的那天自动生效 |

所以先做工具通道（加 prompts），紧接着一个阶段实现标准扩展。下面几条对两条通道都成立：

- **发现**：Runtime 上的 `mcp-serve` 扫项目里的 `.claude/skills/*/SKILL.md`、`.claude/commands/**/*.md`，以及 Codex / gld 已经在用的 `.agents/skills/*/SKILL.md`。个数、单个大小都设上限，排序确定——同一个项目每次握手必须一样。
- **目录**（name + description）放进一个新工具的 **description**，而不是 `instructions`：`instructions` 总共只有 2048 码元，还要装 `CLAUDE.md`；工具 description 有自己独立的 2048 码元。放不下的部分，调用这个工具不带名字就返回完整目录。
- **加载**：工具带名字调用，返回 SKILL.md 正文。按官方语义替换 `$ARGUMENTS` / `$N` / `$name`；`${CLAUDE_SKILL_DIR}` 换成 skill 目录的**工作区相对路径**，模型用 `read_file` 读附件、用 `exec_command` 跑脚本——脚本因此天然在 Runtime 上、以执行账号的身份、受同一套写互斥和沙箱约束执行。
- **`` !`命令` `` 注入不自动执行。** 原生是加载 skill 时由 Bash 工具先跑一遍、把输出填进去。ccnm 如果照做，等于模型一次"读"调用触发了项目指定的命令，绕过了 `exec_command` 上的人工确认（`allow_unattended_exec` 那一层）。第一版原样保留这些行，并在正文开头列出来，模型需要就自己用 `exec_command` 跑。官方对从 claude.ai 同步来的 skill 也是不执行注入命令的，这不是没有先例。
- **用户手动调用**：把可由用户调用的 skill 和命令再登记成 MCP `prompts`，在 Claude Code 里就是 `/mcp__ccnm__<名字> 参数…`。
- **标准扩展那一步要多一个依赖**：SEP-2640 要求给 skill 的每个文件报 SHA-256 和字节数，ccnm 的依赖树里现在没有 SHA-256 实现。toexec 的 crate 守着零依赖，所以算摘要的代码放在产品里，不进共享库。
- **忽略并写明的 frontmatter**：`allowed-tools`（ccnm 改不了 Host 的权限）、`context: fork` / `agent` / `model` / `effort`（要 Agent 面放开之后才有意义）、`hooks`（按第 1 节不带）。
- `read` 模式也能列和读 skill（只读）；外部 bridge、Claude 受管、Codex 受管三种入口共用同一个实现，所以 Codex 会话也第一次有了项目 skills。

**共享库**：frontmatter 解析和目录渲染是纯机制，gld 已经有一份（而且有上面说的多行缺陷），ccnm 要写第二份——符合进 toexec 的条件。新 crate `toexec-skill`：零依赖，手写一个够用的 frontmatter 读取器（单行值、引号、`>` / `|` 块标量、简单列表）。发现路径、上限、怎么交给模型仍由各产品自己定。

**gld 一侧**：compact 档把 skill 目录和读取放回来（加法）；换用 `toexec-skill` 修掉多行 description；允许在已发现的 skill 目录内只读，让用户级 skill 的附件读得到；hub 白名单加入 ccnm 的 skill 工具。

### 4.2 执行面补齐

全部是对现有工具加可选参数或加新工具，不改已有语义：

1. **搜索模式**：`search_text` 加输出模式（内容 / 只列文件 / 计数）、跨行匹配、文件类型过滤、可选包含 dotfile。底下本来就是 `rg`，都是现成开关。
2. **整文件覆盖**：`apply_patch` 加一种操作，覆盖已有文件时必须带 `read_file` 给的版本号。
3. **一行 shell**：`exec_command` 加一个和 `cmd` 二选一的参数，省得模型自己拼 argv。不因此改任何权限判断——现在模型本来就能写 `["sh","-c",…]`。

1–3 已在 ccnm P37 做完（2026-09-17），和上面写的有三处不同，gld 同步时照实现走：执行用的是 `bash -c` 而不是 `sh -c`，没有 bash 就报错、不退回 sh（Debian 的 sh 是 dash，模型写的是 bash 方言）；调用方的 glob 不再作为 rg 的 `--glob`（rg 里 glob 一命中就不看 `.gitignore`，文件级的也一样），改成文件名部分交给 `--type-add` 缩小范围、整条 glob 由 ccnm 按 rg 规则过滤，`type` 和 `glob` 同给取交集（ccnm P38）；覆盖操作叫 `write`，只替换已存在的文件。依据在 ccnm 的 `docs/research/p37-execution-surface-batch1-2026-09-17.md` 和 `p38-glob-gitignore-2026-09-17.md`。

第 4 项（图片）已在 ccnm P39 做完：工具叫 `view_image`，只发 MCP `image` 块、不缩放、只认 PNG / JPEG / GIF / WebP、上限 3932160 字节。依据是 [media-surface](../../evidence/v3-parity/media-surface/README.md) 的零额度实测——`resource` blob 在 Claude Code 里会被写到 Agent 机器的磁盘上、在 Codex 里变成 base64 文本，所以第 6 项的 PDF 也不能用 blob 发；gld 已有的 `view_image` 如果返回的是别的块形状，同步时要对照这份结果。

第 5 项（notebook）已在 ccnm P40 做完，和上面写的不同：**没有改 `read_file`**（它返回 JSON 文本是冻结契约里的行为，已有人照着那份文本改 notebook），而是新增只读工具 `read_notebook`，`apply_patch` 加 `edit_notebook`（字段照 Claude Code 的 NotebookEdit）。写回用 nbformat 的写法，已用 nbformat 5.11.1 核对。gld 同步时照这个形状。第 6 项（PDF）要 Runtime 上有 poppler，ccnm 开发机没装；**用户 2026-09-18 定暂时不做**，第 3 步到此为止，接第 4 步。

第 7 项（后台进程）已在 ccnm P41 做完，形状和下面第 7 条写的不同：没有照 gld 的 `yield_time_ms` / `write_stdin`，而是 `exec_command` 加 `run_in_background`（名字照 Claude Code，马上返回 `output_ref`，不给 `timeout_ms` 就没有期限）、`read_output` 加 `wait_ms`（等命令结束，结束即返回，上限 600000）、新工具 `stop_command`（进程组先 TERM、2 秒后 KILL）；同一个 server 进程最多 8 个；连接结束时先停掉所有命令再放写锁，客户端取消调用也停掉命令（这两条修的是 ccnm 原有缺陷）。不做 stdin / tty。依据是 [background-exec](../../evidence/v3-parity/background-exec/README.md) 的零额度实测：MCP 没有让 server 叫醒模型的可用通道，所以"等"必须是模型主动调的阻塞工具；Claude Code 交互会话里 MCP 调用超过 120 秒会被 Host 自己转后台。gld 同步时对照 ccnm 的 `docs/research/p41-background-commands-2026-09-18.md` 决定是改形状还是只在 hub 白名单里映射。
4. **图片**：加 `view_image`（名字和 gld、Codex 的一致），返回 MCP 图片内容块。受 Claude Code 对 MCP 输出的上限约束（默认 25,000 token），上限和要不要缩放实测后定。
5. **notebook**：`read_file` 遇到 `.ipynb` 按 cell 渲染成带编号的文本；`apply_patch` 加按 cell 改的操作。纯 JSON，不加依赖。
6. **PDF**：MCP 内容块里没有"文档"这一种，只能转成文本。做法和 `search_text` 依赖 `rg` 一样：Runtime 上有 `pdftotext` 就用，没有就报一个说清楚该装什么的错。
7. **后台进程**：`exec_command` 加后台运行，返回句柄；`read_output` 能读还在跑的命令；加停止；（可选）喂 stdin。这是最大的一块——要和会话结束时的清场、保留输出的总量上限（ccnm P31）、`exec_sandbox`（P33）、超时语义一起设计。gld 已有的 `yield_time_ms` / `write_stdin` / `kill_session` 是现成参照。

每做完一个，gld hub 白名单跟着评审加入；gld 自己缺的（PDF、notebook、搜索模式）同步补。

### 4.3 Agent 面放开（ccnm 受管会话）

把 `--tools ""` 换成一份**允许清单**（官方文档：`--tools` 给的是可用工具集合），只放不碰本机文件和进程的那些；settings 里对文件 / 命令类工具的 deny 继续留着当第二道锁。用允许清单而不是拒绝清单，是为了 CLI 以后新增的内置工具默认仍然是关的。

开工前必须在**登录着的真实 CLI** 上实测的事：

- 当前版本里这些工具到底叫什么（子代理是 `Agent` 还是 `Task`，todo 是 `TodoWrite` 还是 `Task*` 一族）。
- 子代理继承到的工具是不是只有 `mcp__ccnm__*`；几个子代理同时调 `apply_patch`，ccnm 这边的串行化是否正确。
- ToolSearch 回来之后，ccnm 的七个工具会不会被延迟加载（外部入口遇到过，靠 `alwaysLoad` 解决）。
- 计划模式是否按 MCP 工具的 `readOnlyHint` 放行只读工具——ccnm 的 annotations 本来就是准的。
- 内置的 Skill 工具回来之后，Agent 机器上用户级 skill 的正文能加载，但它的附件和脚本模型够不着（本机的 Read / Bash 是关的）。这一条是已知限制，写进文档，不是缺陷。

Codex 一侧对应的是放开 `web_search`（在厂商服务端执行）；`multi_agent`、`skill_search` 放不放开，同样实测之后定；本地 `view_image` 和三个执行类特性保持关闭，由 ccnm 的远端工具顶替。

### 4.4 其余项目资产

- **斜杠命令**：并入 4.1，同一套发现和 `prompts`。
- **子代理定义**：依赖 4.3。Runtime 上读 `.claude/agents/*.md`，会话启动时经 CLI 的 `--agents` 参数传入（参数形状开工时实测）；其中的 `hooks`、`mcpServers` 字段剥掉。排在 4.3 之后。
- **hooks**、**项目级 MCP server**：要在 Runtime 一侧重新实现一套执行和代理，语义和原生对不齐的地方很多。**本方案不做**，只在这里记下为什么。真有项目依赖它们，再单独立项。

## 5. 阶段

| 顺序 | 内容 | 在哪做 | 额度 |
| --- | --- | --- | --- |
| 1 | 项目 skills + 斜杠命令（4.1）；`toexec-skill` crate | ccnm P36、toexec | 0 |
| 2 | 搜索模式、整文件覆盖、一行 shell（4.2 的 1–3） | ccnm | 0 |
| 3 | 图片、notebook、PDF（4.2 的 4–6） | ccnm，gld 补 PDF / notebook | 0 |
| 4 | 后台进程（4.2 的 7） | ccnm | 0 |
| 5 | gld：compact 放回 skills、换 `toexec-skill`、hub 白名单加入 1–4 的新工具 | gld | 0 |
| 6 | Agent 面放开（4.3） | ccnm | 要真实 CLI；少量模型运行 |
| 7 | 对照实验：同一批任务，ccnm 路径对比"在 Runtime 上直接跑官方 CLI" | fodelf / 用户终端 | 20–30 次 |

阶段编号在各仓库开工时才登记（ccnm 的并行会话经常撞号，提前占号没有意义）。每个阶段照 ccnm 的惯例：先实测、再定契约、再实现；契约新增的部分同步进 `docs/protocol/` 的 fixture 和 schema。

## 6. 怎么算"达到了"

- **逐项**：第 3 节每一行"没有"变成"有"，并且有一个不 import 产品代码的中立 MCP 客户端测试证明（ccnm 冻结契约时用的同一个办法）。
- **整体**：第 5 节第 7 步的对照实验。比的是任务成功率、回合数、token，不是延迟——V2-P1 量过，RPC 和 ssh 那一跳相对模型一回合的秒级耗时可以忽略。模型额度记账仍以 v2 方案第 10.1 节为准。
- **明确不在"达到"范围内的**：LSP（原生也要装插件才有）、hooks、项目级 MCP server、Windows（另立 RFC，见 v2 第 0.1 节）。

零额度的测量办法（v2 期间积累的，继续用）：Codex 用本机假 Responses 服务抓它实际发出的请求；Claude Code 先试同样的办法（`ANTHROPIC_BASE_URL` 指向本机假端点），不行再退回 V2-Q1 用过的"读 CLI 静态代码 + 真实连接的 debug 日志"。

## 7. 要用户定的事

都不挡第 1–5 步，轮到第 6 步之前定即可：

1. **WebFetch 放不放开。已定（2026-09-17，用户）：WebSearch 默认放开，WebFetch 做成 opt-in。** 理由：WebFetch 是从跑 CLI 的机器——也就是持有 AI 凭据的那台——直接出网抓网页，能碰到那台机器的内网；WebSearch 在厂商服务端执行，没有这个问题。同时定了顺序：先把第 5 节第 2–4 步的执行面补齐，再做第 6 步。
2. **其余 Agent 面（子代理、todo、提问、计划模式等）放开是默认行为还是按 workspace opt-in。** WebSearch 已按上一条默认放开，不在此列。它改变的是受管会话一直以来"只有七个工具"的承诺。建议：opt-in 一个版本，对照实验没问题后再改默认。
3. **`` !`命令` `` 注入**：第一版不自动执行（4.1）。如果实际项目的 skill 大量依赖它，再讨论加一个和 `allow_unattended_exec` 同级的开关。
