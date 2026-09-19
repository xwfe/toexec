# X01：`toexec-fs::replace` 在 Windows 上到底是什么行为

回答的问题：**替换失败的时候，旧文件还在不在。** 以及顺带问清楚——Windows 上
`rename` 到一个已存在的文件到底会不会失败（老实现假设会，所以先删了一次）。

结论和契约写在 [`docs/usage.md`](../../docs/usage.md) 的"已知边界"和
`crates/toexec-fs/src/atomic.rs` 的 `replace` 注释里，这里只放怎么重跑和跑出了
什么。

## 怎么重跑

没有 Windows 机器就靠 CI：仓库的 `.github/workflows/ci.yml` 有一个
`windows` job，在 `windows-latest` 上跑 `cargo test --workspace`，stable 和
`Cargo.toml` 里 `rust-version` 声明的版本各一遍。推一个提交就会跑。

**日志要登录 GitHub 才看得到**，哪怕仓库是公开的。所以那个 job 失败时会把
panic 那几行塞进 annotation，annotation 匿名读得到：

```bash
curl -s "https://api.github.com/repos/xwfe/toexec/commits/<sha>/check-runs" |
  python3 -c "import json,sys;print([(c['name'],c['id']) for c in json.load(sys.stdin)['check_runs']])"
curl -s "https://api.github.com/repos/xwfe/toexec/check-runs/<id>/annotations"
```

手上有 Windows 机器的话直接：

```bash
cargo test -p toexec-fs
```

只读目标、被独占打开的目标这两条用例只在 Windows 上编译（`cfg(windows)` 或者
分支里的 Windows 断言），在 macOS / Linux 上跑不到。

## 跑出了什么

`windows-latest`（NTFS），rustc stable 与 `Cargo.toml` 声明的 MSRV 1.89 各一遍，
2026-09-19。测试名见 `crates/toexec-fs/src/atomic.rs`。

| 提交 | Windows job | 说明 |
| --- | --- | --- |
| `7135dc7` | 红 | 只读目标那条挂了，其余 9 条过。annotation 里只有 `exit code 101`，看不出是哪条 |
| `fa65c46` | 红 | 加了"失败摘要进 annotation"，这才读到 `Access is denied. (os error 5)` |
| `4b4d647` | [绿](https://github.com/xwfe/toexec/actions/runs/35443679601) | 只读那条按实测改成分平台断言；stable 和 1.89 都过 |

| 场景 | Windows | Unix（macOS 26.6 本机） |
| --- | --- | --- |
| 目标已存在，直接 `rename` 顶替 | **成功**——先删那一步本来就不需要 | 成功 |
| 临时文件不在（最小的失败复现） | 失败 `NotFound`，旧目标和内容一个字节没动 | 同左 |
| 目标是非空目录 | 失败，目录和里面的文件都在，临时文件也在 | 同左 |
| 目标带只读属性 | **失败 `Access is denied.`（os error 5）**，旧内容没动 | 成功（rename 看的是父目录权限） |
| 目标被别人独占打开（没带 `FILE_SHARE_DELETE`） | 失败，旧内容没动，临时文件也在 | 不适用（Unix 打开的是 inode，rename 照样成功） |

两件事值得单独记：

- **老实现会丢文件的路径是真的。** 它在 Windows 上先 `remove_file(target)` 再
  `rename`。上表第二行（临时文件不在）就是那条路径的最小复现：删成功、rename
  失败，旧文件没了。现在只有一次 `rename`，这一行的结果是"旧内容一个字节没动"。
- **只读目标替换不了，这不是这次改出来的。** std 的 `FileRenameInfoEx` 回退只带
  `REPLACE_IF_EXISTS | POSIX_SEMANTICS`，没带
  `FILE_RENAME_FLAG_IGNORE_READONLY_ATTRIBUTE`；老实现在这里也一样失败（Windows
  删只读文件同样是 `ACCESS_DENIED`）。两种失败都不动旧文件，所以不违反"失败保留
  旧目标"。要覆盖这种文件，调用方得自己先摘掉只读属性。

## 没验的

- **断电/崩溃**没有实测。`fsync` 内容能挡住"名字对、长度错"，但"rename 这件事"
  在断电后是否可见还得 fsync 父目录，这里没做（[`docs/usage.md`](../../docs/usage.md)
  的已知边界里记着）。
- **只测了 NTFS。** ReFS、网络盘（SMB）、FAT32 上 `FileRenameInfoEx` 支不支持没测；
  不支持时 std 退回 `MoveFileExW`，那条路上目标不能是目录。
- **两个产品在 Windows 上的补丁路径没测。** gld 有 Windows 发布构建，但它的 CI
  Windows job 只做 `cargo check`；ccnm 不支持 Windows。这里验的是共享库这一层。
