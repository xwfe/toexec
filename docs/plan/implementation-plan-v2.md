# toexec 实施方案 v2：共享库为主，收窄 exec-server 复用范围

日期：2026-09-15。状态：**实施中**——V2-K、V2-H、V2-Q 已完成，V2-C 的离线部分已完成（ccnm P21–P23），真机验收 P24 待授权，Claude 实验线 V2-P0–P5 未开始；逐项进度见第 11 节的状态表。

本次修订：gld 本地不接 exec-server；Claude 复用 exec-server 从默认架构降为独立、无模型的收益验证。Codex 原生 exec-server 路线保留。

**用户决定（2026-09-15）**：① 三个仓库统一 `rust-version`，执行时点见第 11 节；② 同意为本计划的模型验证消耗订阅额度，记账与上限见第 10.1 节。

本版是当前方案讨论入口。[v1 原文](implementation-plan.md)保持不变，供差异追溯；其“已确认”“已获同意”和阶段编号不自动成为 v2 的实施或费用授权。两个产品的实际进度仍分别由各自仓库记录，特别是 ccnm 的 `docs/plan/status.json`。

## 0. 一页纸结论

**共享 Rust 库和 gld hub 接 ccnm 是主线；Codex 使用自己的 exec-server；Claude 是否复用它，由额外约束收益与实际成本决定，不预设答案。**

- Codex：保留官方 Agent，通过其原生执行协议使用远端 exec-server。
- Claude Code：保留官方 Agent，默认经 ccnm MCP 使用直接执行/共享库路径；exec-server 仅作为分块读取、进程管理和沙箱的可选验证后端。
- Web AI：仍由 gld hub 接 ccnm 公共 MCP bridge；Web AI 自行分析，不在后台额外启动 Agent。
- gld 本地工具：**走共享库，不走 exec-server**。安装、发布、Windows 能力与交互会话不依赖 Codex 二进制或实验执行协议。
- `toexec`：共享有界读取、编辑/提交、输出与进程原语，优先提取既有实现和复用成熟组件；exec-client 独立用于 Codex 原生接入或 Claude 实验，不成为基础库或 gld 本地的传递依赖。

**MCP 可以接不同执行器，但协议能接通不等于整体值得替换。** V2-P1 保留无模型验证；只有它证明收益大于成本，才继续 Claude 专用接入。验证不通过就维持共享库路径，不阻塞 hub 或自动扩大执行端 fork 范围。

不增加 WebCodex Server、中心账号服务、自己的模型客户端或公开 HTTP 执行服务。这里的 exec-server 是由现有 Runtime 包装进程托管的子进程，不是新增一套产品控制面。

## 1. 相比 v1 的调整

| v1 | v2 |
| --- | --- |
| Codex 用 exec-server，Claude 的完整执行能力另写 | gld 与 ccnm 直接执行路径共用库；Claude 可单独验证复用 exec-server 的少量机制，不据此重写/替换全部工具 |
| 先统一三个仓库 Rust 1.89，再开展全部工作 | 仍统一三个仓库的 `rust-version`，但不提前动：第一个共享 crate 被 gld 或 ccnm 链接时一次性统一（第 11 节） |
| 预先建设完整 Root、编辑、执行会话、MCP 与工具集 crate | 共享库先提取真实重复的小模块；Claude 的 Read + Process 试验独立进行，不先建全套空框架 |
| 拿 guard 后直接 `exec` 官方服务 | 包装进程持有 guard，监督子进程；确认写进程结束后释放，不丢弃锁与收尾责任 |
| WebSocket 只接受首连接 | 验证连接身份或不可伪造的连接能力；首连接限制不是认证 |
| 默认工具迁入模糊编辑、原文批次与新路径规则 | 旧模式保留各自语义；新语义进新工具集，已确认缺陷独立修复 |
| 以三组模型请求 hash 归因缓存变化 | 只记录官方输出实际可见的字段与自己控制的 MCP 输入；不可见字段标记 unavailable |
| 先安排约 145 次模型运行 | 前期验证零模型；硬门禁通过后按明确预算安排少量真实闭环与 A/B |
| 以成功次数/token/调用次数三条决定默认切换 | 正确性、安全、恢复、兼容先过关，再比较资源消耗；完全失败的方案不能因便宜而通过 |
| `wk serve --stdio|--http` 列入本轮交付 | 当前不做新的通用服务；未来有实际消费者再立项 |

## 2. 固定证据与待证事项

### 2.1 首轮源码基线

| 对象 | 固定版本/提交 | 证据级别 |
| --- | --- | --- |
| Codex execution RPC | `rust-v0.154.0` → `6b9826e3aa83b1a5947db50f4332cb9c65f1b340` | 已做公开源码核查；未完成本项目双入口运行验证 |
| gld | `521386afa93ba1a8200d1c5ce5928e0a785df831` | 当前兼容基线；实际实施前再次核对工作区 |
| ccnm | `8205bc2a2c0f777853a5ff574bdc6cfbcf108a98` | 当前身份、guard 和公开契约基线 |
| WebCodex 参考源码 | `e11cb3e4c96b8b4fa9f84ca59fa072ca0bb0b2ae` | 本地只读参考；不是运行依赖 |

构建物的版本、SHA、平台、feature、配置和协议 schema 另行登记；不能从 README 例子或分支名推断实际二进制行为。上游换版本，需重新运行受影响门禁。

### 2.2 exec-server 的可复用能力

`codex exec` 是模型任务入口；`codex app-server` 是 Agent 应用控制接口；本方案讨论的是 **`codex exec-server` 执行 RPC**。首轮限定受控 stdio/本地连接，不采用其需要额外身份配置的 remote relay 注册路线。

| 能力 | 固定版本源码事实 | 适配含义 |
| --- | --- | --- |
| 协议 | stdio/WebSocket；Codex JSON-RPC 方言，非 MCP | 必须适配握手、工具 schema、事件和错误，不能只转发 MCP JSON |
| 分块读 | `fs/open`、`fs/readBlock`、`fs/close` | 可构造有界行读取；句柄不是不可变快照，仍需处理文件原地变化 |
| 整文件读 | `fs/readFile` 返回整份 base64，实现上限 512 MiB | 不能包装成“只读几行”的有界实现；大文件首选分块接口 |
| 文件写 | 有 `fs/writeFile`，无 expectedVersion/CAS 参数 | 直接覆盖不等于受控 Edit，也不等于 ccnm 提交/journal |
| 编辑与搜索 | 无直接 patch、文本搜索、rename 或多文件事务 RPC | 必须保留/补足相应语义；`copy + remove` 不冒充原子 rename |
| 进程 | start/read/write/signal/terminate、输出与退出事件 | 需要 session 绑定、预算、超时与完成判定；实际 `process/write` 要求 `writeId` |
| 交互缺口 | 当前路由没有独立 closeStdin、PTY resize RPC | gld 本地明确不接入；Claude 试验也不能宣称完整交互会话等价 |
| 断线 | WebSocket 有约 30 秒 detached/resume 窗口；stdio 结束走 shutdown | 不得静默改变 ccnm v1 的无 resume 语义 |
| 沙箱 | 文件与 process 请求可带 sandbox；部分无策略路径直接执行 | 服务名称不构成隔离保证；两种前端都必须受相同权限上限约束 |

主来源见第 12 节。上述是源码能力，不表示凭据、事务、断网、Windows 或真实 Agent 已经验证。

对当前上层工具有实际复用价值的重点是分块读取、进程管理及其可施加的沙箱。它还提供原始写入、遍历等 RPC，不能字面概括为“只有两类 RPC”；但这些不是现成搜索、Edit、条件写、journal、读取记录或符合现有保留策略的输出落盘实现，上层仍需共享库承担。

### 2.3 Claude 的接入边界

公开 MCP 是优先入口；未确认存在能整体替换 Claude 原生 Read/Edit/Bash 后端的公开配置。MCP 工具即使叫 `Read`，也不是自动重定向原生工具。

托管 Claude 沿用 ccnm 已验证的本地工具限制，模型调用与订阅登录仍由官方 CLI 管理。外部 Host 的本地工具是否关闭由 Host 控制，不能宣称 ccnm 能消除所有旁路。

Claude Desktop SSH、Remote Control、Cowork 可以参考宿主/执行分层，但不把未公开的 Desktop/broker wire protocol 当作本项目稳定接口。PostToolUse 修改结果发生在原工具执行之后，不用它进行“远端再执行一次”的伪透明替换。

## 3. Claude 专用验证：收益必须覆盖代价

本节不决定 gld 本地架构，也不重新决定 Codex 原生路线。Claude 候选的权重为：新增且可证明的约束/机制收益 35%，旧契约与安全不退化 30%，部署及升级维护成本 25%，运行开销 10%。无运行数据不编造总分，硬门禁失败直接否决。

| 选项 | 做法 | 复用/维护 | 兼容边界 | 决策 |
| --- | --- | --- | --- | --- |
| **主线：直接执行 + 共享库** | gld 本地和 ccnm MCP 使用共享原语 | 无额外 Codex 运行依赖，减少已有重复 | 各产品适配保留原语义 | **默认建设方向** |
| A：Claude 委派少量操作 | MCP adapter 将分块读/进程操作交给官方 exec-server | 可复用进程/沙箱，但多一跳 RPC 和实验协议维护 | 搜索、编辑、提交、输出存储仍在共享库 | **只保留 V2-P1 验证，非默认采用** |
| B：仅派生执行端 | 为已确认高价值缺口添加最小扩展 | 承担额外 fork、构建、升级和回归成本 | 新 RPC 不会自动被官方 Codex 使用 | 需独立收益依据，不由 A 失败自动触发 |
| gld 本地接 exec-server | 所有本地操作经 Codex 子进程 | 增加安装/发布、Windows 和版本耦合 | 缺 closeStdin 等会伤及既有会话 | **本版排除** |

### 3.1 成本与版本边界

| 成本 | 必须记录的内容 |
| --- | --- |
| Runtime 依赖 | Codex 二进制的取得、校验、平台可用性、占用和更新；分别评估已有 Codex 安装与全新安装，不能把已有安装视为永远零成本 |
| 实验协议 | 固定 schema 的客户端代码、升级差异、回归与恢复成本；不用未经本轮核查的提交次数代替实际兼容分析 |
| 版本联动 | Codex 原生链首轮按客户端/服务端同版本验证；Claude 则锁定 MCP adapter 与后端版本的兼容矩阵，不使用随全局 PATH 自动变化的二进制 |
| 多版本运维 | Claude 可独立 pin 后端，因此并非每次 Codex CLI 升级都必须改变 Claude；若选择独立 pin，要计入并存、部署和升级测试成本 |
| RPC 与编码 | 额外 stdio JSON、base64、复制、请求数量、CPU、RSS、延迟和字节量；base64 不应原样送入模型，内部传输字节不直接等于模型 token |
| 未消失的实现 | 搜索/编辑/条件写/journal/读取记录/输出落盘仍需维护，不把整个工具层代码量算成复用收益 |

### 3.2 V2-P1 必须回答的问题

**Claude 路径经过 exec-server，获得的额外安全约束和机制复用收益，是否大于部署、实验协议、版本、RPC 以及维护成本？**

用中立 MCP 客户端代替 Claude 模型，固定相同 Runtime OS 身份、项目、文件权限、命令、环境、输入和输出预算。对照当前 ccnm 直接执行路径与委派路径；必要时在隔离环境加一组 exec-server 无附加沙箱的对照，以区分 RPC 开销和沙箱作用，不在真实项目上放宽策略。

重点验证：在不扩大 ccnm 权限的前提下，是否出现原路径没有的、可复现的 OS 层读/写/进程/网络限制。只由 MCP 参数校验拒绝的请求，不能计作新增沙箱能力。测试使用合成文件、可访问的测试 canary 与受控网络目标，不读取真实秘密。

每项新增约束给出“基线能做、委派后按预期被执行端阻止”的对照证据，并分别覆盖文件 RPC、进程及已打开句柄；不以某一次 shell 越界失败推广到所有入口。合法项目操作和旧契约仍须通过。

试验前冻结各场景开销上限、可接受的依赖/升级成本、最小所需新增约束及安全否决项。结果必须是以下之一：维持直接执行；仅继续验证进程/沙箱；继续验证进程加分块读取。握手成功、两个前端读到同一文件，只说明可连接，不满足收益验收。

没有可证明的新增约束，或成熟库能以更低成本提供同等保护时，优先共享库。不要为了“统一引擎”勉强接入；否决该候选也算 V2-P1 完成。

### 3.3 A 的范围与 B 的准入

A 最多先委派文件分块读取和进程执行，也允许只保留进程/沙箱候选。编辑、提交、读取记录和输出存储继续由直接路径/共享库承担，这是明确架构边界，不是承诺以后必然迁入 exec-server。

不能用两个 RPC 的 `read → 比 hash → write` 伪装 CAS，不能用客户端补偿回滚冒充执行端多文件事务。需要条件提交时，应在执行端同一受控路径实现，并明确它约束哪些合作写者、是否能覆盖外部编辑器。

只有 A 的收益已经得到证据支持，且某个局部缺口阻断这项收益时才评审 B。写明失败用例、最小原语、维护上限，与共享库/保留直接路径比较；不能因为它缺少整套编辑能力，就自动扩展成另一套通用执行服务。B 不改变“gld 本地不接 exec-server”的范围。

不 fork Claude/Codex 的模型循环、登录或提示词。不把 Codex、Grok、ccnm 三套落盘代码并排塞入一个仓库后称为统一。新 RPC 只覆盖会实际使用它的前端；官方 Codex 仍走原生调用链，须独立验证其保护范围。

## 4. 目标调用链与责任归属

```text
gld 本地工具 ── gld 策略 ──────────────────────→ 共享 Rust 库

官方 Claude ── MCP ── SSH stdio ────┐
                                    ├─ ccnm Runtime（身份 / root / writer guard）
Web AI ── gld hub ── 公共 bridge ────┘   ├─ 默认：直接执行 / 共享 Rust 库
                                        └─ 可选实验：分块读或进程委派
                                             → 受管 exec-server

官方 Codex ── 原生执行协议 ── ccnm 监督包装 ──→ 受管 exec-server
```

共享库减少 gld 与 ccnm 直接路径的代码重复；Codex 原生链单独采用官方执行端。Claude 的实验分支不要求使用与 Codex 原生链相同的进程或后端版本。一个物理 workspace 的 coding 会话仍竞争同一写权，不能不受约束地同时修改同树。

gld 本地的安装包、默认依赖、Windows 构建与交互会话均不引入 Codex 二进制/exec 协议。gld hub 只调用 ccnm 公共接口，不关心远端是否选择了实验 backend。

| 责任 | 权威位置 |
| --- | --- |
| 模型、订阅登录与官方 Agent 会话 | Agent 端官方 CLI；不转移给执行端 |
| root、OS 身份、配置、最大权限、写权 | ccnm / gld 产品受控边界；不是模型参数 |
| exec 协议版本、请求/事件相关性、句柄生命周期 | Codex 原生接入与 Claude 可选实验各自固定的 adapter/backend 组合 |
| MCP 参数、旧错误结构、行号/预算、工具集选择 | 各产品适配层；纯机制由共享库承担 |
| gld 本地读取、进程与交互执行 | 共享库与产品适配；不调用 exec-server |
| ccnm MCP 读取/进程 | 默认直接执行/共享库；实验仅委派预先选择的能力 |
| 搜索、编辑、条件写、journal、读取记录、输出落盘 | 直接路径/共享库承担；不算作 exec-server 已提供的复用收益 |
| Goal/Plan/History、业务验收 | 原产品/Orchestrator；不迁入 exec-server |

## 5. 不能绕过的安全和生命周期边界

### 5.1 监督进程必须活着

启用 exec-server 的 Codex 原生链或 Claude 实验链中，ccnm 包装器先审计身份和访问模式。只有 coding/具备写执行权限的会话才获取 writer guard，再 **spawn** exec-server 并继续监督；只读委派会话不取写锁，必须与既有 coding 会话共存，并拒绝任意进程启动、文件写入/删除等越权 RPC。不能因为启动执行后端就自动升级为 writer。

不能拿现有 Rust guard 后直接 `exec` 替换自身：成功 exec 不运行 Drop，带 CLOEXEC 的锁 FD 可能关闭，留下 `held` 状态而不能正常记 `released`。执行通用命令仍按 ccnm 的 coding 权限处理，不因客户端声称“这条命令只读”而免除写权。

关闭前停止接收新写请求，确认受管写进程结束，最后释放 guard。EOF、SSH 退出、exec-server 退出与它启动的进程结束是不同事件。若无法证明退出，保持 unknown/拒绝移交，不按时间清除锁。

gld 目前也不能假设已有覆盖 MCP/Actions/hub 与后台进程的一把会话写锁；迁移写入前要把这一能力显式建设并测试。

### 5.2 连接身份与执行权限

- MCP 路径沿用公开 SSH bridge；默认在 Runtime 内直接使用共享库，仅实验分支再以受管 stdio 连接 exec-server。不另开公网执行端口。
- Codex 原生 transport 按固定版本支持情况验证；若需要本机 WebSocket 桥，必须证明连接身份或不可伪造连接能力，随机端口与“只允许首连接”都不等于认证。
- **V2-G05 候选方案：URL 路径携带一次性连接能力。** 事实（0.154.0 源码）：ccnm 带 `--ignore-user-config` 启动 Codex 时只读 `CODEX_EXEC_SERVER_URL`，自定义 HTTP 头只能来自被忽略的 `environments.toml`；但客户端经 `into_client_request()` 按完整 URL 发起升级请求，路径原样保留（`exec-server/src/client_transport.rs:531-537`）；官方服务端本身不认证，只拒绝带 Origin 头的请求（`server/transport.rs:210`）。候选做法：ccnm 本机桥监听回环地址，`CODEX_EXEC_SERVER_URL=ws://127.0.0.1:<端口>/<每会话 256 位随机串>`，桥只接受路径完全匹配的一次升级，成功后立即作废，会话结束也作废。能挡：其他 OS 用户（读不到 Codex 进程环境变量）。不在防护范围：同一 Agent 账号的进程（它本来就能读 Codex 登录）。已知坑：客户端把完整 URL 写进连接标签 `exec-server websocket {url}`（`client_transport.rs:554`），验证时必须检查日志与遥测是否出现该串；出现则该方案否决或需额外脱敏。未通过前 Codex 原生链只做合成数据实验。**2026-09-16 实测结果：按此判据否决。**重放和错误令牌都挡住了，但会话中途断线一次，带令牌的完整 URL 就进了发给模型的工具输出、会话记录和 stderr（5/5 次）；脱敏只能改 Codex。漏出去的是已作废的令牌。见 `evidence/v2-c/g05/README.md`。**用户随后选了方向 2：按连接对端的 OS 用户放行，URL 里不放秘密。实测连接身份通过**：Linux 上别的用户和 root 连进来都被拒，识别出对方的 uid；macOS 上 Codex 端到端可用；查不到 uid 就拒绝。另外确认：网桥若放行重连，Codex 会自己 resume，所以每个会话只能放行一条连接。见 `evidence/v2-c/g05-peer/README.md`。
- **2026-09-16 P23 开工前的核对推翻了"需要本机 WebSocket 桥"这个前提。**Codex 0.154.0 的 TUI 先读 `CODEX_HOME/environments.toml`，每个环境要么 `url`（WebSocket），要么 `program`/`args`（stdio 子进程，由 Codex 自己 spawn、自己持管道，客户端里没有 reconnect 策略）。ccnm 走后者：每会话一份 CODEX_HOME，`program` 指向 `ccnm internal exec-transport`，它 exec 成到 Runtime 的 ssh。连接身份、只放一条、不 resume 三件事由"Codex 自己 spawn"直接给出，没有监听端口，也没有新依赖；G05-peer 的 uid 方案留作对照，不进产品。实测和实现见 ccnm `docs/research/p23-stdio-transport-2026-09-16.md`，脚本在 `evidence/v2-c/p23-stdio/`。`--ignore-user-config` 会关掉 environments.toml，但交互模式本来不传它（也不认它）。
- 无法建立可靠认证的 transport 只做合成数据、无敏感目录的隔离实验，不进入产品。不能为绕过此门禁自动移除 `--ignore-user-config` 或复用个人配置。
- 每个请求绑定物理工作区、调用作用域、配置版本和执行会话；模型不能传入/提升 root、身份、bridge executable 或权限上限。
- 原生 RPC 路线同样需要检查，不允许只在 MCP 前端做授权，然后把原生连接当无限权限字节管道。
- 路径策略先检查原始输入，再安全打开；ccnm 对 `..` 的拒绝、gld 外部读取选项及根内 symlink 语义分别保持，不能先词法抹掉信息。

### 5.3 沙箱、凭据、配置和网络

分别验证 file read/write/remove、process、open-handle read、environment/config 与 HTTP RPC。没有 sandbox 参数的执行路径不能默认为安全。由受控配置生成权限上限，不能转发模型指定的任意 sandbox/env。

Runtime 不保存模型凭据或 ccnm 主动控制链的私钥/agent。采用明确环境白名单、受控 HOME/CODEX_HOME、FD/socket 继承策略和现有账号审计；变量名包含 TOKEN/KEY/SECRET 的检查只作辅助。

首轮只允许必需 RPC。`http/request`、远端注册/relay、环境配置读取等未证明必要的入口默认不纳入准入范围；如果官方 Codex 实际依赖某入口，先证明最小用途与权限，再决定开放。文件沙箱不自动覆盖独立网络请求，未验证 egress 就不作保证。

**Codex 原生链的读边界（用户 2026-09-16 决定，依据 `evidence/v2-c/g06/README.md`）：原生文件读和 MCP 读同一契约**，不接受"可读范围 = 执行账号可读范围"。规则细节、`.git` 上溯查询怎么答、命令执行为什么不受这条约束，只写在 ccnm `docs/plan/runtime-surfaces.md` 第 12.2 节；实施和验收在 ccnm 的 P21–P24。

### 5.4 取消、恢复、重试

- `terminate` 返回已发送终止或仍 running，不等于确认退出；保留最终退出/关闭证据。
- 官方 WebSocket 的 detached/resume 行为不能直接套到 ccnm v1。首轮不新增对外 resume：连接失效则句柄失效，对账或 unknown，禁止偷偷恢复后重放。
- 写入和进程启动默认不自动重试。同键去重仅在后端契约已验证时使用；`writeId` 的 stdin 去重不等于整个命令、事务或模型任务的 exactly-once。
- 不能把未知结果记成失败再换旧后端执行。同一任务中后端不热切；读句柄、进程 ID、输出引用都绑定连接代次。
- 实验允许“读取仍走共享库、只有进程走 exec-server”，但各能力的路由在会话开始前固定；不能在某次调用失败后临时换执行方式重做。

## 6. 共享代码和旧契约

### 6.1 最小模块，按已证明需要建设

共享库与实验适配分开建设，以下名称只是职责划分，不要求立即拆成多个 crate。共享读取/写入/进程机制从既有实现和成熟组件逐步提取，不以实验成功为前提：

| 模块 | 只负责 | 不负责 |
| --- | --- | --- |
| text / fs | 有界行片段、编码与截断、句柄操作；输入由产品授权 | 强迫两产品使用同一编码/BOM/路径策略 |
| edit / commit | 旧编辑模式适配、条件提交、journal与恢复原语 | 把多次 write RPC 包装成虚假的原子事务 |
| process / output | 复用进程生命周期机制、交互/输出预算与保留机制 | 模型、业务任务状态、权限授予 |
| tool-adapter | 旧工具参数/结果与共享原语的转换 | 复制两份持续演进的同类算法 |
| exec-client（独立可选） | 固定 codec、事件、分块读取/进程句柄，仅供原生接入或 Claude 试验 | 成为 gld 本地/基础库默认依赖；参与普通本地打包 |
| conformance | 中立协议客户端、已知失败反例、故障注入与资源检查 | 只比较格式，不检查真实落盘与进程状态 |

主线产物是 gld 与 ccnm 直接路径可复用的 Rust 机制，而不是先自建所有底层算法。A 若获准，仅替代明确范围；仍须如实保留其他共享库职责，不能把减少某段进程代码宣传成完整工具内核替换。

### 6.2 兼容矩阵必须先于迁移

| 项目 | 旧模式要求 | 新语义如何进入 |
| --- | --- | --- |
| ccnm `ccnm.workspace-mcp/1`、`ccnm.machine/1` | 保留已冻结参数、权限、错误与生命周期；保留显式 version 与顺序依赖 edits | 破坏性变化按协议版本升级，不由模型 A/B 决定能否破例 |
| gld 默认工具集 | 保留文本 patch/Unified Diff、现有输入输出与范围；已确认错误定位单独修复 | `.env*` 新禁读规则、模糊匹配等作为显式新策略/工具版本，不夹带进抽库 |
| JSON Schema | 分别测试省略、null、合法值、非法值 | 可空联合是语义，不能当去噪删除；去除元数据也需证明不改变消费行为 |
| 输出 | 保留旧 `content/isError/structuredContent` 契约和已声明 outputSchema | 新纯文本工具面单独试验；不能声称所有 Host 永远只看某一字段 |
| 读取与搜索 | 保留编码、BOM、范围、总行数、ignore、排序、截断约定 | 新默认预算、自动上下文、mtime 排序只进明确版本 |
| stdin/PTY | gld 本地继续走共享库，保留当前会话能力 | Claude 实验按其真实需要核对缺口；不能以补扩展为由将 gld 本地接入 exec-server |

读取记录若用于写入前置检查，必须由产品注入 opaque `ReadScope`，绑定调用者/执行会话、物理 root、文件身份与内容版本。完整读取指同版本内容已完整交付给该作用域，不是后台扫描到了 EOF；分页范围合并、失效和跨主体隔离需测试。

旧 ccnm 的 `a→b、b→c` 顺序编辑不能因新模式“全部对原文匹配”而丢失，也不能按使用比例超过 2% 才恢复。新的匹配档位必须明确选择；歧义拒绝、重叠规则与错误诊断有各自预算。

## 7. gld hub 仍走公共 ccnm bridge

本节独立推进，不等待 Claude 的 exec-server 试验，也不要求远端安装 Codex。第一版是远端工具调用，不提交自主 Agent 任务；ccnm 默认直接执行路径即可支撑它。

- Local / CcnmRuntime 成员分型；远端不构建本机 Workspace/Harness，不读取本机同名路径的 Planning/History。
- 使用固定前缀 `remote_workspace_info/read_file/list_files/search_text` 等静态工具；schema 基于冻结协议，加 gld 路由字段，但保留 null 等接受值。只开放已评审工具，不自动跟随上游增加权限。
- read bridge 按已验证主体、成员、配置代次绑定，按需建立与有界空闲回收；不是按模型传入的 session metadata 授权。
- coding 使用显式 `remote_coding_begin/end` 和随机句柄。所有 coding/输出调用携带 workspace + handle；句柄绑定主体、模式、配置版本、真实 bridge 实例，避免同名 output_ref 串会话。
- 后端收到的参数只含 ccnm 工具数据；本机 bridge executable、SSH node/身份、root、私钥不能由模型覆盖。远端 exec 的 argv 按 ccnm 原契约执行，不能误禁所有“可执行程序”参数。
- hub、成员最大模式与 Runtime 权限取交集；read-only 不能通过 remote wrapper 调 exec/patch。旧本地调用继续由原策略管控。
- 同一 coding 句柄串行调用；空闲到期停止接新请求，排空或核对在途操作后关闭。MCP ping 不是 Web 用户活跃证明，不能无限续租。
- gld 重启、bridge 丢失或 Web 请求断开后，未知写入不重放，不切本机，不强删 Runtime guard；旧输出引用不能冒充新会话引用。

远端命令期限必须与真实 Web/MCP 链路预算一致；超过支持范围时在启动前拒绝，不在 HTTP 超时后偷偷当后台任务运行。若未来需要异步任务 API，单独设计，不伪装成已有 ccnm v1 能力。

## 8. 阶段与交付

编号使用 `V2-`，不与 v1 P0–P5 或 ccnm 自己的阶段编号（P0 起）混用。进度见第 11 节。

| 阶段 | 交付 | 通过/停止条件 | 模型调用 |
| --- | --- | --- | --- |
| V2-K 共享库主线 | 先提取有界文本读取，再分批共享编辑/提交、进程/输出机制；gld 与 ccnm 直接适配 | 各自兼容与安全回归通过；gld 本地在未安装 Codex 的环境也能构建、安装、运行并维持交互会话 | 0 |
| V2-H hub 接入线 | AuthContext、静态 remote 工具、合成 peer、公开 bridge、read 后 coding | 使用 ccnm 默认直接路径先离线后真机；V2-G12 及相关权限/恢复门禁通过，不依赖 exec-server 或工具面 A/B | 离线 0；Web 实际使用单独记账 |
| V2-C Codex 原生路线 | 保留官方原生 exec-server 接入，独立锁定客户端/服务端组合和监督器 | 原生链的协议、权限、执行位置、guard和恢复门禁通过；不以 Claude 试验通过为前提 | 离线 0；真实回合需明确预算 |
| V2-Q Claude 工具面快速验证 | Q1：实测 instructions 超过 2048 个 UTF-16 码元时 Claude Code 是否截断（带标记行，1 次回合）；Q2：ccnm 7 个工具加 `_meta["anthropic/alwaysLoad"]`（纯加法，不删可空联合、不做 schema 去噪）后与现状做小样本对照 | 按第 10.2 节规则判定；只改工具元数据和 instructions 顺序，不碰执行路径、冻结工具语义与共享库；不采纳时撤回该字段 | 按第 10.1 节实验单记账 |
| V2-P0 Claude 试验基线 | 固定当前 ccnm 与 adapter/backend 版本；同身份/目录/环境/命令/预算的对照；明确拟新增约束、成本阈值和取得二进制的方式 | 基线可重建，收益/成本判据在候选运行前冻结，不修改生产配置 | 0 |
| **V2-P1 Claude 收益验证（保留）** | 中立 MCP 客户端与原生 RPC 客户端完成分块读/进程闭环；对照直接执行，分别记录沙箱增量和RPC/部署/维护成本 | 完成 V2-G01–G04 的能力/正确性记录和 V2-G13 的测量报告，回答第 3.2 节问题，输出继续/仅进程/否决结论。失败或无净收益可以判否决并结束，只有拟继续的能力必须通过相应门禁 | **0** |
| V2-P2 候选深入门禁 | 仅对 P1 值得继续的能力完善监督、权限、取消、断连、输出与互斥 | V2-G05–G10 适用项通过；任何权限弱化或未知写权停止接入 | 0 |
| V2-P3 Claude 准入决策 | 核对保留的库职责、交互缺口、版本运维与完整收益/成本；必要时评审最小 B | 既有编辑契约不变，V2-G11 相关项及 V2-G13 准入项通过；不因缺完整工具层就自动 fork | 0 |
| V2-P4 Claude 小范围 opt-in | 仅启用获准的进程/沙箱或分块读委派；搜索、编辑、journal、输出存储继续共享库；不改 gld 本地 | 受影响硬门禁、独立版本部署、升级和回退均通过；真实 Claude 回合仅在已有授权范围内进行 | 仅有明确预算时 |
| V2-P5 可选优化与收敛 | alwaysLoad/native@1/lean@1、小样本 A/B；分别收敛已验证的共享库与可选 backend | 优化不要求先采纳 exec-server；只删除真正被替代的重复代码，默认切换另作决定 | 按实验单批准 |

V2-K、V2-H、V2-C、V2-Q 按各自依赖推进，不被 Claude 试验阻塞。V2-Q 不依赖任何其他线，可最早开始。Claude 实验线为 P0 → P1（继续才进入）P2 → P3 → P4；否决后停止该实验线，不暂停共享库或 hub。若 P3 决定采用 B，扩展版本须重跑收益验证、权限与相关兼容门禁，不能继承官方原版通过记录。P5 不阻塞主线。

ccnm 实际改代码前按其规则立新阶段、更新唯一状态源。本计划记录跨仓依赖，不代替产品状态。不为了宣称统一而同时开展多个互相覆盖的核心重构。

### 第一批可执行任务

0. V2-Q：instructions 2048 码元截断实测，然后 alwaysLoad 小样本对照；同时修第 13 节标"立即修"的缺陷。
1. 共享库线保存旧 fixture，先提取两个产品真正共用的有界文本原语；hub 线先做类型/认证与合成 peer；Codex 线先按第 5.2 节候选方案验证 URL 能力认证（无模型）。
2. Claude 实验线冻结直接执行对照和候选沙箱策略，写两个无模型协议客户端，验证分块读与进程生命周期。
3. 用合成 canary 明确证明额外约束来自执行端，记录无沙箱/有沙箱的开销，以及二进制部署与版本升级成本。
4. 对照成熟库可实现的同等约束，形成继续/仅进程/否决结论；任何结果都不把 gld 本地接到 exec-server。

## 9. 硬门禁与预算

| 编号 | 必须验证的行为 |
| --- | --- |
| V2-G01 协议 | 真正支持的初始化和 RPC 字段；未知版本/方法/超大帧拒绝；README 与源码不同以固定版本实测为准 |
| V2-G02 同引擎 | MCP 与原生客户端调用同一实现，返回实际文件和进程证据；不能只比较两段相同字符串 |
| V2-G03 读取 | UTF-8 跨块、BOM、非法编码、空文件、CRLF、无尾换行、巨型单行、分块期间原地修改；句柄及时关闭，不把句柄当快照 |
| V2-G04 进程 | argv/cwd/env、双流排空、seq 游标、writeId 有界去重、signal/exit 区分、自然退出与取消；缺 closeStdin/resize 明确反映能力 |
| V2-G05 身份 | 未授权连接、抢首连接、跨主体/工作区/配置代次句柄、合成凭据不可访问；无原始 token 日志 |
| V2-G06 权限上限 | file/process/open-handle/config/http 分别验收；原生和 MCP 同样约束；缺 sandbox 不默认放行，原始父路径/根内外 symlink 按产品契约处理 |
| V2-G07 写权 | 只读不取写锁且可与 coding 共存；coding 跨入口竞争同物理资源；同会话并发修改串行；监督进程持锁覆盖写入/写进程生命周期；未确认退出不写 released，不按期限夺锁 |
| V2-G08 故障 | 前端断开、SSH 黑洞、监督器/服务/子进程分别崩溃、在途超时；已执行未回包保留 unknown，不重放；stdio/ws 生命周期分别记录 |
| V2-G09 资源 | 头尾边界、多字节、200 MiB 连续输出、磁盘配额/写失败、过期引用、快速完成后续读；内存和磁盘均有界 |
| V2-G10 兼容 | 旧 schema 的省略/null/非法值、旧错误与预算、read/exec/patch范围；错误修复单独记录，不重录 golden 掩盖漂移 |
| V2-G11 写入语义 | 顺序依赖 edits、重复片段、create-only、stale版本、ReadScope隔离与截断交付、多文件中途失败、恢复再次中断、权限位/Windows替换；未满足就保留旧写路径 |
| V2-G12 产品链 | hub 本地成员零回归；远端无本地 Planning/Harness 副作用；真实 Web/CLI 的执行位置、返回结果、关闭和回退有证据；官方 Agent 回合与无模型测试分开记 |
| V2-G13 Claude 收益 | 同 Runtime 身份与既有权限下，对照额外执行端约束；分别计量 RPC/base64/CPU/RSS/延迟和部署/版本/维护成本。测量并给出可追溯的否决结果也算完成实验；只有达到冻结收益/成本判据和安全门禁才算候选准入，不以连接成功代替收益 |

### 实验前冻结，不事后调整判据

各主线与实验在运行前按“平台 × 前端 × 操作”明确必测/不适用、拒绝策略和预算。Claude 试验的 V2-P0 另冻结收益/成本判据；V2-G02/G13 只要求于相关 exec-server 实验，不作为共享库与 hub 的依赖。权限、执行位置、结果真实性不能列为不适用；未暴露的能力才可排除。下列为首轮拟定上限，不是已测试保证，实施前可有依据地调整并记录：

- 普通协议请求 deadline 10 秒；文件 readBlock 最大 1 MiB，模型预览默认 16 KiB、上限 32 KiB；JSON/base64 包装单独计量。
- 单流输出保留默认最多 64 MiB、每会话总保留最多 256 MiB；文件数量和整服务总配额另定。达到预算仍排空进程管道并记录丢弃/截断，不因不再存盘而让子进程死锁。
- 活动写进程不因缓存淘汰而丢失监督和写权；只清理已确认终态且允许过期的结果。磁盘写失败独立上报，不能伪造命令失败或成功。
- 超时测试用 2 秒期限，取消后 5 秒内确认普通受管后代退出和管道回收；逃离进程组的对抗情形另列平台隔离边界。
- 读取测试使用生成式流和 8/128 MiB 样本，固定页面预算；记录峰值 RSS、扫描量及耗时，禁止整行无界缓存。原 RPC 的大整文件分配不因返回内容被截断就算有界。
- 涉及写权/写锁移交、并发写和“请求已执行未回包”的故障点各至少重复 20 次；其余故障点（只读取消、连接失败、输出上限等）各至少 5 次。零越权副作用、零被隐藏的错误片段写入、零违反已声明重试语义的重复执行。

平台与内核能力未验到时只声明已验证范围，不以 Windows 交叉编译代替进程树/文件替换实测。新上限与旧契约冲突时，不覆盖旧默认：调整产品预算、缩小启用能力或进入新版本，须明确决策。

## 10. 模型验证与工具面优化：后置、可观测、有限额

### 10.1 先闭环，再比较

各自无模型门禁通过后，Codex 原生链可独立做最小真实回合。Claude 只有在 V2-P1/P3 收益与准入得到支持后才测试委派回合；未采纳时继续直接执行/共享库路径，不为凑齐双入口评测而消耗额度。只有已有授权覆盖的回合才能运行；新额度、系统部署或权限变化按既有规则确认。

每个实验单列 `max_runs`、重试是否计入、deadline、停止条件和授权引用。预算用“提供方 × 任务 × 组数 × 重复次数 + 冒烟/重试”明确计算；历史对照复用必须证明版本/模型/配置/夹具一致，不同时声称与本轮随机交替运行。

**额度授权**：用户已于 2026-09-15 同意为本计划的模型验证消耗 Claude/ChatGPT 订阅额度。授权引用写 `user-consent-2026-09-15`；全部实验累计上限 145 次模型运行（沿用 v1 估算），每个实验单的 `max_runs` 计入累计并记入 evidence。以下情况先告知用户再运行：累计将超过上限、新增不在本计划中的实验类型、需要系统部署或权限变化。

### 10.2 正确性先于 token

- 基线本身须有效；连接、认证、缺工具导致的低成本失败不是优化收益。
- 按任务冻结最低成功要求，逐任务比较，不跨任务抵消退步。0/5 对 0/5 无论多便宜都不得通过。
- 身份、目录、审批、guard、恢复和兼容硬门禁一项未解决，都不能靠 token/调用次数优势切默认。
- 成功任务资源消耗与失败成本分别报告；记录墙钟、input/output、cached usage 可见字段、工具次数及副作用证据。少量样本只能支持有限结论，追加运行不得超出预算。
- CLI 未公开的实际 tools/system/messages 不伪造 hash。只 hash 自己控制的 MCP schema、instructions、输入和配置，缓存变化最多先描述相关性。

### 10.3 新工具面单独版本化

native@1、lean@1、纯文本输出、自动上下文、大纲和重复调用提示都是后置实验，不绑进底层机制迁移。alwaysLoad 与 instructions 截断实测例外：它们只改工具元数据和说明顺序，按 V2-Q 提前做。MCP 名称相似不代表 Claude 原生权限规则/hooks 自动适用；纯文本模式若保留 outputSchema，仍须符合 MCP 输出要求。

不以未锁定版本的宿主行为矩阵、外部项目百分比或单段字符串 tokenizer 对比，直接承诺本项目节约比例。

## 11. 工程、回退和阶段状态

### 工程与来源

- **三个仓库统一 `rust-version`（用户决定）。** 执行时点：第一个共享 crate 被 gld 或 ccnm 链接时，三个仓库在同一批提交里改成同一个值，并各加一个 MSRV CI 任务（`cargo +<版本> check --workspace --all-targets --locked`）。统一值取"三仓现有声明"与"共享 crate 实际依赖闭包要求"中的最大者；当前下限是 ccnm 的 1.89。在此之前不改任何仓库的声明。以后升级也三仓同步，提交说明写明是哪个依赖或 std API 要求。独立构建的 exec-server 二进制不计入。
- 只建立有真实消费者的模块；gld 本地与 ccnm 直接路径共享库，优先采用成熟组件而非重写机制。exec-client 独立可选，不能把 Codex 二进制/实验协议引入 gld 默认依赖、安装器或 Windows 发布物。
- Codex 原生客户端/服务端按已验组合部署；Claude 若最终采纳，使用显式固定的 backend 路径、版本与独立升级流程，不跟随全局 `codex` 自动替换。共同使用某个版本是可选运维决策，不是 Claude 必须随每次 Codex CLI 升级的技术结论。
- 常规依赖优先 crates.io；必要的 Git 依赖固定完整 revision，发布前验证打包要求。应用保留自己的 Cargo.lock，不照抄整个上游 lock 解决 root patch 问题。
- 本仓库拟 Apache-2.0，实际代码落地时补 LICENSE。移植逐文件记来源 SHA、路径、许可、修改摘要和测试；借鉴设计同样保留来源，不隐藏参考记录。
- Grok、WebCodex 优先作为小组件与行为测试来源；不整体引入各自模型、账号、权限、Workflow/ledger 状态，也不复制私有客户端协议或提示词。

### 回退

1. 新 backend 默认 opt-in；旧公开模式不自动切换。停止新写请求，列出并排空受管操作。
2. 有未知执行、未决 journal 或未确认退出进程时先核对，不能直接换旧 backend 重做。
3. 保留版本、配置、输出引用、审计与可恢复状态；schema 不向后兼容时，旧二进制不能直接读取新数据。
4. 确认资源可移交后回退配置或消费者 revision；文件内容恢复是另一项授权操作，不自动 reset/clean、不覆盖用户修改。
5. 只删除已验收替代且无消费者依赖的重复代码。任一产品可独立回退，不要求三仓同步改历史。

### 当前状态

| 阶段 | 状态 | 本轮证据 |
| --- | --- | --- |
| v2 方案文档 | 已按反馈收窄，并写入用户两项决定 | 本文件；gld 本地走共享库，Claude exec-server 为独立实验；统一 rust-version、额度授权见第 11、10.1 节 |
| V2-K | **两刀都已落地**：`toexec-text`（有界行读取）和 `toexec-fs`（原子文件替换），两个产品都链接了 | 开工前的重复度盘点见 `evidence/v2-k/duplication-audit.md`：两边 `read_file` 契约不同**不统一**，真正共有的只有「读一行但不把整行读进内存」。`toexec-text` 提交 `fbf28bb`（9 个测试）；ccnm `e589d08`（719 passed，24 个 read 测试断言一条没改）；gld `b4c8a77`（467 passed，rust-version 1.85→1.89，行为变化是超长行只搜前 1 MiB）。依赖按 tag 固定在这个仓库的公开远端 `github.com/xwfe/toexec`，两边都在「旁边没有 toexec」的目录里构建通过 |
| V2-H | **H01–H08 全部完成**（gld） | gld `5333763`…`201af28`：成员分本地/远端、鉴权主体、`gld hub remote`、只读链、远端 coding（写租约）、会话内有界等待、真机闭环。跨两台真机的现场记录在 gld `docs/rfc/evidence/v2-h-read-chain.md`，逐项对照在 gld RFC-0002 第 9 节。**未做**：单独归档的脱敏 transcript、公网入口链路 |
| V2-C | **连接身份、协议、权限三道门禁已实测，读边界已定；ccnm P21–P23 已完成（Runtime 侧 + Agent 侧，离线闭环），P24 真机验收待授权** | URL 一次性令牌方案否决（`evidence/v2-c/g05/`：断线后令牌进入发给模型的文本）。方向 2（`evidence/v2-c/g05-peer/`）：Linux 容器里 bob、root 连进来都被拒；macOS 上 Codex 端到端可用；查询失败即拒；200 次查询 p50 5.2 ms（含起进程）；网桥必须每会话只放行一条连接，否则 Codex 会自己 resume。全程零模型请求。V2-G05 的会话绑定各项要接进 ccnm 后才能测。**V2-G01 协议实测**（`evidence/v2-c/g01/`，stdio，3 遍一致）：没有版本协商，只能核对 `executorVersion`/`providerId`；`http/request` 和写类 fs 方法都开着，网桥必须按方法做白名单；未知通知和超过 64 MiB 的帧都会直接断开连接、不回错误；关 stdin 时服务端会清掉自己起的进程。**V2-G06 权限实测**（`evidence/v2-c/g06/`，3 遍一致）：exec-server 完全信客户端的 sandbox——`null` 就不受限，客户端放宽 `workspaceRoots` 也照做；Codex 自己的 34 次 `fs/getMetadata` 都是 `sandbox: null`；`http/request` 没有限制参数；`environmentConfig/read` 会返回服务端配置里的凭据，而 Codex 每次启动都调它。权限上限只能靠运行身份 + 网桥按方法校验。**用户随后决定原生文件读和 MCP 读同一契约**（第 5.3 节）。**ccnm 已立 P21–P24 实施这条链**（ccnm `e9847e4`）。**P21 已完成**（`evidence/v2-c/native-surface/`，17 个实验各 3 遍一致，零额度）：交互模式 `-C <Runtime 根>` 可用、`codex exec` 要求 Agent 本机有同路径，所以原生链首版只开交互模式；人在 Codex 里批准提权后，命令带 `sandbox: null`、越界 patch 带多出来的路径写条目，都能写到工作区外，规则表因此逐条核对 sandbox；Linux 需要 bubblewrap 和 user namespace；Linux 发行包握手的 `executorVersion` 是 `0.0.0`。冻结的规则表在 ccnm `docs/research/p21-codex-native-surface-2026-09-16.md`。**P22 已完成**（ccnm `3a79225`、`5d656bf`、`3cce11a`）：Runtime 侧 `ccnm internal exec-serve` 按规则表过滤后转给 exec-server，和 MCP 入口同一套审计、同一把写锁；会话结束前按环境变量标记扫进程表，证明 exec-server 起过的进程都没了才放锁（实测 `setsid` 脱离的进程 exec-server 自己不清）。集成测试两组故障注入各 20 次，本机接真 Codex 0.154.0 exec-server 跑通，CI 的 macOS 与 ubuntu-24.04 都通过。**P23 已完成**（ccnm `968d55c`、`96b53d0` 及后续，记录在 ccnm `docs/research/p23-stdio-transport-2026-09-16.md`，脚本与三轮结果在 `evidence/v2-c/p23-stdio/`）：开工前核对源码发现 Codex 会按 `CODEX_HOME/environments.toml` 自己 spawn 一个 stdio 传输子进程，于是 Agent 侧不是 WebSocket 网桥而是 `ccnm internal exec-transport`（exec 成到 Runtime 的 ssh）+ 每会话 CODEX_HOME（auth.json symlink 到 profile，实测读写穿透）；零额度端到端：真实 Codex + 真实 `exec-serve` + 真实 exec-server，读改跑通过、提权请求被拒不落盘、断线不重连、Runtime 连不上时 Codex 没有工具、Codex 进程树无监听端口。ssh 那一跳用本机 TCP 管道代替。下一步 ccnm P24：真机（装 Codex、换二进制、模型回合逐项授权），V2-G07/G08 的真机部分在那里 |
| V2-Q | Q1 客户端层已确认，模型侧确认仍受阻；**Q2 已完成，结论采纳 alwaysLoad** | Q1：Claude Code 2.1.269 按 2048 个 UTF-16 码元截断 instructions（静态代码 + 真实连接 debug 日志），模型侧那一次尝试因 CLI 未登录未发出请求，见 `evidence/v2-q1/README.md`。Q2：fodelf 上 2.1.272 + ccnm 0.7.0 跑 18 格（3 任务 × 2 组 × 3 次）全通过，A 组每格恰好一次 ToolSearch、多一个回合，四条判据全满足，见 `evidence/v2-q2/README.md`。**累计模型运行 20/145，$2.1454** |
| 第 13 节缺陷队列 | 全部已修 | gld `7aac894`（git 超时）、`bfcdffb`（LICENSE）、`67159d3` / `6cdffe5`（task_context 与任务回包里的逐文件清单）、`a3618ea`（变更摘要比错基线）；ccnm `dc30b69`（协议上限）、`741f23c`（AGENTS.md） |
| ccnm instructions 预算与顺序 | 已修（ccnm P13） | `60ad480` 代码、`557837d` 阶段验收；记录在 ccnm `docs/research/p13-instructions-host-cap-2026-09-16.md` |
| Q2 结论的落地 | 已落地（ccnm P15，只改文档） | ccnm `db53098`：外部入口的 `mcpServers` 示例加 `"alwaysLoad": true`，协议文档写清依据与代价；选服务器配置而非工具 `_meta`。本机零额度复现 `coding` 21→14、`read` 18→14，记录在 ccnm `docs/research/p15-alwaysload-2026-09-16.md`。Managed 路径不需要改 |
| V2-P0–V2-P5 | 未开始 | Claude 收益验证及后续阶段未执行；不得推断已采纳 |

2026-09-16 第四批：V2-H 在 gld 那边做完（见上表）。接的过程中在 ccnm 撞出两个缺陷，按 ccnm 的规则各自立阶段修，这里只留指针，账在 ccnm `docs/plan/status.json`：

- **冻结协议的工具表 fixture 与实现对不上**（ccnm P18 `861617d`、P19 `ef567bd`）：`tools-list-coding.json` 把 `apply_patch` 的参数写成 `changes`，实现收的是 `files`，另漏 7 个参数。现在 `external_mcp` 起真实 server 逐工具比参数名、`required` 和 `description`。gld 的远端白名单当时已改成以 ccnm 的 `*Args` 结构体为准，不受影响。
- **只读外部会话被拒时消息指错地方**（ccnm P20 `c720154`、`721b1fb`，开发时叫 P18，合并撞号后顺延）：闸不动，消息不再说 `exec_command`、不再推荐开不了这道门的 `allow_unconfined_exec`。**只读链只需要 `allow_unisolated_credentials`**，真机验过。真机还查出这条链上**退出码不可用**：fodelf 的 22 端口由 Tailscale SSH 应答，它不透传远端退出码，`mcp-serve` 退 33 而 bridge 退 0；ccnm 无代码可改，已写进其协议 11.2。

两台机器上装着的 ccnm 仍是 0.7.0，不含 P18–P20；这几个提交已推到 ccnm 远端（合并提交 `012ff6e`）。

2026-09-15 第一批：修了第 13 节 4 项"立即修"（只改 gld git 工具超时这一处执行路径，另三项是文档/许可证），做了 V2-Q1。未安装依赖、未构建 exec-server、未跑 SSH、未改 ccnm 阶段状态。后续结果放入可追溯的 evidence 目录，记录命令、固定版本、输入/输出 hash、OS/身份、通过/失败/跳过与限制；真实秘密不进入证据。

2026-09-16 第三批：V2-K 开工。先做重复度盘点（`evidence/v2-k/duplication-audit.md`），结论是**计划里"先提取有界文本读取"按字面做是错的**——两个产品的 `read_file` 是两套对外契约，不能也不该统一；真正共有的只有一个原语。于是第一刀只切那一个：共享 crate `toexec-text` 的 `next_line`，ccnm 和 gld 都改成调用它，行为各自逐字节不变（ccnm 719 passed，gld 467 passed，两边的既有断言都没改）。同时按用户 2026-09-15 的决定统一了 `rust-version`（gld 1.85→1.89），时点就是这一刻。

**依赖方式中途改过一次。**先用的是本地 `path` 依赖（当时这个仓库还没有 remote），结果两个产品的 CI 都构建不了——GitHub runner 只 checkout 一个仓库，cargo 在解析 manifest 阶段就失败，而且绕不过去（optional 依赖也要求 path 存在，vendor 进去等于又抄一份）。同日用户决定把这个仓库推成远端：`github.com/xwfe/toexec`，**公开**（用户说的是私有，推之前发现它其实是公开的，确认后按公开推——ccnm 和 gld 本来也都是公开仓库），两边改成 `{ git = "https://github.com/xwfe/toexec.git", tag = "toexec-text-v0.1.0" }`。

按 tag 不跟 `main`：共享库改了不会在某次 `cargo update` 之后突然改变产品行为，升级是显式的一步。用 https 不用 ssh：公开仓库匿名可读，本地和 CI 都不必配凭据；`github.com-xwfe` 那种 SSH 别名只存在于本机 `~/.ssh/config`，写进 `Cargo.toml` 的话 runner 上永远解析不了。**验证方式就是 runner 的处境**：把 ccnm 和 gld 分别 clone 到旁边没有 toexec 的目录，`cargo check --workspace` 都通过。**GitHub Actions 上还没有真跑过一次。**

**第二刀（原子写入）同日也做完了**，就是盘点里认定收益最大的那块：共享 crate `toexec-fs`，ccnm P17（`92830d6`，719 passed，既有断言一条没改）、gld（`342d15c`，470 passed）。gld 是真正的受益方——它原来暂存时既没有 fsync、也不带原文件权限，**打完补丁的脚本会从 0755 变成 644**，下一次 `./run.sh` 直接 Permission denied；新测试验过红灯基线。备份与回滚编排没有共享，两边差得远。

这一刀留下一条适用于后面每一刀的经验：**错误的分类和措辞是产品的对外契约，不是可以顺手统一的实现细节。**`toexec-fs` 0.1.0 把四步失败合成一个 `io::Error`，接进 ccnm 时才发现那会把刷盘失败从 `internal` 悄悄变成 `invalid_args`（没有测试会失败，但 MCP 错误码的含义变了），于是 0.2.0 改成报出是哪一步失败、由调用方归类。

还没做的：**父目录 fsync**——`rename` 这件事要在断电后仍然可见还得 fsync 父目录，两个产品原来都没做，这两刀也没做，补它要给每次替换再加一次 fsync，是个单独的决定。进程/输出仍然不碰。

**同一天另一条线（gld 自己的缺陷，不属于 V2-K）**：盘点顺带查出 gld 的 `context_lines` 会把长行克隆很多份，当时估「1 GB 量级」。另一个会话接手实测并修掉了（gld `be98252`）：41 行 × 每行 1 MiB 配 `context_lines=20`，返回的 JSON 里字符串 1.19 GiB、进程峰值 RSS 2.50 GB；改成留存前按 `max_preview_bytes` 截一刀之后，峰值 RSS 降到 57 MB。匹配仍然拿整行去比，搜得到什么没有变。

2026-09-16 第二批：跑完 V2-Q2（唯一一次动用模型额度，20 次 $2.1454），并把它的结论落到 ccnm（P15，纯文档，没再花额度）。同一天在 ccnm 那边做的四件事都在 ccnm 仓库记账，这里只留指针：P13（instructions 预算与顺序）、P14（read_file 按行有界读取）、0.7.0 发版并把两台机器都换成该版本、P15（外部入口配置示例启用 alwaysLoad）。**V2-Q 这条线到此收尾**——Q1 的模型侧确认仍缺，但它不挡任何东西。共享库、hub、exec-server 仍未开工。

## 12. 固定来源与延伸阅读

以下用于说明能力和设计边界，不等于本仓库已跑过对应测试：

- [Codex exec-server README，rust-v0.154.0](https://github.com/openai/codex/blob/rust-v0.154.0/codex-rs/exec-server/README.md)
- [执行 RPC 类型](https://github.com/openai/codex/blob/rust-v0.154.0/codex-rs/exec-server-protocol/src/protocol.rs)、[完整路由](https://github.com/openai/codex/blob/rust-v0.154.0/codex-rs/exec-server/src/server/registry.rs)
- [分块读取](https://github.com/openai/codex/blob/rust-v0.154.0/codex-rs/exec-server/src/file_read.rs)、[文件执行实现](https://github.com/openai/codex/blob/rust-v0.154.0/codex-rs/exec-server/src/local_file_system.rs)
- [进程执行实现](https://github.com/openai/codex/blob/rust-v0.154.0/codex-rs/exec-server/src/local_process.rs)、[process sandbox](https://github.com/openai/codex/blob/rust-v0.154.0/codex-rs/exec-server/src/process_sandbox.rs)
- [stdio/WebSocket transport](https://github.com/openai/codex/blob/rust-v0.154.0/codex-rs/exec-server/src/server/transport.rs)、[session 恢复](https://github.com/openai/codex/blob/rust-v0.154.0/codex-rs/exec-server/src/server/session_registry.rs)
- [Rust exec 的析构/FD 边界](https://doc.rust-lang.org/std/os/unix/process/trait.CommandExt.html#tymethod.exec)
- [Claude Code MCP](https://code.claude.com/docs/en/mcp)、[Hooks](https://code.claude.com/docs/en/hooks#posttooluse-decision-control)、[Desktop SSH](https://code.claude.com/docs/en/desktop#ssh-sessions)、[Cowork 架构](https://support.claude.com/en/articles/14479288-claude-cowork-architecture-overview)
- [JSON Schema：null 不等于省略](https://json-schema.org/understanding-json-schema/reference/null)、[MCP outputSchema](https://modelcontextprotocol.io/specification/2025-11-25/server/tools#output-schema)

本地兼容依据：`/Users/bing/xdw/ccnm/docs/protocol/remote-workspace-mcp-v1.md`、`/Users/bing/xdw/ccnm/docs/protocol/machine-protocol-v1.md`、`/Users/bing/xdw/ccnm/crates/ccnm-core/src/mcp/write_guard.rs`、`/Users/bing/xdw/gld/docs/rfc/0002-shared-kernel-and-ccnm-hub.md`。这些文件的既有状态不被本草案自动覆写。

## 13. 已知缺陷队列（独立修复，不夹带进抽库或迁移）

每项先写失败测试再修；修复提交单独说明行为变化。"立即修"表示不等任何主线；其余在对应主线迁移前修。

| 仓库 | 缺陷 | 证据 | 时点 |
| --- | --- | --- | --- |
| gld | `run_git` 接收超时参数后 `let _ = limit` 丢弃，git 卡住时工具调用一直挂着 | `crates/core/src/tools/git.rs:476-503` | 已修 `7aac894` |
| gld | Cargo.toml 声明 Apache-2.0，仓库无 LICENSE 文件 | 仓库根目录 | 已修 `bfcdffb` |
| gld | unified diff 解析丢弃 `@@` 位置，每个 hunk 从文件开头找第一处匹配，重复片段可能改错位置 | `crates/core/src/tools/patch.rs:198-204`、`:335-407` | 已修 `3697f4d`：hunk 只在上一个之后找，按 `@@ -a,b` 行号取最近匹配，Codex `@@ 锚点` 生效（gld 0.4.0） |
| gld | 超时与显式取消只对直接子进程发信号，不保证清理子孙进程 | `crates/core/src/tools/exec.rs:250-279`、`session.rs:223-231`、`:550-559` | 已修 `3f18e10`：Unix 进程组整组发信号；Windows 改 `taskkill /T`，只经 CI 编译、未在 Windows 实跑 |
| gld | inline 正常结束立即移除 session，快速超预算输出返回的引用可能读不到（timeout 与 yield 路径不同） | `crates/core/src/tools/exec.rs:309-324`、`:347-374` | 已复现并修 `4248b79`：正常结束也保留 30 秒 |
| gld | schema 声明的上限代码未收紧；search 默认值 schema 与代码不一致 | `crates/core/src/tools/registry.rs`、`file.rs:243` | 已修 `bdb5ba6`：30 个整数参数集中到 `tools/args.rs`，测试逐项比对 schema；search_text 默认 max_results 1000→100 |
| ccnm | 读取超长单行先整行 `read_until` 进内存，读完才检查 64MiB 扫描上限 | `crates/ccnm-core/src/mcp/read.rs:292-332` | 已修：ccnm P14（`ce13fbf`），200 MiB 单行峰值 215→13 MiB（ccnm 0.7.0） |
| ccnm | 协议文档 exec 预览写"头尾各 16KiB"，代码是默认总 4KiB、上限总 16KiB；patch 写"单文件 1MiB"，代码是整次请求合计 | `docs/protocol/remote-workspace-mcp-v1.md:256-258` | 已修 `dc30b69` |
| ccnm | AGENTS.md 仍称 Remote Workspace MCP "experimental、无真实 Host 验证"，协议实际已于 2026-09-11 冻结 | `AGENTS.md:22` | 已修 `741f23c` |
| ccnm | Claude Code 把 instructions 截到 2048 个 UTF-16 码元，ccnm 按 16 KiB 字节做预算，且清单与标记行在末尾，超长时先被截掉 | `crates/ccnm-core/src/provider/context.rs:151-167`；`evidence/v2-q1/README.md` | 已修：ccnm P13（`60ad480`），Claude/外部按 2048 码元、标记行前置；真实 Claude Code 连接改前截断 4600→2048、改后 2030 无截断 |
| gld | `task_context` 的 schema 声明 `max_bytes`（8192–131072），代码不读，固定取 100 条事件并返回 `truncated: false`，输出无上限 | `crates/core/src/harness/tools.rs:117-131` | 已修 `67159d3`：按 `max_bytes` 装事件，截了说 `truncated` 并给 `next_cursor`。真正的大头是 `task.baseline.entries`（每个文件一条 sha256）：1500 个文件的工作区默认预算下回了 277059 字节。同一份清单还从 start/update/pause/resume/finish 和 `project_state` 六个出口原样回给客户端，`6cdffe5` 一并去掉，改为 `entry_count` |
| gld | `finish_task` 附带的 `change_summary` 把工作区每个文件都报成 `added`（上限 200 条）：先把任务转成非活动状态再算摘要，`project_state` 找不到活动任务，拿空基线去比 | `crates/core/src/harness/tools.rs` 的 `finish_task`；`harness/state.rs` 的 `project_state` 用 `current_task()` | 2026-09-16 修上一条时实测发现（1500 个文件、什么都没改，报 `{"added": 200}`）。已修 `a3618ea`：摘要按该任务自己的基线算全部改动再截到 200 条，加 `total_changed_files`；同一提交修了两处同类错——摘要先截前 200 个文件再挑改动（排在后面的改动整个漏掉），`project_state` 的 `clean` 在截断后判 |
