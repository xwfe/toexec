# workspace-kernel 实施方案 v2：双协议入口，共用执行底座

日期：2026-09-15。状态：**草案，待评审；尚未实施**。

本版是当前方案讨论入口。[v1 原文](implementation-plan.md)保持不变，供差异追溯；其“已确认”“已获同意”和阶段编号不自动成为 v2 的实施或费用授权。两个产品的实际进度仍分别由各自仓库记录，特别是 ccnm 的 `docs/plan/status.json`。

## 0. 一页纸结论

**优先验证 Codex 与 Claude 能否共用官方 Codex exec-server 的执行能力，而不是先重写一整套文件和进程内核。**

- Codex：保留官方 Agent，通过其原生执行协议使用远端 exec-server。
- Claude Code：保留官方 Agent，通过 ccnm 的 MCP 工具适配层调用同一种 exec-server。
- Web AI：仍由 gld hub 接 ccnm 公共 MCP bridge；Web AI 自行分析，不在后台额外启动 Agent。
- gld 本地工具：在共同底座证明兼容后，再决定哪些操作接入；旧工具和产品策略保留。
- `workspace-kernel`：优先沉淀共享协议客户端、适配所需的文本机制和必要的编辑语义；只有证实上游缺口后才扩展执行端。

**MCP 是工具入口，不意味着另一套执行器。修改开源执行端也不等于 fork 模型循环。** 先验证“官方服务端不改 + MCP 适配”，必要时才做“仅派生执行端 + 双协议前端”。

不增加 WebCodex Server、中心账号服务、自己的模型客户端或公开 HTTP 执行服务。这里的 exec-server 是由现有 Runtime 包装进程托管的子进程，不是新增一套产品控制面。

## 1. 相比 v1 的调整

| v1 | v2 |
| --- | --- |
| Codex 用 exec-server，Claude 的完整执行能力另写 | Codex 原生协议与 Claude MCP 优先共用执行底座；协议不同不等于底层必须不同 |
| 先统一三个仓库 Rust 1.89，再开展全部工作 | 先验证真实依赖和 MSRV；独立二进制的 MSRV 不传播到客户端，编译链接时再明确升级影响 |
| 预先建设完整 Root、编辑、执行会话、MCP 与工具集 crate | 先跑通 Read + Process，再按实际缺口增设模块，不先建全套空框架 |
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
| 交互缺口 | 当前路由没有独立 closeStdin、PTY resize RPC | 不承诺完整兼容 gld 交互会话；所需能力缺失时保留旧路径或扩展后再迁移 |
| 断线 | WebSocket 有约 30 秒 detached/resume 窗口；stdio 结束走 shutdown | 不得静默改变 ccnm v1 的无 resume 语义 |
| 沙箱 | 文件与 process 请求可带 sandbox；部分无策略路径直接执行 | 服务名称不构成隔离保证；两种前端都必须受相同权限上限约束 |

主来源见第 12 节。上述是源码能力，不表示凭据、事务、断网、Windows 或真实 Agent 已经验证。

### 2.3 Claude 的接入边界

公开 MCP 是优先入口；未确认存在能整体替换 Claude 原生 Read/Edit/Bash 后端的公开配置。MCP 工具即使叫 `Read`，也不是自动重定向原生工具。

托管 Claude 沿用 ccnm 已验证的本地工具限制，模型调用与订阅登录仍由官方 CLI 管理。外部 Host 的本地工具是否关闭由 Host 控制，不能宣称 ccnm 能消除所有旁路。

Claude Desktop SSH、Remote Control、Cowork 可以参考宿主/执行分层，但不把未公开的 Desktop/broker wire protocol 当作本项目稳定接口。PostToolUse 修改结果发生在原工具执行之后，不用它进行“远端再执行一次”的伪透明替换。

## 3. 两个实现选项与决策门

权重：复用既有执行能力 35%，兼容与安全可验证性 35%，维护/升级成本 20%，交付与退出成本 10%。当前没有运行评分，不编造加权总分；任何安全硬门禁失败都否决切换。

| 选项 | 做法 | 复用/维护 | 兼容边界 | 决策 |
| --- | --- | --- | --- | --- |
| 现状（基准） | gld/ccnm 各自执行 | 保留重复维护 | 当前公开行为 | 过渡和对照，不立即删除 |
| **A：官方执行端 + 适配** | Claude MCP → exec RPC；Codex 用原生入口；两者使用同一种官方二进制 | 不维护执行端 fork，先验证投入小 | 编辑事务等缺口不能靠转换隐藏 | **首选验证路线** |
| **B：仅派生执行端** | 保持原生协议兼容，增加 MCP 前端或必要扩展；底层 I/O/提交只保留一份 | 可补共同原语，但承担上游差异维护 | 原生客户端不会自动调用新增 Edit RPC | 只有 A 被具体缺口阻断后才评审 |
| 重新实现完整通用内核 | 自写所有文件、进程与协议，再接两产品 | 重复投入最大 | 全部保证重新证明 | 不作为默认路线 |

### A 的适用范围

先复用文件分块读取和进程执行。若旧编辑仍由 ccnm 原实现完成，应明确标为过渡：此时只统一了部分底层能力，不宣传“全部工具已共用”。

不能用两个 RPC 的 `read → 比 hash → write` 伪装 CAS，不能用客户端补偿回滚冒充执行端多文件事务。需要条件提交时，应在执行端同一受控路径实现，并明确它约束哪些合作写者、是否能覆盖外部编辑器。

### 进入 B 的条件

只在缺口清单能给出具体失败用例、原语需求和维护边界时决定 B：例如旧契约所需的条件写/journal 或必须保留的 stdin 生命周期。决策记录应比较受控 helper、最小服务端扩展与保留旧路径的成本。

不 fork Claude/Codex 的模型循环、登录或提示词。不把 Codex、Grok、ccnm 三套落盘代码并排塞入一个仓库后称为统一。新 RPC 只覆盖会实际使用它的前端；官方 Codex 仍走原生调用链，须独立验证其保护范围。

## 4. 目标调用链与责任归属

```text
Agent / Operator 侧                         Runtime 执行身份侧

官方 Codex ── 原生执行协议/受控 transport ──┐
                                           ├─ ccnm 监督包装
官方 Claude ── MCP ── SSH stdio ────────────┤   身份审计 / 项目授权 / writer guard
                                           │   ├─ 原生 RPC 前端
Web AI ── gld hub remote_* ── 公共 bridge ──┘   └─ MCP 工具适配 → exec RPC client
                                                       │
                                               受管 exec-server 子进程
                                               文件 / 进程 / 平台执行
```

图表示同一种执行实现，不要求两个 Agent 共享一个可写进程/会话。一个物理 workspace 的 coding 会话仍竞争同一写权；不同时无约束修改同树。gld 的本机执行以后按同样边界接入，而不是为本地工具制造 SSH 往返。

| 责任 | 权威位置 |
| --- | --- |
| 模型、订阅登录与官方 Agent 会话 | Agent 端官方 CLI；不转移给执行端 |
| root、OS 身份、配置、最大权限、写权 | ccnm / gld 产品受控边界；不是模型参数 |
| exec 协议版本、请求/事件相关性、句柄生命周期 | 共享 exec-client 适配代码 |
| MCP 参数、旧错误结构、行号/预算、工具集选择 | 各产品工具适配层，共享纯机制 |
| 实际字节读取、进程执行 | 选定执行引擎；不能为了“适配”又独立写一套等价执行器 |
| 编辑语义、前置版本、提交与恢复 | 已验证的执行端原语；缺失时明确保留旧提交实现或评审 B |
| Goal/Plan/History、业务验收 | 原产品/Orchestrator；不迁入 exec-server |

## 5. 不能绕过的安全和生命周期边界

### 5.1 监督进程必须活着

ccnm 包装器先审计身份、获取 writer guard，再 **spawn** exec-server 并继续监督。不能拿现有 Rust guard 后直接 `exec` 替换自身：成功 exec 不运行 Drop，带 CLOEXEC 的锁 FD 可能关闭，留下 `held` 状态而不能正常记 `released`。

关闭前停止接收新写请求，确认受管写进程结束，最后释放 guard。EOF、SSH 退出、exec-server 退出与它启动的进程结束是不同事件。若无法证明退出，保持 unknown/拒绝移交，不按时间清除锁。

gld 目前也不能假设已有覆盖 MCP/Actions/hub 与后台进程的一把会话写锁；迁移写入前要把这一能力显式建设并测试。

### 5.2 连接身份与执行权限

- MCP 路径优先沿用公开 SSH bridge，Runtime 内部用受管 stdio 连接执行端，不另开公网执行端口。
- Codex 原生 transport 按固定版本支持情况验证；若需要本机 WebSocket 桥，必须证明连接身份或不可伪造连接能力，随机端口与“只允许首连接”都不等于认证。
- 无法建立可靠认证的 transport 只做合成数据、无敏感目录的隔离实验，不进入产品。不能为绕过此门禁自动移除 `--ignore-user-config` 或复用个人配置。
- 每个请求绑定物理工作区、调用作用域、配置版本和执行会话；模型不能传入/提升 root、身份、bridge executable 或权限上限。
- 原生 RPC 路线同样需要检查，不允许只在 MCP 前端做授权，然后把原生连接当无限权限字节管道。
- 路径策略先检查原始输入，再安全打开；ccnm 对 `..` 的拒绝、gld 外部读取选项及根内 symlink 语义分别保持，不能先词法抹掉信息。

### 5.3 沙箱、凭据、配置和网络

分别验证 file read/write/remove、process、open-handle read、environment/config 与 HTTP RPC。没有 sandbox 参数的执行路径不能默认为安全。由受控配置生成权限上限，不能转发模型指定的任意 sandbox/env。

Runtime 不保存模型凭据或 ccnm 主动控制链的私钥/agent。采用明确环境白名单、受控 HOME/CODEX_HOME、FD/socket 继承策略和现有账号审计；变量名包含 TOKEN/KEY/SECRET 的检查只作辅助。

首轮只允许必需 RPC。`http/request`、远端注册/relay、环境配置读取等未证明必要的入口默认不纳入准入范围；如果官方 Codex 实际依赖某入口，先证明最小用途与权限，再决定开放。文件沙箱不自动覆盖独立网络请求，未验证 egress 就不作保证。

### 5.4 取消、恢复、重试

- `terminate` 返回已发送终止或仍 running，不等于确认退出；保留最终退出/关闭证据。
- 官方 WebSocket 的 detached/resume 行为不能直接套到 ccnm v1。首轮不新增对外 resume：连接失效则句柄失效，对账或 unknown，禁止偷偷恢复后重放。
- 写入和进程启动默认不自动重试。同键去重仅在后端契约已验证时使用；`writeId` 的 stdin 去重不等于整个命令、事务或模型任务的 exactly-once。
- 不能把未知结果记成失败再换旧后端执行。同一任务中后端不热切；读句柄、进程 ID、输出引用都绑定连接代次。

## 6. 共享代码和旧契约

### 6.1 最小模块，按已证明需要建设

先用本仓库的 `spike/`、`fixtures/` 和 `conformance/` 完成验证，以下名称只是职责划分，不要求立即拆成多个 crate：

| 模块 | 只负责 | 不负责 |
| --- | --- | --- |
| exec-client | 固定版本 codec、初始化、请求 ID、事件、分块读取与进程句柄 | 账号、模型、授予权限、通用网络转发 |
| tool-adapter | MCP 工具到已批准执行操作的映射、旧结果投影、预算 | 复制一整套 filesystem/process 实现 |
| text | 行片段、增量编码校验、截断；输入可为已授权的分块流 | 默认强制所有产品用同一种编码/BOM/行数策略 |
| edit/commit（按决策门） | 原有编辑模式适配、必要条件写/journal 原语 | 把多次 write RPC 包装成虚假的原子事务 |
| conformance | 中立协议客户端、已知失败反例、故障注入与资源检查 | 只比较格式，不检查真实落盘与进程状态 |

A 路线共享的执行代码在官方二进制中，本仓库共享客户端和必要机制。若最终需要共享可直接链接的执行库，在 B 中明确提取边界。不要为了仓库叫 kernel，就必须先自行实现整个内核。

### 6.2 兼容矩阵必须先于迁移

| 项目 | 旧模式要求 | 新语义如何进入 |
| --- | --- | --- |
| ccnm `ccnm.workspace-mcp/1`、`ccnm.machine/1` | 保留已冻结参数、权限、错误与生命周期；保留显式 version 与顺序依赖 edits | 破坏性变化按协议版本升级，不由模型 A/B 决定能否破例 |
| gld 默认工具集 | 保留文本 patch/Unified Diff、现有输入输出与范围；已确认错误定位单独修复 | `.env*` 新禁读规则、模糊匹配等作为显式新策略/工具版本，不夹带进抽库 |
| JSON Schema | 分别测试省略、null、合法值、非法值 | 可空联合是语义，不能当去噪删除；去除元数据也需证明不改变消费行为 |
| 输出 | 保留旧 `content/isError/structuredContent` 契约和已声明 outputSchema | 新纯文本工具面单独试验；不能声称所有 Host 永远只看某一字段 |
| 读取与搜索 | 保留编码、BOM、范围、总行数、ignore、排序、截断约定 | 新默认预算、自动上下文、mtime 排序只进明确版本 |
| stdin/PTY | 原 gld 能力不能被缺少 closeStdin/resize 的后端静默替代 | 缺口明确拒绝/保留旧 backend，或补足后按能力迁移 |

读取记录若用于写入前置检查，必须由产品注入 opaque `ReadScope`，绑定调用者/执行会话、物理 root、文件身份与内容版本。完整读取指同版本内容已完整交付给该作用域，不是后台扫描到了 EOF；分页范围合并、失效和跨主体隔离需测试。

旧 ccnm 的 `a→b、b→c` 顺序编辑不能因新模式“全部对原文匹配”而丢失，也不能按使用比例超过 2% 才恢复。新的匹配档位必须明确选择；歧义拒绝、重叠规则与错误诊断有各自预算。

## 7. gld hub 仍走公共 ccnm bridge

本节可独立推进，不必等待全部 exec-server/编辑能力迁移。第一版是远端工具调用，不提交自主 Agent 任务。

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

编号使用 `V2-`，不与 v1 P0–P5 或 ccnm 既有 P0–P12 混用。当前全部待实施。

| 阶段 | 交付 | 通过/停止条件 | 模型调用 |
| --- | --- | --- | --- |
| V2-P0 证据与实验基线 | 固定二进制/源码/schema；旧兼容 fixture；最小 RPC 允许集；平台、权限和预算矩阵；记录取得构建物的方式 | 能从空临时目录重建；能力/证据不足先记录，不修改生产配置 | 0 |
| V2-P1 双入口无模型闭环 | 一个原生 RPC 中立客户端，一个 MCP 中立客户端；MCP adapter 实际调用同一种官方 exec-server；完成分块读与进程输出 | V2-G01–G04 通过；同一项目先后经两入口互相核对结果，不能用两个 fake 后端冒充执行引擎复用 | 0 |
| V2-P2 监督与权限门禁 | 真正持有 guard 的监督器；文件/进程/配置/网络逐入口策略；取消、断连、输出上限、跨入口互斥 | V2-G05–G10 适用项通过；身份/执行位置/权限/写权不确定即停止产品接入 | 0 |
| V2-P3 编辑与交互缺口决策 | 实测旧 Edit/Write/patch 与 stdin 契约；写明 A 可覆盖/保留旧路径/必须 B 的清单 | V2-G11 通过才能迁移相应能力；决定 B 前形成最小扩展规格和维护成本说明 | 0 |
| V2-H hub 接入线 | AuthContext、静态 remote 工具、合成 peer、公开 bridge、read 后 coding | 先离线，再已授权真实 Runtime；V2-G12 通过；不依赖可选工具面 A/B | 离线 0；Web 实际使用单独记账 |
| V2-P4 产品 opt-in | ccnm 原生 Codex与 Claude MCP分别接真实链；gld按能力接入；模式显式选择，不切默认 | V2-G01–G12 对启用平台/能力全部通过；先用中立客户端跑真实 transport，再做少量官方 Agent 回合；缺口不以换工具集掩盖 | 仅有明确预算时 |
| V2-P5 优化与收敛 | 可选 alwaysLoad/native@1/lean@1、小样本 A/B、升级/回退演练；仅删除已替代的重复代码 | 硬门禁和旧协议均通过后再比较体验；是否设默认/发布另作决定 | 按实验单批准 |

依赖：P0 → P1 → P2 → P3 → P4；H 的离线设计可与 P1 并行，coding 仍受其自身身份/写权/恢复门禁约束。P5 不阻塞基础复用和 hub 交付。若 P3 决定采用 B，扩展后的引擎须重跑 P1/P2 和相关兼容测试，不能继承官方原版的通过记录。

ccnm 实际改代码前按其规则立新阶段、更新唯一状态源。本计划记录跨仓依赖，不代替产品状态。不为了宣称统一而同时开展多个互相覆盖的核心重构。

### 第一批可执行任务

1. 保存固定版本的协议能力表、旧成功/拒绝 fixture、原始证据索引；确认已有可用工具链，不自动安装。
2. 写两个不调用模型的协议客户端，验证真实 exec-server 的握手、分块读、进程启动/输出/退出。
3. 给监督器写 guard 保持/关闭失败/unknown 测试，给 transport 写未授权连接和错误主体测试。
4. 输出一张“已复用 / 缺口 / 旧路径保留 / 必须扩展”的清单，再决定实现多少共享内核。

## 9. 硬门禁与预算

| 编号 | 必须验证的行为 |
| --- | --- |
| V2-G01 协议 | 真正支持的初始化和 RPC 字段；未知版本/方法/超大帧拒绝；README 与源码不同以固定版本实测为准 |
| V2-G02 同引擎 | MCP 与原生客户端调用同一实现，返回实际文件和进程证据；不能只比较两段相同字符串 |
| V2-G03 读取 | UTF-8 跨块、BOM、非法编码、空文件、CRLF、无尾换行、巨型单行、分块期间原地修改；句柄及时关闭，不把句柄当快照 |
| V2-G04 进程 | argv/cwd/env、双流排空、seq 游标、writeId 有界去重、signal/exit 区分、自然退出与取消；缺 closeStdin/resize 明确反映能力 |
| V2-G05 身份 | 未授权连接、抢首连接、跨主体/工作区/配置代次句柄、合成凭据不可访问；无原始 token 日志 |
| V2-G06 权限上限 | file/process/open-handle/config/http 分别验收；原生和 MCP 同样约束；缺 sandbox 不默认放行，原始父路径/根内外 symlink 按产品契约处理 |
| V2-G07 写权 | 同物理资源跨入口竞争；同会话并发修改；监督进程全程持锁；未确认子孙退出不写 released，不按期限夺锁 |
| V2-G08 故障 | 前端断开、SSH 黑洞、监督器/服务/子进程分别崩溃、在途超时；已执行未回包保留 unknown，不重放；stdio/ws 生命周期分别记录 |
| V2-G09 资源 | 头尾边界、多字节、200 MiB 连续输出、磁盘配额/写失败、过期引用、快速完成后续读；内存和磁盘均有界 |
| V2-G10 兼容 | 旧 schema 的省略/null/非法值、旧错误与预算、read/exec/patch范围；错误修复单独记录，不重录 golden 掩盖漂移 |
| V2-G11 写入语义 | 顺序依赖 edits、重复片段、create-only、stale版本、ReadScope隔离与截断交付、多文件中途失败、恢复再次中断、权限位/Windows替换；未满足就保留旧写路径 |
| V2-G12 产品链 | hub 本地成员零回归；远端无本地 Planning/Harness 副作用；真实 Web/CLI 的执行位置、返回结果、关闭和回退有证据；官方 Agent 回合与无模型测试分开记 |

### 实验前冻结，不事后调整判据

V2-P0 按“平台 × 前端 × 操作”明确必测/不适用、拒绝策略和预算。权限、执行位置、结果真实性不能列为不适用；未暴露的能力才可排除。下列为首轮拟定上限，不是已测试保证，实施前可有依据地调整并记录：

- 普通协议请求 deadline 10 秒；文件 readBlock 最大 1 MiB，模型预览默认 16 KiB、上限 32 KiB；JSON/base64 包装单独计量。
- 单流输出保留默认最多 64 MiB、每会话总保留最多 256 MiB；文件数量和整服务总配额另定。达到预算仍排空进程管道并记录丢弃/截断，不因不再存盘而让子进程死锁。
- 活动写进程不因缓存淘汰而丢失监督和写权；只清理已确认终态且允许过期的结果。磁盘写失败独立上报，不能伪造命令失败或成功。
- 超时测试用 2 秒期限，取消后 5 秒内确认普通受管后代退出和管道回收；逃离进程组的对抗情形另列平台隔离边界。
- 读取测试使用生成式流和 8/128 MiB 样本，固定页面预算；记录峰值 RSS、扫描量及耗时，禁止整行无界缓存。原 RPC 的大整文件分配不因返回内容被截断就算有界。
- 并发、取消、请求已执行未回包等选定故障点各至少重复 20 次；零越权副作用、零被隐藏的错误片段写入、零违反已声明重试语义的重复执行。

平台与内核能力未验到时只声明已验证范围，不以 Windows 交叉编译代替进程树/文件替换实测。新上限与旧契约冲突时，不覆盖旧默认：调整产品预算、缩小启用能力或进入新版本，须明确决策。

## 10. 模型验证与工具面优化：后置、可观测、有限额

### 10.1 先闭环，再比较

无模型门禁通过后，再用官方 Claude/Codex 各做最小真实回合：读取固定文件、修改允许目标、运行确定性测试、报告结果与执行位置。只有已有授权覆盖该范围时运行；新额度、系统部署或权限变化按既有规则确认。

每个实验单列 `max_runs`、重试是否计入、deadline、停止条件和授权引用。预算用“提供方 × 任务 × 组数 × 重复次数 + 冒烟/重试”明确计算；历史对照复用必须证明版本/模型/配置/夹具一致，不同时声称与本轮随机交替运行。v1 的 145 次不自动延续到本版。

### 10.2 正确性先于 token

- 基线本身须有效；连接、认证、缺工具导致的低成本失败不是优化收益。
- 按任务冻结最低成功要求，逐任务比较，不跨任务抵消退步。0/5 对 0/5 无论多便宜都不得通过。
- 身份、目录、审批、guard、恢复和兼容硬门禁一项未解决，都不能靠 token/调用次数优势切默认。
- 成功任务资源消耗与失败成本分别报告；记录墙钟、input/output、cached usage 可见字段、工具次数及副作用证据。少量样本只能支持有限结论，追加运行不得超出预算。
- CLI 未公开的实际 tools/system/messages 不伪造 hash。只 hash 自己控制的 MCP schema、instructions、输入和配置，缓存变化最多先描述相关性。

### 10.3 新工具面单独版本化

alwaysLoad、短 instructions、native@1、lean@1、纯文本输出、自动上下文、大纲和重复调用提示都是后置实验，不绑进底层机制迁移。MCP 名称相似不代表 Claude 原生权限规则/hooks 自动适用；纯文本模式若保留 outputSchema，仍须符合 MCP 输出要求。

不以未锁定版本的宿主行为矩阵、外部项目百分比或单段字符串 tokenizer 对比，直接承诺本项目节约比例。

## 11. 工程、回退和阶段状态

### 工程与来源

- 不先统一三个仓库工具链。基础纯文本代码可维持 Rust 1.85；需要编译链接 1.89+ 或更高依赖时列出实际依赖闭包、平台和迁移理由，再决定产品 MSRV。独立 exec-server 的构建要求单列。
- 只建立有真实消费者的模块；共享物可以是固定上游二进制加共同客户端，不必全部是本仓库自行实现的 crate。
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
| v2 方案文档 | 已生成，待评审 | 本文件；不代表采用已验证 |
| V2-P0–V2-P5 / V2-H | 未开始 | 无运行/部署/模型验收记录 |

本轮只生成 Markdown、更新 README 入口并保留 v1 原文。不安装依赖、不构建 exec-server、不运行模型或 SSH、不修改 gld/ccnm 产品状态与执行路径。后续结果放入可追溯的 evidence 目录，记录命令、固定版本、输入/输出 hash、OS/身份、通过/失败/跳过与限制；真实秘密不进入证据。

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
