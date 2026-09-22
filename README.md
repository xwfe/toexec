# toexec

[gld](https://github.com/xwfe/gld) 和 [ccnm](https://github.com/xwfe/ccnm) 共用的 Rust 小库。两个产品都在给 AI 编程 Agent 提供"读文件、改文件"的工具，其中真正写重复了的那几段纯机制抽到这里，两边按 tag 链接同一份代码。

*Small shared Rust crates for AI coding-agent execution layers: the pure mechanisms that both gld and ccnm need — bounded line reading and durable atomic file replacement. Zero dependencies, pinned by tag. Docs are in Chinese.*

## 什么时候用得上

你在写一个让 AI 读写项目文件的程序（MCP 工具、Agent 的执行端之类），并且撞上了下面任意一件事：

- **读文件时被一行撑爆内存。** 标准库的 `read_line` / `lines()` 会先把整行读进内存。一个 2 GB 的单行文件（压缩过的 JS、一行导出的 JSON）会先分配 2 GB，你才有机会说"太长了"——实际结果是进程先被系统杀掉。
- **写文件写到一半断电或崩溃，留下半截文件。** 或者补丁打完，脚本的可执行位丢了。
- **要读项目里的 skills（`SKILL.md`）。** 它的开头是一段 YAML，而作者的写法五花八门：描述折成多行的、`allowed-tools` 写成列表的、带三层 `hooks` 的。逐行找 `description:` 前缀的读法，遇到多行描述读出来的就是一个 `>`。

| crate | 管什么 | 当前 tag |
| --- | --- | --- |
| `toexec-text` | 有界行读取 `next_line`：事先说好一行最多留多少字节，超出的只数不存 | `toexec-text-v0.1.0` |
| `toexec-fs` | 原子文件替换 `write_durable` + `replace`：内容先落盘，再一次 rename 顶替 | `toexec-fs-v0.2.1` |
| `toexec-skill` | 读 `SKILL.md`：拆 frontmatter 并按 Claude Code 的读法读成键值、按它的规则替换 `$ARGUMENTS` / `$0` / `$name`、找出 `` !`命令` `` 注入（只找不跑）；列出和读取 skill 目录里的其他文件（只在它自己的目录里、不读点文件、只收 UTF-8） | `toexec-skill-v0.3.0` |

三个 crate 都**没有任何依赖**，只用标准库。

## 快速使用

在你的 `Cargo.toml` 里按 tag 引用（要 Rust 1.89 或更新）：

```toml
[dependencies]
toexec-text = { git = "https://github.com/xwfe/toexec.git", tag = "toexec-text-v0.1.0" }
toexec-fs   = { git = "https://github.com/xwfe/toexec.git", tag = "toexec-fs-v0.2.1" }
toexec-skill = { git = "https://github.com/xwfe/toexec.git", tag = "toexec-skill-v0.3.0" }
```

读文件，每行最多留 4096 字节：

```rust
use std::{fs::File, io::BufReader};
use toexec_text::{LineLimits, next_line};

let mut reader = BufReader::new(File::open("big.log")?);
let limits = LineLimits { keep: 4096, scan_limit: None };
let (mut raw, mut scanned) = (Vec::new(), 0u64);
loop {
    raw.clear();
    let Some(_ending) = next_line(&mut reader, &mut raw, limits, &mut scanned)? else {
        break; // 文件读完
    };
    println!("{}", String::from_utf8_lossy(&raw));
}
```

写文件，任何时刻目标要么是旧内容、要么是新内容：

```rust
use toexec_fs::{replace, write_durable};

let temp = target.with_extension("tmp");          // 临时文件建在目标同一个目录里
let mode = std::fs::metadata(&target).ok().map(|m| m.permissions());
write_durable(&temp, new_bytes, mode.as_ref())?;  // 内容落盘之后才返回
replace(&temp, &target)?;                         // 一次 rename 顶替；失败了旧内容还在
```

`.tmp` 这个固定名字只在"只有你自己写得了这个目录"时够用；目录别人也能写的话得用随机名字加 `create_new`，否则名字会被人提前占住。

完整可运行的例子、每个参数的含义、已知边界（替换失败时旧文件一定还在、没有 fsync 父目录、临时文件的命名和清理归调用方）见 [使用说明](docs/usage.md)。

## 这里不放什么

**只放两个产品都真的在用的纯机制。** 两边的 `read_file` 对外契约不一样（非法 UTF-8 一个报错、一个有损替换），回滚编排也不一样（ccnm 写 journal，gld 一刀切恢复）——那些是各自的产品契约，**不会统一**到这里。逐项对照见 [重复度盘点](evidence/v2-k/duplication-audit.md)。

## 文档

| 我想…… | 看这里 |
| --- | --- |
| 查看三仓重构方向评审、关键风险、实施依赖与验收计划 | [跨项目重构评审](docs/plan/2026-09-19-cross-project-refactor-review.md) |
| 照着例子把三个 crate 用起来，弄清边界和常见坑 | [docs/usage.md](docs/usage.md) |
| 改这里的代码、发新 tag、和产品仓库本地联调 | [docs/development.md](docs/development.md) |
| 知道 `evidence/` 里那些实测脚本和结果是干什么的 | [docs/evidence.md](docs/evidence.md) |
| 看 gld / ccnm / toexec 三个仓库的跨仓方案和当前覆盖表 | [docs/plan/implementation-plan-v2.md](docs/plan/implementation-plan-v2.md) |
| 看下一步：gld / ccnm 的工具面怎么对齐原生 CLI 的能力（含项目 skills） | [docs/plan/implementation-plan-v3-native-parity.md](docs/plan/implementation-plan-v3-native-parity.md) |

## 许可证

MIT，见 [LICENSE](LICENSE)。
