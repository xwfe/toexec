# V2-P：Claude 经 exec-server 的收益验证（原始记录）

判据、结论和限制只写在 `docs/plan/` 下：[V2-P0 冻结文档](../../docs/plan/v2-p0-claude-trial-baseline.md)、[V2-P1 结果](../../docs/plan/v2-p1-claude-trial-result.md)。这里放脚本和结果文件。全程零模型额度，只在本机跑，不连外部主机。

| 文件 | 做什么 |
| --- | --- |
| `p0/mcp_client.py` | 中立 MCP 客户端：本机起 `ccnm internal mcp-serve`（受管 open，coding），计两个方向的字节 |
| `p0/baseline.py` | 生成合成夹具，钉住版本，测直接路径 D 的探针和开销 |
| `p1/exec_client.py` | 中立 exec-server 客户端：本机起 `ccnm internal exec-serve`，`process/start` 带录下的 workspace-write 沙箱，分块读用 open/readBlock/close；以及用同一个权限对象跑 `codex sandbox` |
| `p1/trial.py` | V2-P1 全部比较：探针 D/A/S 交替，文件 RPC 读取，子进程环境，M1–M3 交替测量，收尾检查 |
| `p0/runs/*/baseline.json`、`p1/runs/*/p1.json` | 结果文件；原始日志不提交 |

复跑（ccnm 用 release 构建；运行目录不能在 `/tmp` 或 `$TMPDIR` 下，脚本会拒绝）：

```bash
python3 p0/baseline.py p0/runs/<新目录>
python3 p1/trial.py p1/runs/<新目录>
```

`trial.py` 开跑前核对 ccnm 和 Codex 二进制的 sha256 与 P0 结果一致，不一致就退出，要先重跑 P0。
