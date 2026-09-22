# 实施方案 v4：gld / ccnm 用上两台机器上已经装好的 skills 和 MCP server

日期：2026-09-22。状态：**四步都做完**（skills；gld 转本机 MCP server；ccnm 转项目那台机器上的 MCP server；ccnm 转 Agent 机器上的 MCP server）。产品进度仍由各自仓库记录（ccnm 的 `docs/plan/status.json`、gld 的 RFC）；这里只放跨仓的决定和顺序。

承接 [v3 方案](implementation-plan-v3-native-parity.md)：v3 让 gld / ccnm 自己的工具对齐原生能力，外加**项目里**的 skills。v4 回答下一个问题——**用户已经在两台机器上装好的 skills 和 MCP server，怎么也用上**。

## 0. 用户的话和决定

2026-09-22，用户（原话）："是 gld 或 ccnm 都可以使用 agent 和 runtime 机器上的已经安装的 skills 和 mcp，而不是工具自身的 mcp 工具"。随后："最好为 gld 完善一套合理地 mcp/skills 工具来专门操作它们；ccnm 也是，但是默认全开。runtime 传递过程是否会丢信息，数据量是否过大等问题；代码模块化，解耦，如果后期这块功能不需要，能简单的删除。抽取共用模块进 toexec。按建议顺序做，先从第 1 步开始"。

同一天更早做过一版"用 `~/.agents/mcp.json` 按名字收窄 gld / ccnm 自己的工具"，是理解错了需求，已整个撤销（toexec `b6c5e36`、ccnm `37694f6`、gld `8180107`）。**v4 的方向是把装好的东西接进来，不是收窄自己的。**

## 1. 四步，按这个顺序

| 步 | 做什么 | 为什么排这里 | 状态 |
| --- | --- | --- | --- |
| 1 | 两台机器上装好的 skills | 只读、风险最低，用户点名先做 | **做完**（ccnm P48，gld 同步） |
| 2 | gld 聚合本机装好的 MCP server，经它一个入口交给 ChatGPT 这类 Web AI | Web AI 用上本机 MCP 的唯一办法 | **做完**（gld RFC-0006） |
| 3 | ccnm 在 Runtime 上代理 MCP server（含项目自带的 `.mcp.json`） | 数据库这类只能在项目旁边跑的 server | **做完**（ccnm P49，gld hub 同步；共用代码成了 `toexec-mcp` 0.1.0） |
| 4 | ccnm 会话接 Agent 上装好的 MCP server | 风险最大：Filesystem、desktop-commander 这类一接进来就绕过"项目只能经 Runtime 碰到"的保证 | **做完**（ccnm P50；`toexec-mcp` 0.2.0 加了 `kept`、`sse`，gld 同步改用） |

"默认"按产品分：**ccnm 默认全开**（用户定）；gld 可能挂在公网隧道上，skills 维持 gld 现有的默认，第 2 步的 MCP 转发**默认一个都不开、按名字开**（原来写的"默认只放网络类"实测做不到，见 3.3）。

"已安装"的清单直接读各家现成的配置，不让用户再写一份：skills 读 `~/.claude/skills`、`~/.agents/skills`、`~/.codex/skills`、`~/.claude/commands`；MCP server（第 2–4 步）读 `~/.claude.json` 的 user 级 `mcpServers` 和 `~/.codex/config.toml` 的 `mcp_servers`。另外只需要一份"放行 / 藏哪些"的清单。

## 2. 第 1 步：skills

### 2.1 实测决定了做法

零额度实测在 [`evidence/v3-parity/machine-skills/`](../../evidence/v3-parity/machine-skills/README.md)（Claude Code 2.1.278、Codex 0.154.0）：

- Claude 原生的 `Skill` 正文拿得到，附件要 `Read`；只对 skills 目录放开的 `Read` 仍会不经允许读到会话的工作目录（ccnm 的状态目录）。
- Codex 在 ccnm 的远端会话里**已经**列出 Agent 上的 skills，却要靠被 ccnm 关掉的 shell 去读——清单是误导；`-c skills.include_instructions=false` 能整段关掉。
- 同名时原生是用户目录的赢。

所以 **Agent 上装的 skills 由 ccnm 自己读出来交给模型，原生的一律关着**。

### 2.2 各仓做了什么

| 仓 | 做了什么 |
| --- | --- |
| toexec | `toexec-skill` 0.3.0 加 `dir` 模块：列 skill 目录里的文件、只在这个目录里读一个（跟符号链接、不出目录、不碰点文件、限大小、只收 UTF-8）。gld 的 `get_skill` 先有这套规则，ccnm 的 `load_skill` 也要，所以抽出来 |
| ccnm（P48） | Runtime：`load_skill` 并进执行账号 HOME 里装的，项目的排前面，同名时装好的赢。Agent：`ccnm internal agent-skills`，Claude Code / Codex 在 Agent 上直接起的只读 MCP server。`load_skill` 加 `file` / `line`。每台机器自己的 `[machine_skills]`（`enabled` 默认 true、`hidden`），线上格式不变。记录：ccnm `docs/research/p48-machine-skills-2026-09-22.md` |
| gld | 本机装的 skills 本来就读；修了两处：主目录里链到别处的 skill（`~/.claude/skills/x -> ~/code/skills/x`）以前扫不到，现在找得到、附件读链接指向的目录；`get_skill` 改用共享的 `dir`。hub 转发远端 ccnm 的 `remote_load_skill` 透传 `file` / `line`。新增 `gld cfg runtime --hidden-skills` 按名字藏本机装的。记录：gld `docs/rfc/0005-machine-skills.md` |

### 2.3 会不会丢信息、数据量大不大

- **Agent 上的 skills 不过 Runtime 链路**：在 Agent 上读、交给同一台机器上的 Claude / Codex。过 ssh 的只有 Runtime 上的，按需：目录一次，正文和附件每次调用一份。
- 目录受 Claude Code 每个工具说明 2048 个 UTF-16 码元的限制。开发机装了 97 个 skill，名字都放不下，**原先只剩一句"装了 97 个"**；现在先去描述、再列放得下的名字（84 个），项目的永远排前面，不带名字调一次拿全表（33.9 KB）。
- 正文和附件一次最多 64 KiB（ccnm）/ 整个文件最多 256 KiB（gld），长的分段、写明从哪接着读；超过 1 MiB 不读。
- 故意不交的：点文件、链接到 skill 目录外的文件、非 UTF-8 文件（Agent 上的二进制附件没有搬运通道）。

### 2.4 不要了怎么删

各仓记录里有逐项清单。toexec 这边是 `toexec-skill` 的 `dir` 模块——gld 的 `get_skill` 也在用，删之前把它挪回 gld。

## 3. 第 2 步：gld 转本机 MCP server

实测在 [`evidence/v4-mcp/machine-mcp/`](../../evidence/v4-mcp/machine-mcp/README.md)，gld 的决定和删除清单在 gld [RFC-0006](https://github.com/xwfe/gld/blob/main/docs/rfc/0006-machine-mcp.md)。这里只记跨仓要知道的。

### 3.1 做法

- 读 `~/.claude.json` 的 `mcpServers` 和 `$CODEX_HOME/config.toml` 的 `[mcp_servers.*]`，同名时 Claude 的那份生效；gld 自己只存一份"开了哪几个"（`gld mcp on/off`）。
- 模型看到三个服务级工具：`list_mcp_tools`、`call_mcp_tool`、`read_mcp_result`，不把每个 server 的工具平铺进工具表（开发机上 playwright 一家 21 KB，而 ChatGPT 只在连上时读一次工具表）。
- 连接按"server + 调用方"分、用到才开、闲 5 分钟收；stdio 的起在自己的进程组里，关的时候连子进程一起收。

### 3.2 会不会丢信息、数据量大不大

deepwiki 的 `read_wiki_contents` 一次 839 KB（407 KB 正文 + 一份内容相同的 `structuredContent`）。gld：有文字时不带那份重复的；文字一次交 64 KiB，剩下的留 10 分钟，`read_mcp_result` 接着读——实测三次读全 406,840 字节。单条超过 16 MiB 的后半截、单个超过 5 MiB 的图片会丢，都在结果里写明。

### 3.3 默认为什么不是"只放网络类"

context7 在开发机上是 `npx` 起的本机进程，配置里和 Filesystem 长得一样，从配置分不出谁只走网络。所以 gld 默认全关、按名字开；ccnm 第 4 步要分"网络类"时会撞上同一个问题，到时要么也按名字，要么只把远端 URL 那一类算网络类。

### 3.4 共用代码什么时候进的 toexec

第 2 步时只有 gld 在用，按 toexec 的规矩（"两个产品都在用才进来"）留在 gld；第 3 步 ccnm 也要了，就整块搬成 `toexec-mcp` 0.1.0：读配置、握手调用、子进程通道、连接池、结果整理。它是 toexec 里第一个有依赖的 crate（serde_json、toml），例外写在 `docs/development.md` 第 3 条。gld 那边删掉自己那份、改链它，行为没变（跟着代码搬走的测试在 toexec 里接着跑）。

## 4. 第 3 步：ccnm 转项目那台机器上的 MCP server

实测在 [`evidence/v4-mcp/runtime-relay/`](../../evidence/v4-mcp/runtime-relay/README.md)，ccnm 的设计和删除清单在 ccnm 的 P49 记录。

- **一个工具 `call_mcp_tool`**：不带参数列 server，带 `server` 列它的工具（这一步才起它），再带 `tool` 和 `arguments` 调用。一个而不是 gld 那样三个，是因为起 server 就是以执行账号跑程序：在有人值守的会话里它得和 `exec_command` 一样每次都问人，分成"列"和"调"两个工具只会多问一次。大结果接 ccnm 现成的 `read_output`（同一个留存目录、同一套上限和过期），不另加工具。
- **哪些 server**：项目的 `.mcp.json` 在前、同名压过执行账号装的（Claude Code 的 project > user）。只转 stdio 的：HTTP 的不需要跑在项目旁边，从 Agent 那边连（第 4 步），列出来并写明原因。
- **默认全开**（用户定），但过的门和 `exec_command` 一样：只有能写的会话有这个工具，执行门和 Runtime 凭据检查在起 server 之前，工作区配了 OS 沙箱就套沙箱（没网络），环境按命令的规矩清理。配置里给 server 的 token 照传；Agent 的登录变量不传，`${VAR}` 也查不到像凭据的名字。会话结束时先停 server 再放写锁。开关是 Runtime 自己的 `[runtime_mcp]`（`enabled`、`project`、`hidden`）。
- **gld hub**：多一个静态的 `remote_call_mcp_tool`（coding 会话里），原来担心的"白名单是静态的、要动态透传"不成问题——远端的工具面就这一个固定的工具。远端没有可转的 server 时 ccnm 不列它，hub 报"那边没东西可转"，不报"升级 ccnm"。

## 5. 第 4 步：ccnm 转 Agent 机器上的 MCP server

实测在 [`evidence/v4-mcp/agent-mcp/`](../../evidence/v4-mcp/agent-mcp/README.md)，ccnm 的设计和删除清单在 ccnm 的 P50 记录。

### 5.1 为什么不是"写进原生客户端的配置"这条最简单的路

先量了（零额度）：直接写进 Claude Code / Codex 的 MCP 配置，工具调得通，但**大结果会丢**——Claude Code 2.1.278 把超过约 50 000 字符的结果存到 Agent 磁盘、只给模型 2 KB 预览要它用 `Read` 读，受管会话没有 `Read`；Codex（指定模型时）只留 12 KB，Code Mode 留 40 KB。而且每个 server 的全部工具都会进每一次请求。所以和第 3 步一样，由 ccnm 转：P48 起就在 Agent 上的那个小服务（`ccnm internal agent-skills`，到模型那里是 `mcp__ccnm_agent__*`）多两个工具，`call_mcp_tool`（和第 3 步同名同用法）和 `read_mcp_result`（长结果留在内存里按段读）。一次调用的步骤和第 3 步是同一份代码。

### 5.2 默认给哪些

3.3 预告的问题落在这里：从配置分不出谁只联网。按"别的机器上的地址 / 这台机器上跑的"分，这是从配置上唯一分得清的一刀：

- **别的机器上的 HTTP 地址**（exa、DeepWiki）默认给——用户定的"默认全开"加上方案里"只放网络类"。
- **这台机器上跑的**（`command` 起的程序、`127.0.0.1` 上的服务：context7、Filesystem、desktop-commander、playwright……）默认不给，Agent 自己的 `[agent_mcp] local` 点名才给。
- workspace 在 Runtime 上还有一票：`agent_tools` 多了一个值 `mcp_servers`，默认开，去掉就不给。放在 Runtime 上的理由和 P46 的 `web_fetch` 一样：远端 server 收得到模型发给它的东西，exa 还带抓网页的工具。**这和 `web_fetch` 默认关是矛盾的**，文档照实写了，要收紧就去掉 `mcp_servers`。

### 5.3 HTTP 怎么连

ccnm 没有 HTTP 客户端（第 3 步刻意没带），而第 4 步默认给的恰好全是 HTTP 的。用 Agent 上系统自带的 `curl`：TLS、HTTP/2、代理变量都是它的，ccnm 不多一套 TLS 依赖。地址和请求头写进只有本账号能读的临时文件交给 `curl -K`，不上命令行（exa 的 key 就在地址里）。拆 SSE 那一段和 gld 同一份（`toexec-mcp` 的 `sse`）。实测真实 DeepWiki 经它读全 407 KB。

### 5.4 附带修掉的两处

量第 4 步时顺带发现，**已有的** ccnm 工具在两个客户端里也会丢东西：Claude Code 那条 5 万字符的线，ccnm 的 `read_file` / `load_skill` 一页（最多 64 KiB）会撞上；Codex 指定模型时那条 12 KB 的线，ccnm 几乎每个工具的一页都会撞上——第 3 步说"`call_mcp_tool` 32 KiB 一段、不会丢"只在 Claude 上成立。ccnm 给这几个工具声明了 `maxResultSizeChars`、给 Codex 会话加了 `tool_output_token_limit=20000`，实测都生效（0.154.0 上也是）。

### 5.5 共用代码

`toexec-mcp` 0.2.0 加了两块：`kept`（长结果留在内存里按段读，gld 第 2 步那份）和 `sse`（拆 SSE 回复，gld 第 2 步那份）。gld 删掉自己的、改用它们；ccnm 的 Agent 端也用。第 3 步里"一次调用的步骤"那段在 ccnm 内部抽成了两台机器共用，没进 toexec——gld 的工具形状不一样（三个工具），共用不上。
