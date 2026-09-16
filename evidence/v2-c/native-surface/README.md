# Codex 连"项目只在对面"的 exec-server：实测原始记录（ccnm P21）

结论、冻结的方法规则表和对 ccnm 设计的改动只写在 ccnm 的 [`docs/research/p21-codex-native-surface-2026-09-16.md`](https://github.com/xwfe/ccnm/blob/main/docs/research/p21-codex-native-surface-2026-09-16.md)。这里放脚本、每个实验做了什么、3 遍的结果，以及怎么复跑。

Codex 0.154.0，2026-09-16。**零模型额度**：模型接口是本机假服务，Codex 外面套 `sandbox-exec` 禁非本机出站，`HOME`/`CODEX_HOME` 是临时目录。

## 拓扑

```text
macOS：Codex（Homebrew 0.154.0）── ws ──> probe.py 里的逐帧网桥 ── ws ──> 容器端口 47121
                                         记录两个方向的 JSON-RPC；
                                         可按规则替 exec-server 回答
Linux 容器：codex exec-server（官方 musl 发行包，用户 runner）
           项目 /srv/p21/work（Mac 上没有这个路径），"工作区外" /home/runner/outside
```

## 复跑

1. 取 Linux 发行包：`https://github.com/openai/codex/releases/download/rust-v0.154.0/codex-aarch64-unknown-linux-musl.tar.gz`，本轮 sha256 `583b48df32804213bdcd338c2e5adb06b34340821fa757a726cc0a524fa33c27`，解压到 `<dir>`。
2. 起容器。**必须放开 seccomp**，否则 bubblewrap 建不了 user namespace，Codex 的 Linux 沙箱起不来：

   ```bash
   docker run -d --name p21-exec --security-opt seccomp=unconfined -p 127.0.0.1:47121:47121 -v <dir>:/opt/codex:ro debian:bookworm-slim sleep infinity
   docker exec p21-exec sh -c 'useradd -m -u 1000 runner && ln -s /opt/codex/codex-aarch64-unknown-linux-musl /usr/local/bin/codex && mkdir -p /srv/p21/work /home/runner/outside && chown -R runner:runner /srv/p21 /home/runner/outside && apt-get update -qq && apt-get install -y -qq bubblewrap git'
   docker exec -u runner -d p21-exec sh -c 'mkdir -p /home/runner/srvhome/.codex && exec env -i HOME=/home/runner/srvhome CODEX_HOME=/home/runner/srvhome/.codex PATH=/usr/local/bin:/usr/bin:/bin codex exec-server --listen ws://0.0.0.0:47121'
   ```

   `CODEX_HOME` 别放 `/tmp` 下：exec-server 会告警 `Refusing to create helper binaries under temporary dir`。
3. 单个实验：`python3 probe.py <实验名> <新输出目录>`；三轮：`batch.sh`（写 `runs/r1..r3`）；比对：`python3 compare.py runs/compare.json`。交互模式的实验用 `tmux -L p21-<pid>` 起一个独立的 tmux 服务，自动确认"信任目录"，审批按实验设定回答 `y` 或 `Esc`。
4. 入库的只有每次运行的 `summary.json` 和 `compare.json`，本机临时目录的路径换成了 `<scratch>` 或 `<tmp>`。

## 实验与结果

"3 遍"一列由 `compare.py` 判定：比 Codex 退出码、计划是否走完、审批次数、网桥连接数、发出的方法集合、每条请求的（方法、路径、sandbox 在不在、结果、sandbox 条目）集合、被网桥拦下的请求、容器里留下的文件、模型收到的标记、命令输出。不比耗时、进程号、会话 id，也不比启动时 `.git` 上溯重复的遍数（3–4 遍，随时序变）。

| 实验 | 做什么 | 结果 | 3 遍 |
| --- | --- | --- | --- |
| `exec-remote-cwd` | `codex exec -C /srv/p21/work` | rc 1，`Error: No such file or directory (os error 2)`，网桥 0 次连接 | 一致 |
| `exec-local-cwd` | `codex exec`，不带 `-C` | 发的是 Mac 本地目录；`process/start` 和 fs 请求全部 `-32603 No such file or directory`；**rc 0** | 一致 |
| `exec-shared-path` | 两边建同一路径，Mac 上 `AGENTS.md` 写 LOCAL 标记，容器里写 REMOTE 标记；`codex exec -C` 该路径 | 可用；模型收到 REMOTE 标记；工作区内写成，工作区外 `Read-only file system` | 一致 |
| `tui-remote-cwd` | 交互模式 `-C /srv/p21/work` | 可用；命令 `cwd` 是 `/srv/p21/work`，身份 `runner`；工作区内写成，工作区外 `Read-only file system`；`apply_patch` 新增文件成功 | 一致 |
| `tui-no-cwd` | 交互模式不带 `-C` | 发的是 Mac 本地目录，命令与 patch 全部失败 | 一致 |
| `tui-shared-path` | 交互模式，同 `exec-shared-path` 的两份标记 | 模型收到 REMOTE 标记 | 一致 |
| `tui-surface` | 打开 `view_image`；`skills__list`、移动+删除的 patch、读工作区内和工作区外的图、列出嵌套工具 | 方法：`fs/getMetadata`、`fs/readFile`、`fs/writeFile`、`fs/remove`，都带 sandbox；**工作区外的图读到了**；嵌套工具 `apply_patch`、`clock__curr_time`、`exec_command`、`skills__list`、`skills__read`、`view_image`、`write_stdin` | 一致 |
| `tui-escalate-accept` | 模型申请命令提权、往工作区外加文件，人都批准 | 命令 `process/start` 带 `sandbox: null`；patch 的 sandbox 多一条 `{"type":"path"}` 写条目；**两个文件都写到了工作区外** | 一致 |
| `tui-escalate-decline` | 同上，人拒绝 | 本回合随即中止，没有发出执行请求，没有文件 | 一致 |
| `git-none` | 上层目录没有仓库 | 从根往上逐级查 `.git` 到 `/` | 一致 |
| `git-parent-forwarded` | `/srv/p21` 是 Git 仓库，照常转发 | 找到上层 `.git` 后，接着查 `/srv/p21/AGENTS.md`、`AGENTS.override.md`、`.agents/skills`（在工作区根以上） | 一致 |
| `git-parent-intercepted` | 同上，但根以上的 `.git` 查询由网桥回 `-32004` | 请求集合与 `git-none` 相同，只差上溯遍数 | 一致 |
| `git-root` | 工作区根本身是仓库 | 根下找到 `.git`，不再往上查 | 一致 |
| `policy-basic` | 网桥挂规则表原型，跑 `tui-remote-cwd` 的计划 | 除根以上 `.git` 查询外没有请求被拦，结果同 `tui-remote-cwd` | 一致 |
| `policy-surface` | 规则表原型，跑 `tui-surface` 的计划 | 只拦了工作区外读图（`-32600`），其余同 `tui-surface` | 一致 |
| `policy-escalate-accept` | 规则表原型，跑 `tui-escalate-accept` 的计划 | 提权命令、带放宽条目的 patch、以及 Codex 随后自动发的 `sandbox: null` 重试全部 `-32600`；工作区外零写出 | 一致 |
| `project-config-trusted` | 容器里的项目放 `.codex/config.toml`（改模型，外加一个会在 Mac 上 `touch` 标记文件的 MCP server），信任目录 | 这份配置没有被读取：无对应 fs 请求、模型没变、Mac 上无标记文件 | 一致 |

另外两条不在上表里的观察：

- 容器里**没装 bubblewrap** 时（开发脚本期间的一次运行），命令和带 sandbox 的文件方法都失败，命令没有执行，报 `bubblewrap is unavailable: no system bwrap was found on PATH and no bundled codex-resources/bwrap binary was found next to the Codex executable`；装了 bubblewrap 但保持容器默认 seccomp 时，`bwrap` 本身报 `No permissions to create new namespace`。这两种状态没有跑满 3 遍。
- 官方 musl 发行包握手返回 `executorVersion: "0.0.0"`、`providerId: "sha256:ee98eab596fe9f71b415600d19dd994055356ff01a7b7a019813868a53150124"`，而它的 `codex --version` 是 `codex-cli 0.154.0`。

## 没测到的

见 ccnm 那份记录的"没测到的"一节，这里不重复。
