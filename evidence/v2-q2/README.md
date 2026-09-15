# V2-Q2：alwaysLoad 对照

对应 [v2 方案](../../docs/plan/implementation-plan-v2.md) 第 8 节 V2-Q 线 Q2。

## 结论先行（2026-09-16，不耗额度的部分）

- **Managed 路径用不上 alwaysLoad。** ccnm 启动 Claude Code 时传 `--tools ""`（`crates/ccnm-core/src/provider/claude/mod.rs:269`），这同时让 ToolSearch 不可用，Claude Code 日志写 `Tool search disabled: ToolSearchTool is not available`、`Dynamic tool loading: 0/0 deferred tools included`（ccnm P13 的真实连接记录）。7 个工具本来就全量加载。
- **只有外部入口（用户自己的 Claude Code 接 `ccnm mcp bridge`）会被延迟加载**，alwaysLoad 只在这里有意义。
- 机制已在真实 Claude Code 2.1.269 上确认：同一份外部 coding 会话，服务器配置不加 `alwaysLoad` 时延迟池 21 个工具，加上后 14 个，少的正是 ccnm 的 7 个。两次都未登录、`input_tokens` 0。
- 模型侧对照（alwaysLoad 是否减少回合/调用、是否增加每次请求的 token）**还没跑**：本机 CLI 未登录，需要用户在自己终端登录。

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

## 模型对照实验单（冻结，未运行）

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

## 运行前需要用户做的

在自己的终端确认并登录（这台机器上 CLI 目前未登录）：

```bash
claude auth status
```

登录后告诉我：先写模型对照脚本并提交，再按上面冻结的判据运行。
