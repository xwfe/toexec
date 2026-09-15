# workspace-kernel 实施计划

版本：v1（2026-09-15）。状态：已确认方向，未开始实施。
本文是 gld、ccnm 与本仓库共用执行端改造的**唯一计划来源**。两个产品仓库里只写"本产品这一步做什么"，并链接回这里，不复制本文内容。

---

## 0. 一页纸摘要

**要做什么**：把"让 AI 编程 Agent 在项目机器上读文件、改代码、跑命令"这件事，做成一套可复用的 Rust 库（本仓库），让 gld、ccnm 和以后的项目共用；同时把工具面做得更省 token、更少轮次。

**不变的前提**（任何阶段都不能破）：

1. 模型凭据只在 Agent 端。模型由官方 Claude Code / 官方 Codex CLI 调用，用用户自己的订阅登录；**不用 API key，不 fork 任何 harness，不自建模型循环**。
2. Runtime 端只执行，不持有模型凭据；ccnm 的执行账号（`ccrun`）不持有主动连出去的控制凭据。
3. 已冻结的公开契约不改语义：ccnm 的 `ccnm.workspace-mcp/1`、`ccnm.machine/1`，以及 gld 默认工具集。**行为变化只进新工具集**。

**三条主线**：

| 主线 | 一句话 | 收益来源 |
| --- | --- | --- |
| Codex 走官方 exec-server | Agent 端的 Codex 用原生工具，工具在 Runtime 上由官方 `codex exec-server` 执行 | 消除实测的额外轮次：`tool_search`、补丁格式错 4 次 |
| Claude 走更好的 MCP 工具 | 先加 `alwaysLoad` 和精简 instructions，再做贴近原生的 `native@1` 工具集 | 消除延迟加载的额外调用，贴近模型熟悉的参数 |
| 共享内核 + gld hub 接 ccnm | 读取/编辑/提交/执行/搜索做成一份实现；gld hub 通过 ccnm 公开 bridge 访问远端项目 | 修一次、两边受益；Web 端可以操作远端 Runtime |

**第一周就做的三件事**（详见第 11 节）：统一 Rust 版本与 MSRV CI；搭 A/B 评测脚本并跑基线；Codex exec-server 无模型 spike。

---

## 1. 已定决策

每条后面是决定性理由。被否的方案保留在第 1.2 节，别再重新讨论，除非出现新证据。

### 1.1 采用

| 编号 | 决策 | 决定性理由 |
| --- | --- | --- |
| D1 | 共享物是**crate 仓库**，不是网络服务 | 两个产品边界不同（ccnm 隔离执行身份，gld 以用户身份执行）；库只共享机制，策略留在产品。加权评估 4.3，服务形态 2.7，合仓 2.4 |
| D2 | 仓库位置 `/Users/bing/xdw/workspace-kernel`，独立 git 仓库 | 不让任一产品依赖另一产品的发版；独立 CI、MSRV、许可证；未来第三方可直接用 |
| D3 | 许可证 Apache-2.0 | 搬入的 codex / webcodex / grok-build 代码是 Apache-2.0；ccnm（MIT）依赖 Apache-2.0 库没有问题 |
| D4 | 三个仓库统一 `rust-version = "1.89"`，并各加一个 MSRV CI 任务 | 提交层需要 `File::try_lock`（1.89 稳定）；`ignore` 需 1.88、`process-wrap` 需 1.87、`rmcp 3.3` 需 1.88。gld 现声明 1.85 但 CI 只跑 stable，从未验证过 |
| D5 | Codex 在 Runtime 上直接用**官方** `codex exec-server`，ccnm 只做包装 | 0.154.0 已包含；协议不传凭据；沙箱在执行端施加（macOS 实测 Seatbelt 拦住越界写）。自研兼容服务端要自己做沙箱 |
| D6 | Claude 继续走 MCP 工具（唯一合规路），改进顺序：`alwaysLoad` → `native@1` | Anthropic 条款只允许"未修改的官方二进制 + 自己订阅"；Claude Code 没有"循环在本机、工具在远端"的机制 |
| D7 | 行为变更只进新工具集（`native@1`、`lean@1`），旧契约做成内核之上的适配层 | 冻结契约有外部使用者；新语义（对原文匹配、服务端记读取、纯文本结果）与旧契约不兼容 |
| D8 | gld hub 的远端成员用**静态带前缀的工具**（`remote_read_file` 等），不用 `runtime_open/runtime_call` 通用信封 | `workspace-mcp/1` 已冻结只加不删，schema 可以静态发布；通用信封每次会话返回 schema、参数多一层 JSON 转义（+34%），模型也用不上原生函数调用校验 |
| D9 | 所有搬入的第三方代码按文件记录来源；claw-code、Crush、Zed 只借思路，不搬代码 | 保证能发布到 crates.io（不允许 git 依赖）；借思路时先写行为规格，再按规格实现，不对照源码逐行改写，不引入任何 Claude Code 提示词原文 |
| D10 | 任何"更好"都由 A/B 数据判定 | 目前所有 token/轮次收益都是推断，只有 fsck（+34%→+12%）和 Gemini（轮次约 −10%）两组外部数字 |

### 1.2 否决（保留理由）

| 方案 | 否决理由 |
| --- | --- |
| fork codex / grok-build / goose 等，去掉账号和模型 | 去掉模型就只剩工具层；上游 90 天 1500–3200 次提交，fork 很快失效；去账号大多改配置即可，不需要 fork |
| 用开源 harness + API key 在 Runtime 跑 Agent | 与前提 1 冲突（用户明确排除 API key） |
| 第三方 harness 用 Claude 订阅 OAuth | Anthropic 2026-02-20 起条款明文禁止，2026-01-09 起服务端封禁 |
| WebCodex Server + Runner 整体采用 | 与 gld 是同类产品而非后端；Runner 主动外连，违反 ccnm 执行身份约束；v0.4.1→main 5 天 86 次提交、+9 万行 |
| ACP 协议承载 Runtime 执行 | ACP v2 草案删除 client 侧 `fs/*` 与 `terminal/*`；Zed 自己在 2026-02 撤掉了"同名 MCP 工具转发"做法 |
| Crush 式"只读命令前缀白名单免审批" | `ls & rm …`、`timeout <任意命令>` 可绕过（依据其测试用例推断） |
| 多处编辑按顺序应用、失败留半成品（Zed / Crush） | 状态不可预期；我们保持原子提交 |

---

## 2. 目标架构

```text
Agent Node（订阅凭据只在这里）                          Runtime Node（ccrun）
                                                     ┌ ccnm internal exec-serve（新增）
Codex CLI ── exec-server 协议 ── SSH ─────────────> │  身份审计 → 拿写锁 → 清环境 → 空 CODEX_HOME
                                                     │  → exec 官方 `codex exec-server --listen stdio`
                                                     └
Claude CLI ── MCP stdio ── SSH ───────────────────> ccnm internal mcp-serve
                                                       ├ native@1（新增，只给托管 Claude）
                                                       └ workspace-mcp/1（冻结，外部入口）
                                                              │
Web AI ── HTTPS MCP ── gld hub ─┬ 本地成员 → gld 工具（默认集 / lean@1）─┤
                                └ 远端成员 remote_* → ccnm mcp bridge ───┘（按协议，不链接 ccnm-core）
                                                              ▼
                                        workspace-kernel（两边各自编译链接，线上看不见）
```

### 2.1 本仓库的 crate 分层

| crate | 负责 | 不负责 |
| --- | --- | --- |
| `workspace-kernel` | Root 句柄与路径解析、有界读取、编辑引擎、补丁解析、事务提交、读取记录、执行会话、搜索/列表/目录树 | 读 HOME、读配置、认证、全局 tokio runtime、写权授予、协议 |
| `workspace-kernel-tools` | 工具集：参数结构 → 纯文本结果；`native@1`、`lean@1`；token 规范与体积断言 | 传输、会话身份 |
| `workspace-kernel-mcp`（P4 后） | 接 rmcp 的 handler，stdio 与 Streamable HTTP 共用 | 产品认证 |
| `conformance/` | 中立一致性测试集：恶意路径、编码、补丁、进程树、预算、描述与行为一致 | — |
| `bench/` | A/B 评测脚本与任务夹具 | — |
| `wk` 二进制（P5） | `wk serve --stdio|--http`，给非 Rust 项目直接起进程 | — |

### 2.2 产品必须自己提供的东西（trait 注入）

| trait | 含义 | ccnm 的实现 | gld 的实现 |
| --- | --- | --- | --- |
| `PathPolicy` | 哪些路径可读/可写、受保护目录、是否接受绝对路径 | 拒绝绝对路径与 `..`；保护 `.git` | 保护 `.git`、`.github`、自身数据目录；外部读开关 |
| `WriteAuthority` | 调用方必须先持有写权，内核只校验"持有"这个事实 | Git common dir 上的跨进程锁 | 守护进程内的会话级互斥 |
| `EnvPlan` | 子进程环境变量怎么来 | 清除 Agent 凭据相关变量 | 按工作区配置 |

内核里不出现 `provider`、`workspace 名称`、`token` 这类产品概念。

---

## 3. 工程约定

| 项 | 约定 |
| --- | --- |
| Rust | `rust-version = "1.89"`，三个仓库同步；升级时三个仓库一起升，各自一个提交，原因写进提交说明 |
| edition | 本仓库 2024；gld 保持 2021（edition 按 crate 独立，互不影响） |
| CI | macOS、Linux、Windows × stable；另加 `cargo +1.89 check --workspace --all-targets --locked` |
| 依赖 | 只用发布在 crates.io 的依赖；新增依赖在提交说明写用途、MSRV、许可证、替代品 |
| 第三方代码 | 搬入的文件保留原版权头；`THIRD_PARTY.md` 逐行登记：来源仓库、提交 SHA、原路径、许可证、修改摘要、带入的测试 |
| 只借思路的来源 | 先在 `docs/spec/` 写行为规格，再实现；提交说明注明"依据规格 X"，不注明对照了哪段源码 |
| 版本 | 库遵循 semver；工具集以 `名字@主版本` 冻结，语义变化升主版本，旧版至少保留 2 个发布 |
| 消费方式 | P1–P4 期间两个产品以 git tag 依赖；P5 发布 crates.io 后改为版本依赖 |
| 回滚 | 任一产品可单独退回上一个 tag 或旧适配层；本仓库不引入持久化格式，P3 引入 journal 前先写兼容设计 |

---

## 4. 实施阶段总览

| 阶段 | 目标 | 预计周期（单人） | 依赖 | 耗订阅额度 |
| --- | --- | --- | --- | --- |
| P0 | 统一版本、文档归位、已知缺陷分流 | 1–2 天 | — | 否 |
| P1 | 拿到基线数据；三处快速收益；内核打通 | 1–2 周 | P0 | 是（约 75 次运行） |
| P2 | Codex 切到 exec-server；gld hub 只读接 ccnm | 2–3 周 | P1 | 是（约 15 次） |
| P3 | 编辑/提交/执行进内核；gld 迁移；`native@1` | 3–5 周 | P1 | 是（约 45 次） |
| P4 | 搜索/列表/大纲；`lean@1`；hub coding | 2–4 周 | P3 | 是（约 10 次） |
| P5 | 发布与开放复用 | 1–2 周 | P4 | 否 |

周期是粗估，用来排序，不是承诺。P2 与 P3 可以并行。

---

## 5. 分阶段任务

每个任务：**编号 · 仓库 · 做什么 · 验收（可判定）**。验收里写的命令都要实际跑并保留输出。

### P0 基线与归位

| 编号 | 仓库 | 做什么 | 验收 |
| --- | --- | --- | --- |
| P0.1 | gld | `rust-version` 1.85 → 1.89；CI 加 MSRV 任务 | `cargo +1.89 check --workspace --all-targets --locked` 通过；stable 全量测试通过 |
| P0.2 | ccnm | CI 加 MSRV 任务（已是 1.89） | 同上 |
| P0.3 | 本仓库 | 骨架：workspace、LICENSE、`THIRD_PARTY.md`、CI 三平台 + MSRV、空 `conformance/` | CI 绿；`cargo deny`（或等价）许可证检查通过 |
| P0.4 | gld | RFC-0002 顶部注明：内核与工具规范以本计划为准；§5.2 的 `runtime_*` 被 D8 取代；MSRV 以 D4 为准 | gld 文档测试 `docs_commands_exist` 通过 |
| P0.5 | gld | 补 LICENSE 文件（Cargo.toml 已声明 Apache-2.0 但仓库无文件） | 文件存在，内容为 Apache-2.0 全文 |
| P0.6 | ccnm | 开始 P1 的 ccnm 工作时，按 ccnm `docs/plan` 规则立新阶段，链接本计划 | `python3 scripts/check_plan.py` 通过 |

安装 `1.89.0` 工具链属于本机环境变更，执行前单独确认。

### P1 取证与快速收益

#### P1-B 评测脚本（本仓库 `bench/`）

| 编号 | 做什么 | 验收 |
| --- | --- | --- |
| P1-B1 | 三个任务夹具（见第 7 节），每个固定到一个快照提交，自带判分脚本 | 判分脚本对"标准答案补丁"判通过，对未改动判失败 |
| P1-B2 | 运行器：记录 input/output/缓存 token、费用、工具调用次数、墙钟、成功与否；Claude 用 `claude -p --output-format stream-json --verbose`，Codex 用 `codex exec --json` | 同一组参数连跑 2 次，输出结构一致 |
| P1-B3 | 缓存断裂统计：每轮对 tools、system、messages 分别取 hash，缓存读取骤降时记录是哪一项变了 | 人为改一次 instructions，统计能指出"system 变化" |
| P1-B4 | 跑基线：A=官方 CLI 本机原生工具（项目在同机）、B=经 ccnm。Claude、Codex 各 3 任务 × 5 次 × 2 组 | 产出 `bench/results/2026-xx-baseline.md`，报中位数与最小/最大值 |

#### P1-C ccnm 快速收益（ccnm 新阶段）

| 编号 | 做什么 | 验收 |
| --- | --- | --- |
| P1-C1 | 先实测：instructions 超过 2KB 时 Claude Code 是否截断（官方文档说截到 2KB） | 用带标记行的超长 instructions 跑 1 次，记录模型能否复述标记 |
| P1-C2 | 7 个工具加 `_meta["anthropic/alwaysLoad"]: true` | tools/list 含该字段；契约文档注明为加法；`cargo test -p ccnm-cli --test external_mcp` 与 Python 中立客户端测试通过 |
| P1-C3 | instructions 顺序改为：用途与模式 → 其他说明文件路径清单 → 标记行 → 根说明文件正文；总量 ≤2KiB，超出时正文改由 `workspace_info` 返回；说明文件按内容 hash 去重 | 协议 fixture 变化逐条说明为"表面变化"，不重录掩盖 |
| P1-C4 | schema 去噪：去掉 `$schema`、`default:null`、`format:uint32`、`["x","null"]` 写法 | tools/list 从 8,852B 下降，目标 ≤7,800B；工具语义测试不变 |
| P1-C5 | A/B：B′（C2–C4 之后）对 B，Claude 3 任务 × 5 次 | 采纳条件见第 7.4 节 |

#### P1-X Codex exec-server spike（scratchpad，不进产品）

| 编号 | 做什么 | 验收 |
| --- | --- | --- |
| P1-X1 | Runtime 侧包装脚本：身份审计 → 拿写锁 → 设空 `CODEX_HOME` → 清环境 → `exec codex exec-server --listen stdio` | 用 scratchpad 里的探测脚本发 `initialize`、`process/start echo`、`fs/readFile`，全部成功 |
| P1-X2 | Agent 侧桥：ccnm 启动 Codex 带 `--ignore-user-config`，此时 Codex 只读 `CODEX_EXEC_SERVER_URL`（只接受 ws 地址），所以需要本机 ws → SSH stdio 转发；监听 127.0.0.1 随机端口，只接受第一个连接后关闭监听 | 第二个连接被拒；设置该变量后 Codex 自动不挂本地环境（源码 `environment_provider.rs`），模型选不到本地执行 |
| P1-X3 | 故障行为：stdin EOF → exec-server 退出 → 写锁释放；SSH 黑洞时靠 keepalive 断开 | 进程表无残留；锁状态为 released |
| P1-X4 | 安全检查：远端子进程环境不含 `*TOKEN*/*KEY*/*SECRET*`；`ccrun` 的 `CODEX_HOME` 为空（防止 `environmentConfig/read` 注入 MCP 配置）；`!` 命令与命令型 hooks 在 exec 模式下不可用 | 逐项有探测输出 |
| P1-X5 | 用额度跑 1 次真实 `codex exec` 回合：让它在远端建一个文件并跑 `echo` | 文件只出现在 Runtime；Agent 端无该文件；工具调用走 exec-server |

**停止点**：X2 如果无法安全实现（例如 ws 端点无法限制只连一次），改评估"去掉 `--ignore-user-config`、用 ccnm 生成的专用配置目录"方案，另记录代价后再决定。

#### P1-K 内核打通（本仓库 + 两个产品）

| 编号 | 做什么 | 验收 |
| --- | --- | --- |
| P1-K1 | 有界行扫描器：输入是已打开的 `Read`；行数+字节双上限；增量 UTF-8 校验；UTF-8 边界截断；超长无换行输入不整行进内存 | 生成式超长单行（远大于 64MiB 的虚拟流）峰值内存有固定上界；RFC-0002 K01–K05 |
| P1-K2 | 路径解析：先词法规范化（算掉 `.` 和 `..`）再比根；父目录不存在时 `..` 同样拒绝；一次打开，fstat 与读取在同一文件句柄上，拒绝跟随符号链接（Linux 用 openat2 / cap-std） | 一致性用例：`ws/不存在的目录/../../etc/x` 被拒；符号链接指向根外被拒；打开后替换文件不影响已校验句柄 |
| P1-K3 | gld 与 ccnm 的读取改为**真正调用** K1/K2，各自保留原有输出语义 | gld：`call_tool_contract`、`call_tool_security` 通过；ccnm：`cargo test -p ccnm-core --lib mcp::read`、`--test mcp_read_file`、Python 中立客户端通过；ccnm 超长单行内存问题随之修复 |
| P1-K4 | CI 断言"工具描述里写的默认值和上限 = 代码常量" | 故意改一个常量，CI 失败 |

### P2 先拿大头

#### P2-C Codex exec-server 产品化（ccnm 新阶段）

| 编号 | 做什么 | 验收 |
| --- | --- | --- |
| P2-C1 | `ccnm internal exec-serve`：把 P1-X1 的脚本逻辑并入 ccnm；内部协议版本号 +1 | `cargo test -p ccnm-core --lib runtime::` 与 `--test runtime_open` 通过；旧版本号拒绝而不回退 |
| P2-C2 | Codex provider 新增 exec-server 模式（默认仍为 MCP 模式，配置开关切换）；两端 Codex 版本精确一致，不一致启动前拒绝 | 版本不一致时返回明确错误码 |
| P2-C3 | Linux Runtime 实测 bwrap 沙箱；复测 codex issue #33820（删除在本机执行）、#32919（远端绕过审批）、#40306（断线挂起）、#45429（初始化超时） | 每个 issue 有复现记录与结论；#33820 若在 0.154.0 仍存在则停止切换 |
| P2-C4 | A/B：exec-server 模式对 MCP 模式，Codex 3 任务 × 5 次 | 满足第 7.4 节才把默认切到 exec-server；更新 support-matrix |
| P2-C5 | Runtime 出网：记录 `http/request` 能访问任意地址，写进 production-safety；不在本阶段做防火墙 | 文档更新 |

#### P2-H gld hub 只读接 ccnm（gld）

| 编号 | 做什么 | 验收 |
| --- | --- | --- |
| P2-H1 | 鉴权主体（AuthContext）从 HTTP 层传进 hub；成员分 Local / CcnmRuntime 两类，本地成员配置原样兼容 | RFC-0002 H01、H02 |
| P2-H2 | 静态发布 `remote_workspace_info`、`remote_read_file`、`remote_list_files`、`remote_search_text`，schema 照抄 `workspace-mcp/1` 并加 `workspace` 参数；结果原样透传 `content/isError` | RFC-0002 H03、H04；上游新增工具不自动出现 |
| P2-H3 | read bridge 进程按（主体, 成员）复用，空闲超时关闭；只用公开命令 `ccnm mcp bridge <ws> --node <n> --mode read`，以 argv 启动 | RFC-0002 H05；用 ccnm `fixtures-mcp/` 做的合成对端测试通过 |
| P2-H4 | 真实闭环：ChatGPT 网页 → gld hub → bridge → Runtime 读与搜索 | 脱敏记录；模型只看到一份结果 |

### P3 编辑、提交、执行进内核

#### P3-K 内核（本仓库）

| 编号 | 做什么 | 验收 |
| --- | --- | --- |
| P3-K1 | 编辑引擎：带游标四档匹配（精确 → 去尾空白 → 去首尾空白 → Unicode 标点归一），命中档内必须唯一；所有 edit 对原文匹配、区间重叠拒绝；CRLF 与 BOM 保留；去空白档命中时按缩进差修正 new_text 并注明 | 移植 codex 的 27 个补丁场景 + 重复片段、CRLF、BOM、重叠区间、缩进修正用例 |
| P3-K2 | 失败文案：第几个 edit、所有命中行号（多处时）；未命中时给最近似片段并可视化空白（≤20 行，文件原文，不回显模型输入）；诊断计算有时间上限 | 构造 16MiB 文件 + 1MiB old 的失败编辑，耗时 <1s（ccnm 曾测到 57.7s） |
| P3-K3 | 读取记录与陈旧判定：服务端记 {路径: hash, size, mtime, 是否整读}；读后被改但 old 仍精确唯一 → 放行并注明；读后被改且只是模糊命中 → 拒绝；write/delete 要求读过整个文件 | 模拟格式化器并发改文件的用例 |
| P3-K4 | 补丁解析：codex `*** Begin Patch` 宽松解析（按许可搬入）；识别 unified diff 时给一次性纠正提示；数组参数被写成 JSON 字符串时照收 | codex parser 测试随代码搬入并通过 |
| P3-K5 | 事务提交层：从 ccnm 抽出 stage → journal → fsync → rename，中途中断下一次拒绝 | ccnm patch 全部测试通过；Windows 替换语义单测 |
| P3-K6 | 执行会话：进程树管理（参照 webcodex-process 搬入，wait 改异步）、防 PID 复用（确认整树退出后不再发信号）、1MiB 头尾缓冲 + 全文落盘 + seq 游标长轮询、`write_id` 去重、code 与 signal 分字段、超过 yield 阈值转后台并先探测 1 秒快速失败、默认注入 `PAGER=cat` `GIT_PAGER=cat` `GIT_EDITOR=true` `GIT_TERMINAL_PROMPT=0` 且 stdin 为空、给模型的输出去 ANSI 并折叠 `\r`（落盘保留原始字节）、四种结束状态固定文案、cargo/go 测试只摘失败用例（≤20 条、每条 ≤240 字符） | 第 6.3 节 13 项测试全部通过；Windows 编译并实测 Job Object 路径 |
| P3-K7 | 重复调用提示：同一会话内工具、入参、结果完全相同的调用在 10 次内出现超过 5 次，结果附"换个思路"提示 | 单测 |

#### P3-G gld 迁移（gld）

| 编号 | 做什么 | 验收 |
| --- | --- | --- |
| P3-G1 | 补丁定位换成 K1 引擎（修"每个 hunk 从文件开头找"） | 先写失败测试（`old\nmiddle\nold\n` 改第三行），迁移后通过 |
| P3-G2 | 写入换成 K5 提交层（补 fsync 与 journal） | 中断注入测试 |
| P3-G3 | exec 与 git 子进程换成 K6（修"超时只杀主进程""git 超时参数被丢弃""命令结束后输出引用读不到"） | 各自先写失败测试 |
| P3-G4 | 命令权限匹配器升级：按 shell 语法拆子命令，全部命中 allow 才放行；解析失败不认 allow；配置了规则时拒绝 `$VAR`、`$(...)`；优先级 内置拒绝 > deny > confirm > allow > 默认；`.env*` `*.pem` `*.key` 默认禁读 | `call_tool_security` 增加绕过用例：`git status; rm -rf x`、`ls & rm x`、`timeout rm x` 均不被 allow 放行 |

每个迁移任务单独提交；行为变化在提交说明里单列。

#### P3-N `native@1`（本仓库 + ccnm）

| 编号 | 做什么 | 验收 |
| --- | --- | --- |
| P3-N1 | 工具集：Read、Edit、Write、Grep、Glob、Bash、TaskOutput、TaskStop，参数名与默认行为对齐 Claude Agent SDK 导出的类型（例如 `file_path`、`old_string`、`replace_all`、`run_in_background`） | 体积 CI：整个 tools/list ≤8KiB、单工具 ≤1.5KiB |
| P3-N2 | ccnm 只对托管 Claude 会话提供 `native@1`，外部 MCP 入口仍是 `workspace-mcp/1` | 外部入口测试不变 |
| P3-N3 | 文档写明：工具名是 `mcp__ccnm__Read` 等，用户在 Claude Code 里按原生名写的权限规则和 hooks 不会命中；安全边界在 Runtime | support-matrix 更新 |
| P3-N4 | 实测子代理（Task）能否使用这些 MCP 工具 | 记录结论；不可用则在 instructions 中说明 |
| P3-N5 | A/B：`native@1` 对 P1-C 之后的 7 工具，外加本机原生基线；同时跑实验 E1、E2、E5（第 7.3 节） | 满足第 7.4 节才设为托管 Claude 默认 |

### P4 补齐

| 编号 | 仓库 | 做什么 | 验收 |
| --- | --- | --- | --- |
| P4-K1 | 本仓库 | 搜索：grep-searcher 库，默认只列文件名，读够 limit+1 行即停并写"至少 N 条"，按文件分组，单行 ≤512B；命中 1–3 条时自动补上下文（Gemini 称 SWEBench 轮次约 −10%） | 与 rg 结果差分一致（去重排序后） |
| P4-K2 | 本仓库 | 列表与目录树：ignore 库；按字符预算广度优先展开，放不下的目录折叠成一行统计；glob 24 小时内改过的排前 | 大仓库夹具输出 ≤8KiB |
| P4-K3 | 本仓库 | 读取补充：文件不存在附同目录相近文件名（≤3 个）；带范围读取同样受上限；实验 E3 通过则加 tree-sitter 大纲 | 单测；E3 结论 |
| P4-L1 | 本仓库 + gld | `lean@1`：workspace_info、read_file、list_files、search_text、edit_file、apply_patch（codex 文本格式）、exec_command、read_output，可选 write_stdin；只返回纯文本，不发 structuredContent | gld 以可选工具集上线；ChatGPT 实测模型只看到一份；token 对比 gld 默认集有数据 |
| P4-H1 | gld | hub coding：`remote_coding_begin/end` 显式租约 + `remote_apply_patch`、`remote_exec_command`、`remote_read_output`；同会话串行；空闲到期关闭；不重放 | RFC-0002 H06–H08 |
| P4-D1 | 本仓库 | 可选：编辑后诊断，只附当前文件 Error，≤10 条，等待策略"1 秒无变化返回 / 300ms 静默 / 5 秒封顶" | 依据实验结果决定是否做 |

### P5 开放复用

| 编号 | 做什么 | 验收 |
| --- | --- | --- |
| P5.1 | 发布 crates.io（名称可用性待查）；两个产品改为版本依赖 | `cargo publish --dry-run` 通过；产品 CI 通过 |
| P5.2 | `wk serve --stdio|--http`：非 Rust 项目直接起进程用 | 用中立 MCP 客户端跑一致性测试集通过 |
| P5.3 | 接入指南：怎么实现三个 trait、怎么选工具集、怎么跑一致性测试集 | 找一个示例项目只靠文档接入成功 |
| P5.4 | ccnm：若 `lean@1` 数据明显优于 `workspace-mcp/1`，按冻结规则评估 `workspace-mcp/2` | 走 ccnm 契约升级流程 |

---

## 6. 工具面规范（草案，P1 起生效，P3/P4 补全）

本节在 P0.3 之后移到 `docs/spec/tool-surface.md`，本文只留链接。

### 6.1 通用规则

| 项 | 规则 | 依据 |
| --- | --- | --- |
| 结果 | 只返回一个 `content` 文本块，不发 `structuredContent`；UI 专用数据放 `_meta` | Claude Code、Codex 只看 structuredContent；gemini/grok/goose/OpenHands 只看 content；ChatGPT/qwen/cline 两份都发。只发文本时所有客户端正好一份 |
| 格式 | 文件内容不放进 JSON 字符串；读取带行号 `N→`；搜索按文件分组 `行号:文本` | 同一 50 行：纯文本 451 tok、带行号 570、JSON 转义 606、双份 1,294（o200k 实测） |
| 状态行 | 截断/续读信息写在**第一行** | 部分客户端只保留头部 |
| 错误 | `isError` + 一行 `CODE: 原因; next: 下一步` | — |
| 描述 | 每个工具 ≤400B，硬上限 1KiB；首句说"做什么、何时用它"；必含前置条件、默认上限与续读方式、常见失败与对策、能力边界；不复述参数、不放通用行为规范 | Claude Code 截断描述到 2KB |
| schema | 必填最少；无 `$schema`、`default:null`、`format`、可空联合；嵌套 ≤2 层 | ccnm 现有噪音 1,140B（13.7%） |
| 加载 | 高频工具设 `_meta["anthropic/alwaysLoad"]: true`；主编码面全部常驻；Codex 用户文档写明 `omit_tools_from=["deferred"]` | Claude Code 默认延迟加载 MCP 工具；Codex 对 MCP 工具无阈值地延迟 |
| 顺序 | tools/list 顺序固定、会话中不变 | 提示缓存命中 |
| instructions | ≤2KiB；顺序：用途 → 说明文件路径清单 → 标记行 → 正文 | Claude Code 截断到 2KB（P1-C1 待实测） |
| 输出预算 | 单次结果默认 ≤16KiB，硬上限 32KiB | Claude Code 25k token 上限、Codex 约 12k token 截断、cline 每字符串 8k 字符 |

### 6.2 统一文案

```text
读取截断：[partial: lines 1-250 of 1834 (limit 250, 16KiB); continue: offset=251; do not re-read this range]
搜索截断：[at least 200 matches; narrow path/glob, or offset=200]
执行完成：exit 0 · 1.8s
执行转后台：running in background · id b3 · 120.0s · 18.2KiB so far; read: TaskOutput(b3); stop: TaskStop(b3)
执行截断：exit 101 · 48.7s · 3.1MiB (head 6KiB + tail 10KiB); summary: 212 passed, 2 failed: a::x, b::y; more: read_output id=s5 offset=6144
编辑成功：OK src/a.rs: 2 replacements at L41, L88
编辑歧义：src/a.rs edit 1: old_string appears 3 times (lines 12, 40, 88); add surrounding lines or set replace_all
文件未变：[unchanged since version v; not resent]   ← 只在调用方显式传 if_version 时返回
用户中止：stopped by user; ask the user before retrying
```

### 6.3 执行会话必须通过的测试

1. 头尾缓冲边界，含多字节字符跨块。
2. `sh -c 'sleep 100 & echo hi'` 及时返回，之后 terminate 能杀掉 sleep。
3. 超时杀进程后整个组无存活。
4. 转后台后可续读、可停止。
5. 整树已退出后 terminate 不发任何信号。
6. 200MiB 输出不 OOM、不因管道写满卡住。
7. 同一 `write_id` 重试不重复写 stdin。
8. 关闭 stdin 后 `cat` 退出；stdin 为空时读到 EOF。
9. 会话数超过上限触发回收，淘汰顺序正确。
10. SIGSEGV 显示为 `signal 11`，不是 139。
11. `\r` 进度条被折叠，落盘保留原始字节。
12. cargo test 多段汇总时失败数正确累加。
13. Windows：放入 Job 不留空窗；挂起创建后能恢复。

### 6.4 默认预算（初值，P3/P4 由实验修正）

| 工具 | 默认 | 上限 |
| --- | --- | --- |
| 读取 | 250 行 / 16KiB；单行 1000 字符 | 2000 行 / 32KiB |
| 搜索 | 文件名模式 250 条；内容模式 200 行 / 16KiB | 2000 行 / 32KiB |
| 列表/目录树 | 8KiB | 32KiB |
| 执行内联 | 成功 8KiB、失败 16KiB，头 40% / 尾 60% | 32KiB |
| 执行 yield | 10s | 30s |
| 前台超时 | 120s | 600s |
| 后台最长运行 | 2h | 10h |
| TERM → KILL 宽限 | 1s | 5s |
| 编辑结果 | 成功 1 行；失败 ≤4KiB | — |

---

## 7. A/B 评测设计

### 7.1 任务

| 任务 | 内容 | 自动判分 |
| --- | --- | --- |
| T1 修测试 | 在固定快照里注入一个会让某个测试失败的 bug | 该测试退出码 0，且 `git diff` 只改了注入 bug 的文件 |
| T2 代码分析 | 回答"写锁在哪里获取、在哪里释放"，给出文件:行号 | 与标准答案行号对比，命中 ≥2 处且无错误行号 |
| T3 大输出 | 跑全量测试，报出注入的 2 个失败用例名 | 报出的名字集合与注入集合完全一致 |

夹具放在 `bench/fixtures/`，每次运行前重置到同一提交。

### 7.2 控制变量

CLI 版本锁定（Codex 0.154.0，Claude Code 记录版本号）；模型与推理强度相同；只保留一份根说明文件；关闭插件与 hooks；提示词不点工具名；各组随机交替运行，避免缓存冷热不均；每组至少 5 次，报中位数与最小/最大值。

### 7.3 实验清单

| 编号 | 问题 | 对照组 | 任务 | 运行次数 | 阶段 |
| --- | --- | --- | --- | --- | --- |
| E0 | 基线 | 本机原生 vs 经 ccnm（Claude、Codex） | T1–T3 | 60 | P1 |
| E0′ | 快速收益 | P1-C 之后 vs 之前（Claude） | T1–T3 | 15 | P1 |
| E4 | Codex 执行方式 | exec-server vs MCP | T1–T3 | 15 | P2 |
| E1 | 读取默认预算 | 250 行/16KiB、1000 行/48KiB、200 行/200KiB | T2 | 15 | P3 |
| E2 | 搜索默认模式 | 只列文件、给内容、命中 ≤N 文件时自动给内容 | T2 | 15 | P3 |
| E5 | `native@1` | native@1 vs 7 工具 | T1–T3 | 15 | P3 |
| E3 | 大文件大纲 | 部分内容 vs 部分内容 + 大纲 | T2 变体（目标在大文件里） | 10 | P4 |
| E6 | 依赖型多处编辑占比 | 回放已记录会话，离线统计 | — | 0 | P3 |
| E7 | 第五档模糊匹配 | 四档 vs 加行级相似度 | 离线真实失败样本 | 0 | P3 |
| E8 | 断线宽限 | 断线即清 vs 保留 60–120s 续传 | 模拟断线 | 0 | P4 后另立项 |

合计约 145 次模型运行。按 ccnm 已有记录的单任务折算费用 $0.10–$1.43，约相当于 $40–$120 的订阅额度（粗估，已获同意）。

### 7.4 采纳规则

新方案同时满足以下三条才替换默认：

1. 成功次数 ≥ 对照组（5 次里失败不多于对照组）。
2. 总 token 中位数 ≤ 对照组。
3. 工具调用次数中位数 ≤ 对照组。

只满足部分时不切默认，记录结论，留作可选项。E6 若依赖型编辑占比 >2%，编辑引擎加兜底：对原文匹配失败时改按顺序匹配，但仍整体原子提交。E8 会改变 ccnm 已冻结的"没有 resume"规则，数据支持也要单独立项。

---

## 8. 风险与应对

| 风险 | 影响 | 应对 |
| --- | --- | --- |
| Codex exec-server 标 EXPERIMENTAL，协议 2026-06-23 至今 44 次提交 | 升级 Codex 时可能断 | 两端版本精确钉死；每次换版本重跑 P1-X 探测与 E4；保留 MCP 模式开关 |
| `native@1` 数据不比现状好 | Claude 侧收益落空 | 保留 P1-C 快速收益作为下限；不设默认 |
| 内核抽取改变冻结契约行为 | 外部用户受影响 | 旧契约只做适配层；ccnm golden fixture 与 Python 中立客户端不重录必须通过 |
| Windows 进程/PTY 行为 | gld Windows 用户 | 管道模式先行，PTY 仅 Unix；Windows 单独实测后再开 |
| 订阅条款变化 | 整体路线 | 只使用未修改的官方二进制；每次大版本前复查 legal-and-compliance 页面 |
| 单人维护三个仓库 | 进度拖慢 | P2 与 P3 可并行但同一时间只推进一条写代码的主线；每阶段结束更新本文第 10 节 |
| 5 次运行样本小 | 结论有噪声 | 报最小/最大值；结论接近时追加 5 次，不改采纳规则 |

---

## 9. 已知缺陷队列（独立修复，不夹带进迁移）

| 仓库 | 缺陷 | 证据 | 计划 |
| --- | --- | --- | --- |
| gld | `run_git` 收了超时参数后 `let _ = limit` 丢弃 | `crates/core/src/tools/git.rs:476-503` | 立即修；P3-G3 最终换内核 |
| gld | unified diff 每个 hunk 从文件开头找匹配 | `crates/core/src/tools/patch.rs:335-407` | 先写失败测试；P3-G1 |
| gld | 超时只杀主进程 | `crates/core/src/tools/session.rs:223-231` | P3-G3 |
| gld | inline 正常结束立即移除 session，返回的输出引用可能读不到 | `crates/core/src/tools/exec.rs:309-324` | 先复现；P3-G3 |
| gld | schema 上限在代码里没收紧；search 默认值 schema 100 对代码 1000 | `registry.rs`、`file.rs:243` | P4-L1 |
| gld | Cargo.toml 声明 Apache-2.0，仓库无 LICENSE 文件 | 仓库根目录 | P0.5 |
| ccnm | 读取超长单行先整行进内存再查 64MiB 上限 | `crates/ccnm-core/src/mcp/read.rs:298` | P1-K3 |
| ccnm | 协议文档 exec 预览"头尾各 16KiB"与代码（默认总 4KiB、上限总 16KiB）不符；patch"单文件 1MiB"与代码（整次请求合计）不符 | `docs/protocol/remote-workspace-mcp-v1.md:256-258` | 立即修文档 |
| ccnm | AGENTS.md 仍写 Remote Workspace MCP "experimental、无真实 Host 验证"，实际已于 2026-09-11 冻结 | `AGENTS.md:22` | 立即修文档 |
| ccnm | instructions 可能被 Claude Code 截到 2KB，清单与标记行在末尾会先被截 | `provider/claude/context.rs` | P1-C1 实测，P1-C3 修 |

---

## 10. 进度记录

| 日期 | 阶段 | 状态 | 证据 |
| --- | --- | --- | --- |
| 2026-09-15 | 计划 v1 | 已确认 | 本文件首个提交 |

每完成一个任务编号，在此追加一行：编号、结论、提交或证据文件。产品仓库各自的状态文件（如 ccnm `docs/plan/status.json`）只记本产品任务，并链接到这里。

---

## 11. 第一周动作清单

1. P0.1–P0.3：统一 1.89 + MSRV CI；建本仓库骨架（安装 1.89 工具链前先确认）。
2. P0.4–P0.5：RFC-0002 顶部加指向说明；gld 补 LICENSE。
3. 第 9 节里标"立即修"的三项（gld git 超时、ccnm 两处文档）。
4. P1-B1–B3：评测夹具、运行器、缓存断裂统计。
5. P1-X1–X4：Codex exec-server 无模型 spike。
6. P1-B4：跑基线 E0（这一步开始消耗额度）。

---

## 12. 参考来源与复用方式

| 来源 | 许可 | 复用方式 | 用在哪 |
| --- | --- | --- | --- |
| openai/codex（`apply-patch`、`unified_exec/head_tail_buffer.rs`、`exec-server`） | Apache-2.0 | 搬代码（补丁解析、匹配阶梯、头尾缓冲）；exec-server 直接用官方二进制 | P3-K1、K4、K6；P2-C |
| yyjeqhc/webcodex（`webcodex-process`、`apply_patch_shared` 唯一匹配、测试输出解析） | Apache-2.0 | 搬代码 | P3-K1、K6 |
| xai-org/grok-build（目录折叠、二进制嗅探、行截断、ProcessScope 思路） | Apache-2.0 | 搬代码 + 借思路 | P3-K6、P4-K2 |
| sst/opencode、pi-mono、kimi-code | MIT | 借思路（续读文案、多 edit 对原文匹配） | P3、P4 |
| qwen-code、gemini-cli、cline、goose、OpenHands、vtcode | Apache-2.0 / MIT | 借思路（陈旧检测、自动补上下文、截断比例、非交互环境） | P3、P4 |
| Zed | GPL | 只借思路（缩进修正、大纲、终端清洗、子命令审批） | P3-K1、K6，P3-G4，P4-K3 |
| Crush | FSL | 只借思路（最近似匹配可视化、自动转后台、非交互环境、重复调用检测） | P3-K2、K6、K7 |
| claw-code | 来源有争议 | 只借通用工程思路，作为反面教材（路径越界、超时不杀进程） | P1-K2、P3-G4 |
| ignore、grep-searcher、grep-regex、cap-std、process-wrap、portable-pty、vte、diffy、similar、rmcp、tree-sitter | 各自开源许可 | 直接依赖 | 各阶段 |

---

## 13. 未验证事项

本计划中的以下内容目前只有源码阅读或文档依据，没有运行证据，实施时以实测为准：

- 所有 token / 轮次收益（除第 6.1 节 o200k 实测的格式对比）。
- `codex exec` 经 SSH 连接远端 exec-server 跑完整回合；Linux 远端 bwrap 沙箱。
- Claude Code 对 instructions 的 2KB 截断在 ccnm 上的实际影响。
- ChatGPT 是否对 content 与 structuredContent 双重计费。
- Crush 命令白名单绕过、claw-code 路径越界写（都是读代码推断）。
- Windows 上的进程树与 PTY 行为。
- crates.io 上 `workspace-kernel` 等名称是否可用。
