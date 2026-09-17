# MCP Host 怎么对待工具 description、prompts 和 skills 扩展（原始记录）

对应 [v3 方案](../../../docs/plan/implementation-plan-v3-native-parity.md) 第 4.1 节和 ccnm 的 P36.1。结论和它对设计的影响写在 ccnm 的 `docs/research/p36-skills-surface-2026-09-17.md`，这里只放脚本和结果。**全程零模型额度**，只在本机跑。

## 要回答的问题

把 Runtime 上项目自带的 skills 交给模型，有三条可能的通道：放进某个工具的 description、登记成 MCP `prompts`、走 MCP 官方的 skills 扩展（[SEP-2640](https://github.com/modelcontextprotocol/ext-skills)，2026-09-13 定稿）。哪条通道在哪个 Host 上真的通，得看 Host 实际怎么做，不能按文档猜。

## 怎么测的

| 文件 | 做什么 |
| --- | --- |
| `probe_server.py` | 探针 MCP server（stdio，只用标准库）。一个 description 长 6000 字符的工具，在第 300 / 1000 / 2000 / 2100 / 3000 / 5900 个字符处埋了标记；一个 prompt；按 SEP-2640 声明 skills 扩展并提供一个 skill。**把收到的每个方法名记下来**——Host 连上之后调了什么，一目了然 |
| `run_codex.py` | Codex 连探针。模型接口是本机假服务（把 Codex 发出的请求原样存盘），进程树用 `sandbox-exec` 禁掉非本机出站，`HOME` / `CODEX_HOME` 是临时空目录。在存下来的模型请求里找标记 |
| `run_claude.sh` | **未登录**的 Claude Code 连探针。`HOME` 是临时空目录，模型地址指向一个关着的本机端口；脚本先确认 `claude auth status` 是 `loggedIn: false`，不是就停。MCP 连接发生在认证失败之前，所以方法记录和 debug 日志照样拿得到 |
| `runs/*.json` | 两次运行的汇总。原始日志和存盘的模型请求不入库 |

```bash
python3 run_codex.py <仓库外的新目录>
bash run_claude.sh <仓库外的新目录>
```

## 结果（2026-09-17，macOS arm64）

| | Claude Code 2.1.273 | Codex 0.154.0 |
| --- | --- | --- |
| 连上之后调的方法 | `tools/list`、`prompts/list`、`resources/list` | **只有** `tools/list` |
| `skills/list` | **没调**（见下面的静态证据：挂在默认关闭的特性开关后面） | 没调 |
| 工具 description | 截到 **2048 个 UTF-16 码元**，debug 日志：`Tool "probe_tool" description truncated from 6000 to 2048 chars` | **不截**：6000 字符、六个标记全部出现在发给模型的请求里 |
| `instructions` | 截到 2048（V2-Q1 已测） | 原样进模型请求 |
| 模型请求 | 0 次（`input_tokens: 0`、`duration_api_ms: 0`） | 1 次，发给本机假服务 |

Claude Code 那一列没有验证"截断后的文本确实进了模型上下文"——没登录，没有模型请求可看。截断发生在客户端、在发请求之前，这一点由下面的静态证据支持。

## 静态证据：Claude Code 2.1.273 的打包代码

`strings` 从本机二进制抽出来的 JS，三处：

**工具 description 和 instructions 用的是同一个截断函数**（`Yk` 是 2048）：

```js
function Ao(e,n){if(!e)return e;return Io(e,"Server instructions",n)}
function Io(e,n,r){if(e.length<=Yk)return e;
  if(r!==void 0)Q(r,`${n} truncated from ${e.length} to ${Yk} chars`);
  return se(e,Yk)+"… [truncated]"}
// 构造 MCP 工具对象时：
Me=g?.tools?.[V.name]??V.description??"", O=Io(Me,`Tool "${V.name}" description`,e), …
async description(){return Me}, async prompt(){return O}   // 给模型的是 O，截断过的
```

**MCP prompt 变成斜杠命令**，名字是 `mcp__<server>__<prompt>`，界面上显示 `<server>:<prompt> (MCP)`；用户敲的参数按空白切开，依次对应 prompt 声明的 `arguments`，缺必填的会报 `Missing required argument`：

```js
name:"mcp__"+pn(e.name)+"__"+g.name, … userFacingName(){return …`${e.name}:${g.name} (MCP)`}
async getPromptForCommand(N,Y){let te=N.trim(),A=te?te.split(/\s+/):[]; …
```

**skills 扩展的客户端已经在里面，但挂在一个默认关闭的特性开关后面**：

```js
var Mlt="io.modelcontextprotocol/skills";
function qd(){return P("tengu_mcp_skills",!1)}                       // 默认 false
function NMt(e){return qd()&&!!e?.resources&&qjn(e)}                 // 要有 resources 能力
function qjn(e){return e?.extensions?.[Mlt]!==void 0}                // 和扩展声明
```

开关打开时它的行为（同一个二进制里读到的）：`skills/list` 分页取，最多 20 页、100 个 skill，每个字段最长 4096；对每个条目用 `resources/read` 取 `…/SKILL.md` 的文本；skill 在 CLI 里叫 `<server>:<skill>`；**MCP 来的 skill 声明的 `hooks` 和 `allowed-tools` 一律忽略**（日志原话：`MCP-sourced skills cannot register hooks` / `cannot bypass permissions`）。它认的条目形状是 `{frontmatter, uri, digest}`——定稿前的草案；定稿版把 `digest` 挪进了 `resources` 数组。

## 范围

只测了这两个版本。Claude Code 那个开关对哪些账号打开、什么时候默认打开，从本机看不出来。ChatGPT 等 Web AI 经 gld 接入时支持 MCP 的哪些部分，没有测。
