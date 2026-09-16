# ccnm P24：Codex 原生链真机验收（原始记录）

结论、和计划不一样的地方、发现与限制只写在 ccnm 的 [`docs/research/p24-native-real-machine-2026-09-16.md`](https://github.com/xwfe/ccnm/blob/main/docs/research/p24-native-real-machine-2026-09-16.md)。这里放脚本、每项的结果文件和复跑方法。授权清单在 ccnm [`docs/plan/p24-native-real-machine-session.md`](https://github.com/xwfe/ccnm/blob/main/docs/plan/p24-native-real-machine-session.md)。

2026-09-16。Agent：macOS 26.6.2 arm64（`bing`，临时 Controller）；Runtime：hpsrv，Debian 13 / x86_64（`ccrun`，uid 1002）。两边 ccnm 都是 `7ae2d4b` 构建，Codex 都是 0.154.0。模型额度共用 3 次。

## 拓扑

```text
本机：tmux → ccnm supervise → codex（CODEX_HOME = 会话的 codex-home）
                                  └─ environments.toml 的 program：ccnm internal exec-transport
                                       └─ exec 成 /usr/bin/ssh -T ccnm-p24-hpsrv … internal exec-serve --payload <protocol 6>
hpsrv（ccrun）：sshd-session → ccnm internal exec-serve（规则表、写锁、进程扫描）→ codex exec-server --listen stdio
                                                                                   └─ bwrap … codex-linux-sandbox → 命令
```

中立客户端（`native_client.py`）走的是同一条 ssh：选项与 ccnm 的 `Ssh::exec_transport_cmd` 逐项相同（ccnm 单元测试钉住），请求取自 P21 录下的 Codex 0.154.0 真实请求（`ccnm/tests/fixtures/codex-0.154.0/exec-server/`），只把路径改成 hpsrv 上的。它不 import ccnm 和 Codex。

## 文件

| 文件 | 做什么 |
| --- | --- |
| `hpsrv-runtime.sh` | root 那两步（A1 公钥、A2 bubblewrap），`--check`/`--apply`/`--revoke-key`/`--revert`，清单记在 `/var/lib/ccnm-p24-20260916`；公钥和指纹写死，执行前重算 |
| `native_client.py` | 中立客户端：`exec-serve` / `mcp-serve` / 任意命令（外部 bridge）；在 hpsrv 上读锁标记、找带会话标记的进程 |
| `gate.py` | 零额度门槛：规则表放行与拒绝（看磁盘不看回包）、P12.2 身份检查（账号本身和执行端沙箱里各一遍）、关闭后的锁与残留 |
| `locks.py` | P24.2：三个入口轮流持锁、互相抢；真实 Codex 空闲会话当持锁方和抢锁方 |
| `faults.py` | P24.3：`agent-ssh` / `runtime-sshd` / `executor` / `setsid` / `supervisor` / `freeze` / `blackhole` |
| `spotcheck.py` | P24.3 真实 Codex 抽查：命令运行中掐断传输 / 杀 exec-server（各 1 次模型） |
| `runs/` | `gate.json`、`locks.json`、`faults.json`、`spotcheck.json`、`p24-1.json`（P24.1 会话的工具调用、退出和两边核对） |

## 复跑

前提：hpsrv 上 `ccrun` 有本轮公钥、装了 bubblewrap、`~/.local/bin/ccnm` 与 Codex 就位、`~/.config/ccnm/config.toml` 有 workspace `p24`（形状见 ccnm 研究记录第一节）；本机有别名 `ccnm-p24-hpsrv`、`~/.config/ccnm/p24-agent.toml` 和临时 Controller。**这些在本轮收尾时都撤掉了**（hpsrv 上保留 bubblewrap、`ccrun` 名下的 ccnm 和 Codex 二进制，公钥已撤；清单在 `/var/lib/ccnm-p24-20260916`），复跑要按授权清单重建。

```bash
python3 gate.py runs/gate.json
python3 locks.py 3 runs/locks.json
python3 faults.py agent-ssh 20 runs/faults.json      # runtime-sshd / executor / setsid / supervisor 同理
python3 faults.py freeze 5 runs/faults.json
python3 faults.py blackhole 5 runs/faults.json       # 要 hpsrv 的 root：规则只丢这一条连接，60 秒后由 systemd 定时器删除
python3 spotcheck.py transport runs/spotcheck.json   # 各花 1 次模型额度
```

## 结果

| 项 | 结果 |
| --- | --- |
| 门槛 | 39/39 |
| P24.1 真实任务 | 读 → 改 → 跑测试通过，改动属主 `ccrun`，81 秒，退出码 0；结束后锁 `released`、无残留 |
| P24.2 抢锁 | 74/74（中立矩阵 3 轮 + 真实 Codex 当持锁方、抢锁方各一次） |
| `agent-ssh` × 20 | 20/20，第一次查询（经 ssh 约 0.3 秒）时已 `released`，无残留 |
| `runtime-sshd` × 20 | 20/20，同上 |
| `executor` × 20 | 20/20，同上 |
| `setsid` × 20 | 20/20，同上；Linux 上 setsid 逃不出 bwrap 的 PID 命名空间，ccnm 扫描 0 次需要动手 |
| `supervisor` × 20 | 20/20：锁保持 `held`，执行端进程 ≤ 1.1 秒内消失，下一个会话被拒为 `left held by an interrupted process`，人工恢复后可重开 |
| `freeze` × 5 | 5/5：冻住 60 秒期间锁不移交，恢复后同一会话继续可用 |
| `blackhole` × 5 | 5/5：定时器都按时删了规则；**锁 0/5 自己释放**，从掐断到观察结束 222.9–223.4 秒一直 `held`（Agent 静默离网，Runtime 察觉不到）；结束孤儿 `sshd-session` 后都正常 `released`、无残留 |
| 真实 Codex 抽查 × 2 | 掐传输、杀 exec-server 各 1 次：0.3–0.4 秒放锁，命令被清掉，Codex 不重连、不重放 |
