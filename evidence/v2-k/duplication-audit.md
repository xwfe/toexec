# V2-K 开工前的重复度盘点（2026-09-16）

对应 [v2 方案](../../docs/plan/implementation-plan-v2.md) 第 6.1 节和第 8 节的 V2-K 线。

盘点对象：gld `e577a04`（0.4.0）、ccnm `db53098`（0.7.0），都是当前工作区的 HEAD。
只读源码，没有改动任何一侧。

## 结论先行

1. **V2-K 写的"先提取有界文本读取"，按字面做是错的。**两边的读取契约不一样，
   而且各自都有对拍测试锁死。真正重合的只有"别把多字节字符切成两半"那几十行。
2. **重合度最高、收益最明确的是原子写入与回滚**，不是读取。两边都实现了「临时文件
   → rename → 失败回滚」，但 ccnm 那份明显更硬（fsync、保留权限、备份落盘、回滚
   失败留 journal），gld 那份把整个原文件读进内存当备份、没有 fsync、不保留权限。
3. **进程/输出这条线短期不要碰**：gld 是 tokio async + 要支持 Windows，ccnm 是同步
   + 只跑 Unix。共享的代价最大，收益最不确定。
4. 按现在的真实重复量，第一刀能消掉的重复大约在**一两百行**量级。要不要为此引入
   一个跨仓依赖（发版、pin 版本、双仓 CI 联动），是需要先算清楚的账，见最后一节。

## 逐块对照

### 读取：契约不同，不是同一个东西

ccnm `crates/ccnm-core/src/mcp/read.rs`（1275 行）对
gld `crates/core/src/tools/file.rs` 的 `read_text_selection`（约 100 行）。

| | ccnm | gld |
| --- | --- | --- |
| 给调用方的粒度 | 一行一行（`Ending` 区分 CRLF / LF / 无终结符） | 一整段 `content`，换行符原样留着 |
| 非法 UTF-8 | 有损替换成 U+FFFD，继续读，加一条 note | **直接报 `UNSUPPORTED_ENCODING`** |
| 是否读到文件尾 | 不一定：撞上 `max_lines` / `max_bytes` 就停，`total_lines` 可能是 `None` | **一定读完**——`total_lines` 和「整份文件是否合法 UTF-8」都要看到最后一个字节 |
| 超长单行 | 有界读，扫描超过 64 MiB 报错让人改用 `search_text` | 截断后继续，后面只数换行符 |
| BOM | 剥掉并标记 | 不处理 |
| CRLF | 识别、剥离、报 `line_ending` | 留在 content 里 |
| 截断信息 | `truncated_by` 枚举 + `next_start_line` + `partial_line` | 一个 bool |
| 行号类型 | `u32` | `usize` |

这些不是实现细节的出入，是**对外契约**：ccnm 那套写进了 `ccnm.workspace-mcp/1`
（已冻结），gld 那套有 `assert_same_as_reference` 拿旧算法逐字节对拍。v2 第 6.2 节
自己也写了「读取与搜索：保留编码、BOM、范围、总行数、ignore、排序、截断约定」。
硬抽成一个函数，只会得到一个全是开关的四不像。

**真正重合的是下面那一层**，两边都得干、都各写了一遍：

- 把切口退回字符边界，别切出半个字符：ccnm `trim_cut`（10 行），gld 读完之后
  `str::from_utf8(&kept)` 那几行。
- 跨读取块的 UTF-8 增量校验：gld `utf8_chunk_ok`（20 行，块尾留 tail）。ccnm 没有
  等价物，因为它不校验整份文件。
- 有界行切分：ccnm `next_line`（45 行，带 `keep` 上限和 `scanned` 计数）；gld 是
  `split_inclusive(b'\n')` 加 room 判断。同一件事，边界条件不同。

### 编辑：模型不同，不能共享；提交机制能

| | ccnm `mcp/patch.rs`（3428 行） | gld `tools/patch.rs`（793 行） |
| --- | --- | --- |
| 输入 | 结构化 edits（old/new 字符串），**顺序依赖**（a→b、b→c） | Unified Diff / codex patch 文本，解析成 hunk |
| 定位 | 先整体按位置匹配，失败再逐条按序匹配，带 near-miss 诊断 | `find_hunk_position`，按 header 行号找 |
| 版本检查 | 有（`check_version`，stale 就拒） | 无 |

v2 第 6.2 节要求两边各自保留，所以编辑算法本来就不在共享范围。

**提交那一段是真重复**，而且质量差得明显：

| | ccnm | gld |
| --- | --- | --- |
| 备份 | 落盘成临时文件（`Staged.backup`） | **整个原文件读进内存** `HashMap<PathBuf, Option<Vec<u8>>>` |
| 落盘保证 | `sync_all()` 之后才 rename | **没有 fsync** |
| 权限 | `set_permissions` 保留原权限（补丁不会让脚本丢掉可执行位） | 不保留 |
| 回滚失败 | 写 journal，下一次 patch 会发现工作区不一致 | `let _ =` 尽力而为，失败无声 |
| 中途失败 | 已提交的逐个 rollback，未提交的 discard | 同样是先恢复备份再报错 |
| Windows | 不支持 | `replace_file` 先 remove 再 rename |

抽这一块，gld 能拿到 fsync、权限保留和不吃内存的备份；ccnm 能拿到 Windows 的
替换路径（暂时用不上，但以后要用）。这是**收益最实在的一块**。

代价也最大：它是两个产品最危险的代码路径，第一刀切在这里，一旦有回归就是丢数据。

### 进程与输出：短期不要碰

| | ccnm `mcp/exec.rs` + `mcp/output.rs` | gld `tools/exec.rs` |
| --- | --- | --- |
| 执行模型 | 同步（`mcp` 之外全仓不用 async） | **tokio async**，带 SessionStore、超时监控、会话驱逐 |
| 平台 | Unix（macOS / Debian 13 验过） | **要支持 Windows**（隐藏窗口 flags、bat 命令行拼装、PATH 解析） |
| 输出 | 落盘 + `read_output` 分页（`output_ref`） | 会话内保留 |

要共享就得先统一执行模型，那是把 ccnm 拖进 async，或者把 gld 的会话机制拆开。
代价远大于当前能省的重复。

### 搜索：匹配逻辑不重复，读行重复

ccnm 的 `search_text` 调外部 `ripgrep`；gld 自己实现 `Matcher`。匹配这一层没有可
共享的东西。

但 gld 的 `search_file_streaming` 用 `BufRead::lines()` 逐行读，**一行有多长就往
内存里放多长**——和 ccnm P14 修掉的是同一类问题。严重程度差很多，别混为一谈：

- ccnm 的 `read_file` 当时**没有文件大小上限**，一个 2 GB 的单行文件能先分配
  2 GB，Runtime 可能先被 OOM 杀掉。
- gld 的 `search_text` **有** `max_file_bytes`（默认 2 MiB，最大 64 MiB），超过就
  整个文件跳过。所以最坏是 64 MiB 进内存，不是 2 GB。

**真正的放大器在别处**：`context_lines`（最大 20）会把行克隆进 `recent` 队列，
每个待定匹配又各持有一份 before 和 after 的克隆。一个 64 MiB、每行约 1.6 MiB 的
文件，克隆总量能到 1 GB 量级。这是 gld 自己的缺陷，跟共享库无关，**不在这一刀里
夹带修**——要修就单独立项。

## 三个仓库的工程现状（盘点当时）

下面这张表是**开工前**的状态，留着是为了说明后面那两个决定是怎么来的。同一天这两行
都变了：这个仓库推成了公开远端 `github.com/xwfe/toexec`，gld 的 `rust-version` 提到了
1.89。当时它还叫 `workspace-kernel`，crate 还叫 `wk-text`。

| | gld | ccnm | 本仓库 |
| --- | --- | --- | --- |
| Rust 工程 | 有（3 个 crate） | 有（2 个 crate） | **没有，只有文档和 evidence** |
| `rust-version` | 1.85 | 1.89 | — |
| edition | 2021 | 2024 | — |
| resolver | 2 | 3 | — |
| git remote | `xwfe/gld` | `xwfe/ccnm` | **没有 remote，纯本地仓库** |
| CI | GitHub Actions，单仓 checkout | GitHub Actions，单仓 checkout | 无 |

两条直接后果：

- 统一 `rust-version` 的时点到了（用户 2026-09-15 定的下限 1.89）：gld 要从 1.85 提到
  1.89。edition 不必统一，共享 crate 自己声明就行。
- **`path` 依赖在 CI 上直接死**：GitHub runner 只 checkout 当前仓库，旁边那个共享库
  目录不存在。要让两边 CI 继续绿，共享 crate 必须能从网络取到——也就是这个仓库
  得有 remote。这是本轮第一个需要用户决定的事，后来的结论是推成公开仓库、按 tag
  引用。

## 建议的切法

1. **先切一块小的、纯函数、无 I/O 的**：UTF-8 增量边界 + 有界行切分原语。它是
   「有界文本读取」里两边真正共有的内核，可以穷举测试，出错也只影响文本切分，
   不碰写入。这一刀的主要目的是**把基础设施打通**——crate 放哪、怎么被依赖、
   两边 CI 怎么绿、rust-version 怎么统一。
2. **再切原子写入与回滚**，收益最大那块，等第一刀的联动机制被证明可用之后再动。
3. 进程/输出留到后面，或者干脆不动，等有新证据。

每一刀都要求：两边现有测试一个不改断言地通过，语义逐字节不变。

## 实际切完是什么样（2026-09-16 补记）

两刀当天都做完了。跟上面的预判对一遍：

| 预判 | 实际 |
| --- | --- |
| 第一刀消掉的重复「一两百行」 | ccnm 少了 72 行，gld 的搜索换成同一个原语。**消重本身不是收益**，收益是 gld 的搜索路径跟着拿到了有界读行 |
| 第二刀收益最大 | 对。gld 原来暂存时既没有 fsync、也不带原文件权限——**打完补丁的脚本从 0755 变成 644**，下次 `./run.sh` 直接 Permission denied。新测试验过红灯基线 |
| 「两边现有测试一个不改断言地通过」 | 做到了。ccnm 719、gld 470（加了 4 条新测试）、共享库 19，全部 0 失败 |
| 基础设施打通 | 打通了，但绕了一圈：先用本地 `path` 依赖，两边 CI 当场构建不了；改成按 tag 的 git 依赖才行。验证办法是把产品 clone 到一个旁边没有这个仓库的目录构建——那正是 runner 的处境 |

**一条预判里没有的经验，适用于后面每一刀**：抽公共机制时，**错误的分类和措辞是
产品的对外契约，不是可以顺手统一的实现细节**。`toexec-fs` 0.1.0 把四步失败合成
一个 `io::Error`，接进 ccnm 才发现那会把刷盘失败从 `internal` 悄悄变成
`invalid_args`——没有任何测试会因此失败，但两个错误码在 MCP 里的含义不同，把机器
的问题报成调用方的参数问题，会把人支到错误的方向。0.2.0 改成报出是哪一步失败、
由调用方自己归类。

**维持不碰的**：进程/输出。理由没有变化——gld 是 tokio async 且要支持 Windows，
ccnm 是同步且只跑 Unix，要共享就得先统一执行模型，代价远大于能省的重复。

**还欠着一个决定**：父目录 fsync。`rename` 本身是原子的，但「rename 这件事」要在
断电后仍然可见，还得 fsync 目标的父目录，两个产品原来都没做，这两刀也没做。现在
的故障模式是**丢失而不是损坏**（内容已经 fsync 过，所以断电后要么是全新的、要么是
全旧的，不会半新半旧），这个模式本身可以接受。补它的代价是每次替换多一次 fsync，
而且**效果没法在单元测试里验证**——真要证明得能模拟掉电。所以它是一个单独的、
需要权衡的决定，不是这两刀的遗留 bug。
