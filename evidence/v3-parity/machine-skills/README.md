# 受管会话能不能用上 Agent 机器上装好的 skills（原始记录）

对应 [机器级 skills 与 MCP 方案](../../../docs/plan/implementation-plan-v4-machine-skills-mcp.md) 第 1 步。**全程零模型额度**，只在本机（macOS arm64）跑。ccnm 按这些结果怎么设计写在 ccnm 的研究记录里，这里只放脚本和结果。

## 要回答的问题

用户要 gld / ccnm 都能用上 Agent 机器和 Runtime 机器上**已经装好**的 skills（`~/.claude/skills`、`~/.agents/skills` 这类用户级目录），不只是项目里自带的。Runtime 那边由 ccnm 的 `load_skill` 读，没有悬念；Agent 这边要先弄清：

1. Claude Code / Codex 在 ccnm 受管会话的启动参数下，会不会自己列出 Agent 机器上的 skills；
2. 列出来之后，模型读得到正文和附件吗；
3. 要让它读到，最窄的放法是什么，会不会顺带漏出别的东西；
4. 同名 skill 同时在项目和用户目录里时，原生让谁生效（决定 ccnm 合并两处清单时的优先级）。

## 怎么测的

`run_probe.py`：假模型接口（Anthropic 与 Responses 两种都接）把每次请求原样存下，按剧本回工具调用；`HOME` 是临时目录，只放探针 skill；`sandbox-exec` 禁非本机出站。启动参数照 ccnm 的 `launch_cmd`（Claude）和 `build_launch_cmd`（Codex，print 模式）拼，和 [agent-surface](../agent-surface/README.md) 同一套，只改要测的几项。探针 skill 的描述是唯一记号，在请求 JSON 里搜得到就说明它进了模型上下文。

| 场景 | 做什么 |
| --- | --- |
| `denied` | ccnm 现在的做法：`--tools WebSearch`，settings 的 deny 里有 `Skill` |
| `skill` | `--tools WebSearch,Skill`，allow `Skill`：清单在哪、调 `Skill` 回来什么、再按正文去读附件 |
| `scoped_read` | 再加 `Read`，allow 只写 skill 目录（绝对路径 `Read(//…/**)` 和 `Read(~/…/**)` 两种写法各一遍）：依次读 skill 附件、cwd 里的文件、HOME 里别的文件、`/etc/hosts` |
| `precedence` | 同名 skill 同时在 cwd 的 `.claude/skills` 和 `~/.claude/skills`，外加一个只在项目里的 |
| `codex` | Codex：`~/.agents/skills` 和 `$CODEX_HOME/skills` 里各放一个探针 |
| `codex_off` | 同上，加 `-c skills.include_instructions=false` |

结果在 `runs/claude-2.1.278-codex-0.154.0.json`（临时路径已换成 `<out>`）。Codex 两个场景在 Homebrew 的 0.155.1 上也跑过，结论相同。

`run_ccnm_agent.py` 是第二步：照下面的结论做出 ccnm 的 `ccnm internal agent-skills` 之后，用**真实的 ccnm 二进制**接到真实的 Claude Code / Codex 上再验一遍（`CCNM_BIN` 指定二进制，Runtime 那一半仍由 `tiny_server.py` 冒充）。结果在 `runs/ccnm-agent-skills-claude-2.1.278-codex-0.154.0.json`，见文末。

## 结果：Claude Code 2.1.278

- **ccnm 现在的做法下一个用户 skill 都看不到**（`denied`）：请求里没有任何探针记号。
- **放开 `Skill` 后**（`skill`）：清单以 "The following skills are available for use with the Skill tool" 出现在第一条 system 消息里，**连同 Claude Code 自带的 skill**（如 `dataviz`，它们假定 Read/Bash 都在）。`~/.claude/skills` 进清单；**`~/.agents/skills` 不读**，除非软链进 `~/.claude/skills`。调 `Skill` 返回 "Launching skill: …"，正文和 "Base directory for this skill: <绝对路径>" 跟在后面一条消息里。接着读附件：`No such tool available: Read`。
- **只放 skill 目录的 Read**（`scoped_read`，两种写法结果一样）：skill 附件读得到；HOME 里别的文件和 `/etc/hosts` 被自动拒绝（"requires approval, and this session has no approval surface"）；**但 cwd 里的文件不用任何 allow 就读到了**。ccnm 的 cwd 是会话状态目录（`mcp.json`、`settings.json`、传给 Runtime 的 payload 都在里面），这等于为了读附件顺带把它交给了模型。交互模式下没 allow 的读会弹给人确认，那是另一回事。
- **同名时用户目录的赢**（`precedence`）：只有 `~/.claude/skills` 那份的描述进了清单；只在项目里的那个照常进，说明项目 skills 是读了的、是被盖掉而不是没读到。

## 结果：Codex 0.154.0（0.155.1 相同）

- **ccnm 现在的启动参数下，Agent 机器上的 skills 已经进了清单**（`codex`）：developer 消息里有一段 `<skills_instructions>`，列出 `$CODEX_HOME/skills`、`~/.agents/skills`、`$CODEX_HOME/skills/.system`（Codex 自带的 imagegen、openai-docs、plugin-creator、skill-creator、skill-installer）三个根下的全部 skill，每条给 Agent 机器上的路径，让模型自己去打开 `SKILL.md`。**可 ccnm 关了 shell，模型没有任何工具能读 Agent 机器的文件**——这份清单是误导：模型要么放弃，要么拿这个路径去调 ccnm 的 `read_file`，在 Runtime 上找一个不存在的文件。
- `~/.claude/skills` 不读。
- **`-c skills.include_instructions=false` 把整段去掉**（`codex_off`）：请求里一个探针记号都没有，`<skills_instructions>` 也没了。

## 这些结果决定了什么

写在方案第 1 步里，这里只列结论：Agent 机器上的 skills 由 ccnm 自己在 Agent 上读出来交给模型，而不是放开原生的——Claude 原生要开 Read 而会话状态目录会跟着漏、还会带上一批依赖原生工具的内置 skill；Codex 原生根本读不到。Codex 那份读不到的清单用 `skills.include_instructions=false` 关掉。合并 Runtime 上的清单时，同名照原生让用户目录的赢。

## 验证：ccnm 的 Agent 端服务接上真实客户端（`run_ccnm_agent.py`）

配置照 ccnm P48 生成的写：Claude 的 `mcp.json` 里第二个 server `ccnm_agent`，settings allow 加 `mcp__ccnm_agent__load_skill`，`Skill` 仍在 deny；Codex 加 `-c skills.include_instructions=false` 和 `-c mcp_servers.ccnm_agent.*`。

| | 结果 |
| --- | --- |
| Claude Code 2.1.278 | 工具表里有 `mcp__ccnm_agent__load_skill`，description 末尾是 `Installed here:` 加两个探针 skill（`~/.claude/skills` 和 `~/.agents/skills` 各一个）。print 模式下四次调用都没被权限拦：不带名字拿到全表、带名字拿到正文和 `${CLAUDE_SKILL_DIR}` 的绝对路径、`file=reference.md` 拿到附件内容、`file=.env` 被拒（`CCNM_E_POLICY`，正文里没有密钥） |
| Codex 0.154.0（`gpt-5.1-codex`） | 请求里有 `mcp__ccnm_agent` 命名空间；假模型发 `namespace=mcp__ccnm_agent, name=load_skill` 的调用，`function_call_output` 里是附件内容；请求里没有 `<skills_instructions>` |

**没验到的**：Codex 不写 `--model` 时（默认模型自带 Code Mode），MCP 工具是延迟加载的、模型要在 JS 里找，这条路径没让假模型走一遍——ccnm 自己的工具也走同一条，所以只是没有单独证据，不是已知不通。真实模型会不会主动用这些 skill，没测（要额度）。
