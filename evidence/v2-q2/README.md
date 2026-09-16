# V2-Q2：alwaysLoad 对照

对应 [v2 方案](../../docs/plan/implementation-plan-v2.md) 第 8 节 V2-Q 线 Q2。

## 结论先行（2026-09-16，不耗额度的部分）

- **Managed 路径用不上 alwaysLoad。** ccnm 启动 Claude Code 时传 `--tools ""`（`crates/ccnm-core/src/provider/claude/mod.rs:269`），这同时让 ToolSearch 不可用，Claude Code 日志写 `Tool search disabled: ToolSearchTool is not available`、`Dynamic tool loading: 0/0 deferred tools included`（ccnm P13 的真实连接记录）。7 个工具本来就全量加载。
- **只有外部入口（用户自己的 Claude Code 接 `ccnm mcp bridge`）会被延迟加载**，alwaysLoad 只在这里有意义。
- 机制已在真实 Claude Code 2.1.269 上确认：同一份外部 coding 会话，服务器配置不加 `alwaysLoad` 时延迟池 21 个工具，加上后 14 个，少的正是 ccnm 的 7 个。两次都未登录、`input_tokens` 0。
- **模型侧对照已跑完（2026-09-16，18 格全通过）：加 alwaysLoad 每次任务省掉一个回合和一次 ToolSearch 调用，成功率不变，墙钟和 token 都不更差。按冻结的判据 → 采纳 B。**见下面"模型对照结果"。

## Host 行为依据（静态，2.1.269）

- 默认模式：`ENABLE_TOOL_SEARCH` 未设、官方 API 时 `mode=tst`，MCP 工具**一律延迟**，不看数量阈值。
- 是否延迟：`function SY(e){if(e.alwaysLoad===!0)return!1; if(Tt(e))return!1; if(e.isMcp===!0)return!Xye(); return e.shouldDefer===!0}`。
- 工具的 `alwaysLoad`：`alwaysLoad: d || tool._meta?.["anthropic/alwaysLoad"]===!0`，`d` 是该服务器配置里的 `alwaysLoad`。所以**服务器配置 `"alwaysLoad": true` 和逐个工具 `_meta` 对延迟的效果相同**。
- 两者的差别：配置了 `alwaysLoad` 的服务器会在首轮请求前被等待连接完成（连接分组 `qe.alwaysLoad===!0`），`_meta` 不影响连接顺序。对 ccnm 这种"没有它就干不了活"的服务器，等连接是想要的行为。

## 零额度机制检查

脚本 [`defer_check.sh`](defer_check.sh)（`defer_check.sh <ccnm 二进制> <仓库外目录> <true|false>`）：临时 Runtime 配置 `external_mcp = "coding"`、`allow_unconfined_exec = true`；MCP 命令用 `/usr/bin/env -i` 启 `ccnm internal mcp-serve`（原因见 ccnm `docs/research/p13-instructions-host-cap-2026-09-16.md` 的两个坑）；`env -i` 起未登录的 `claude -p`，**不传 `--tools ""`**，让 ToolSearch 可用。

| 服务器配置 | debug 日志 | 延迟池 |
| --- | --- | --- |
| 不加 `alwaysLoad` | `[ToolSearch:optimistic] mode=tst … result=true`；`Dynamic tool loading: 0/21 deferred tools included` | 21 |
| `"alwaysLoad": true` | 同上；`Dynamic tool loading: 0/14 deferred tools included` | 14 |

两次都 `Not logged in`，`input_tokens` 0。ccnm 构建为 0.7.0（`9532b1c`）。

## 模型对照实验单（冻结，已于 2026-09-16 执行）

只测外部入口。

| 项 | 值 |
| --- | --- |
| 授权引用 | `user-consent-2026-09-15` |
| 组 | A：服务器配置不加 `alwaysLoad`；B：`"alwaysLoad": true`（不改 ccnm 代码；与 `_meta` 对延迟效果相同，见上） |
| 任务 | T1 只读：问一个只在夹具文件里的数（答案随机生成，不可猜）；T2 修一个让单元测试失败的 bug 并跑测试；T3 加一个函数和测试并跑通 |
| 次数 | 3 任务 × 2 组 × 3 次 = 18，加 2 次冒烟，`max_runs` = 20；累计上限 145，此前 0 |
| 模型与客户端 | Claude Code 2.1.269，`--model sonnet`，两组相同；每次全新夹具副本 |
| 判定成功 | 看副作用不看模型文字：T1 答案与夹具一致；T2/T3 事后在夹具上重跑测试通过、且改动只在允许的文件 |
| 记录 | `stream-json` 里的回合数、ToolSearch 调用次数、ccnm 工具调用次数；`result` 的 input/output/cache 读写 token、墙钟 |
| 采纳 B 的条件 | 每个任务 B 的成功次数 ≥ A；且 B 的 ToolSearch 调用为 0；且每任务中位墙钟不劣于 A 20% 以上、中位总输入 token（含 cache）不劣于 A 10% 以上。任一任务成功次数下降即不采纳 |
| 停止 | 冒烟失败两次（非模型原因）就停；不追加次数 |

采纳后的落地（另立 ccnm 阶段）：外部入口文档示例的服务器配置加 `"alwaysLoad": true`，或在 7 个工具上加 `_meta`（后者用户不改配置也生效，但不会让首轮等待连接）。Managed 路径不需要改。

## 模型对照结果（2026-09-16）

在 fodelf（Agent 机器，Claude CLI 已登录）上跑，驱动脚本是 [`ab.py`](ab.py)，夹具在
[`fixture/`](fixture)，每一格的原始指标在 [`runs/`](runs)（只留 `cell.json`，夹具副本和
stream-json 留在机器上的 `~/q2-out`，没入库）。

**18 格全部通过，两组成功率都是 3/3。** 差别只在过程：

| 任务 | 组 | 成功 | ToolSearch 调用 | ccnm 工具调用 | 回合中位 | 墙钟中位 | 输入中位（含 cache） | 每次约 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| T1 只读 | A | 3/3 | 3 | 5 | 4 | 14.0 s | 128041 | $0.0845 |
| T1 只读 | B | 3/3 | **0** | 6 | **3** | **11.5 s** | **102268** | **$0.0705** |
| T2 修 bug | A | 3/3 | 3 | 21 | 9 | 27.0 s | 244692 | $0.1311 |
| T2 修 bug | B | 3/3 | **0** | 21 | **8** | **24.4 s** | **211847** | **$0.1097** |
| T3 加函数 | A | 3/3 | 3 | 22 | 10 | 29.0 s | 244173 | $0.1355 |
| T3 加函数 | B | 3/3 | **0** | 20 | **8** | 23.3 s | 245734 | $0.1205 |

（ToolSearch 调用和 ccnm 工具调用是三次的**合计**，其余是中位数。）

**规律非常整齐**：A 组每一格都恰好调一次 ToolSearch，然后 ccnm 的工具序列跟 B 组
一模一样；B 组一次都不调。省下的就是那一个回合。`builtin=0` 是 18 格的共同结果——
模型全程没用自己的 Read/Bash，对照没被污染（做法见 `ab.py` 头部第 2 条）。

判据逐条：三个任务的成功次数 B ≥ A、B 的 ToolSearch 全为 0、墙钟没有比 A 差 20%
以上、总输入没有比 A 多 10% 以上（T3 的 245734 vs 244173 是 +0.6%）。**全满足 →
采纳 B。**

### 跟实验单不一致的地方，以及一处判分 bug

- **客户端版本**：实验单写的是 2.1.269（那是做零额度机制检查的机器），实际跑的
  fodelf 上是 **2.1.272**。两组同版本，不影响组间比较。
- **判分 bug（已修）**：第一轮 18 格里有 12 格被判成"改了不该改的文件"，改的是
  `__pycache__`。任务本身就要求跑测试，跑测试就会生成它，它是产物不是改动。
  修法是 `diff -rq -x __pycache__ -x '*.pyc'`，并加了 `ab.py regrade` 子命令——
  **重判分不重跑模型**，夹具副本还在原地，所以这次改判没有多花一分钱额度。
- **缓存**：跑的顺序是「每一轮里 t1a t1b t2a t2b t3a t3b」，两组交替，所以 API 侧
  的 prompt 缓存对两组是对称的。统计的输入 token 是 `input + cache_creation +
  cache_read` 的合计，不是只数未命中的那部分。

### 额度

20 次（2 冒烟 + 18 正式），**合计 $2.1454**，都在 `user-consent-2026-09-15` 的
145 次上限内。此前该授权下已用 0 次，现在累计 20 次。

## 落地（要另立 ccnm 阶段，本轮没做）

实验单里"采纳后的落地"给了两条路，选**服务器配置**那条：本轮就是这样测的，
`_meta` 那条没有实测数据，而且它不会让首轮等待连接。

## 怎么复跑

在 Agent 机器上（Claude CLI 得是登录状态）：

```bash
python3 ab.py smoke ~/.local/bin/ccnm ~/q2-out    # 两格冒烟，不过就停
python3 ab.py run   ~/.local/bin/ccnm ~/q2-out    # 18 格
python3 ab.py report ~/q2-out
```

输出目录别放在仓库里。改了判分标准就跑 `ab.py regrade ~/q2-out`，不用重跑模型。
