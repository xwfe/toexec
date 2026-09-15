# V2-Q1：Claude Code 会把 MCP instructions 截到多长

对应 [v2 方案](../../docs/plan/implementation-plan-v2.md) 第 8 节 V2-Q 线 Q1、第 13 节 ccnm 最后一项。

## 实验单（运行前冻结）

| 项 | 值 |
| --- | --- |
| 授权引用 | `user-consent-2026-09-15` |
| `max_runs` | 2：正式 1 次；只有连接/认证/CLI 报错这类没进到模型判断的失败才补 1 次，补跑计入 |
| 本实验前累计 | 0 / 145 |
| deadline | 单次 5 分钟 |
| 停止条件 | 拿到一次模型实际作答即停；两次都失败则记失败并停止，不再追加 |
| 客户端 | Claude Code 2.1.269（Homebrew cask `claude-code@latest`），macOS arm64，`--model sonnet` |
| 隔离 | `--strict-mcp-config` 只挂探针；`--tools ""` 且禁用探针唯一工具，模型只能凭上下文作答；标记值每次随机生成、只在进程环境里 |

### 探针版面

`python3 probe_server.py --layout` 的输出：说明共 3018 个 UTF-16 码元、8114 个 UTF-8 字节（中文填充，每字 3 字节）。五个标记：

| 标记 | UTF-16 起点 | UTF-16 终点 | UTF-8 字节起点 |
| --- | --- | --- | --- |
| Z | 300 | 317 | 820 |
| A | 900 | 917 | 2430 |
| B | 2000 | 2017 | 5410 |
| C | 2100 | 2117 | 5660 |
| D | 3000 | 3017 | 8090 |

### 判据（运行前写定）

| 模型实际看到 | 结论 |
| --- | --- |
| Z、A、B，看不到 C、D，末尾有 `[truncated]` | 按 2048 个 UTF-16 码元截断（与静态证据一致） |
| 只有 Z | 按 2048 字节截断 |
| Z、A，看不到 B | 按约 2000 字符截断 |
| 五个都在 | 这条路径不截断 |
| 其他组合或列出不存在的标记 | 结果无效，不下结论（计入次数，不补跑） |

## 静态证据（不耗额度）

从本机二进制 `strings` 抽出的打包 JS（`GIT_SHA d0733697`，`BUILD_TIME 2026-09-11T17:33:46Z`）：

```js
var FT=2048
function Ro(e,n){if(!e)return e;return Ao(e,"Server instructions",n)}
function Ao(e,n,r){if(e.length<=FT)return e;
  if(r!==void 0)J(r,`${n} truncated from ${e.length} to ${FT} chars`);
  return ne(e,FT)+"… [truncated]"}
```

连接建立后 `Ro(getInstructions(), serverName)` 处理一次再存入连接对象。`e.length` 是 JS 字符串长度，即 UTF-16 码元数；`ne` 截断时避开半个代理对。所以"2KB"准确说是 **2048 个 UTF-16 码元**：纯 ASCII 约 2 KiB，中文约 6 KiB。

## 结果

（运行后填写）
