# 实施方案 v4：gld / ccnm 用上两台机器上已经装好的 skills 和 MCP server

日期：2026-09-22。状态：**第 1 步（skills）、第 2 步（gld 转本机 MCP server）做完，第 3–4 步没开工**。产品进度仍由各自仓库记录（ccnm 的 `docs/plan/status.json`、gld 的 RFC）；这里只放跨仓的决定和顺序。

承接 [v3 方案](implementation-plan-v3-native-parity.md)：v3 让 gld / ccnm 自己的工具对齐原生能力，外加**项目里**的 skills。v4 回答下一个问题——**用户已经在两台机器上装好的 skills 和 MCP server，怎么也用上**。

## 0. 用户的话和决定

2026-09-22，用户（原话）："是 gld 或 ccnm 都可以使用 agent 和 runtime 机器上的已经安装的 skills 和 mcp，而不是工具自身的 mcp 工具"。随后："最好为 gld 完善一套合理地 mcp/skills 工具来专门操作它们；ccnm 也是，但是默认全开。runtime 传递过程是否会丢信息，数据量是否过大等问题；代码模块化，解耦，如果后期这块功能不需要，能简单的删除。抽取共用模块进 toexec。按建议顺序做，先从第 1 步开始"。

同一天更早做过一版"用 `~/.agents/mcp.json` 按名字收窄 gld / ccnm 自己的工具"，是理解错了需求，已整个撤销（toexec `b6c5e36`、ccnm `37694f6`、gld `8180107`）。**v4 的方向是把装好的东西接进来，不是收窄自己的。**

## 1. 四步，按这个顺序

| 步 | 做什么 | 为什么排这里 | 状态 |
| --- | --- | --- | --- |
| 1 | 两台机器上装好的 skills | 只读、风险最低，用户点名先做 | **做完**（ccnm P48，gld 同步） |
| 2 | gld 聚合本机装好的 MCP server，经它一个入口交给 ChatGPT 这类 Web AI | Web AI 用上本机 MCP 的唯一办法 | **做完**（gld RFC-0006） |
| 3 | ccnm 在 Runtime 上代理 MCP server（含项目自带的 `.mcp.json`） | 数据库这类只能在项目旁边跑的 server | 没开工 |
| 4 | ccnm 会话接 Agent 上装好的 MCP server | 技术上最简单、风险最大：Filesystem、desktop-commander 这类一接进来就绕过"项目只能经 Runtime 碰到"的保证 | 没开工 |

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

### 3.4 共用代码为什么还没进 toexec

读两份配置的代码（gld `machine_mcp/installed.rs`）第 3、4 步 ccnm 都要用，但这一步只有 gld 在用，而读 Codex 的 TOML 要 `toml` 库——toexec 的规矩是"两个产品都在用才进来"和"不加依赖"。所以它先留在 gld、只依赖标准库 + serde_json + toml，第 3 步 ccnm 要用时整个搬成 `toexec-mcp`，那时对"不加依赖"单独破例并写明理由。

## 4. 第 3–4 步只记开放问题

- 第 3 步：Runtime 上以执行账号起 server，风险和 `exec_command` 同级，要不要过同一道执行门；gld hub 的 G1 白名单是静态的，要动态透传。
- 第 4 步：哪些 server 算"只走网络"（context7、exa、deepwiki、mcp-time），哪些碰本机（Filesystem、desktop-commander、playwright、Puppeteer、frida、idapro），默认怎么定。
