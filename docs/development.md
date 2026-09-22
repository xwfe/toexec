# 开发与发版

改这个仓库的代码、发新 tag、和 gld / ccnm 本地联调时看这一篇。

## 改完先跑这四条

```bash
cargo fmt --all --check
cargo clippy --workspace --all-targets --locked -- -D warnings
cargo test --workspace --locked
cargo +1.89 check --workspace --all-targets --locked
```

CI（`.github/workflows/ci.yml`）跑的就是这四条。最后一条用根 `Cargo.toml` 里 `rust-version` 声明的版本再编一遍：只跑 stable 的话，用了比 1.89 更新的标准库 API 照样全绿，要等 gld 或 ccnm 的人拿 1.89 去编才炸出来。本机没装 1.89 就先 `rustup toolchain install 1.89 --profile minimal`。

三个仓库（gld / ccnm / toexec）的 `rust-version` 统一是 1.89。下限来自 ccnm：它的 `apply_patch` 用到 `File::try_lock`。要升就三个仓库一起升。

## 往这里抽东西的规矩

1. **两个产品都真的在用，才进来。** 只有一边用的代码留在那一边。开工前先对照 [重复度盘点](../evidence/v2-k/duplication-audit.md)：很多看起来重复的东西，对外契约其实不同。
2. **一个 crate 管一件事**，名字是 `toexec-` 加职责。读文本的不塞进写文件的。
3. **不加依赖。** 哪天一个 crate 需要 tokio、HTTP 客户端或某个产品的错误类型，说明抽错了层。**唯一的例外是 `toexec-mcp`**（2026-09-22，v4 第 3 步）：它要 serde_json 和 toml，因为 JSON-RPC 消息和 `~/.claude.json`、`~/.codex/config.toml` 两种格式就是它要处理的东西本身，手写解析器只会更糟；两个产品本来就链着这两个库、同一个版本。它仍然不碰 tokio 和 HTTP——所以 HTTP 通道、怎么起进程怎么杀，留在产品里。再加任何依赖都要单独说明理由。
4. **错误的分类和措辞是产品的对外契约，不是可以顺手统一的实现细节。** 共享库只报事实（哪一步失败、原始错误是什么），归成哪个错误码由产品决定。`toexec-fs` 0.1.0 就栽过这个：它把四步失败合成一个 `io::Error`，接进 ccnm 时把"刷盘失败"从内部错误悄悄变成了参数错误，0.2.0 才改成分步报。
5. **接进产品时，产品那边原有的测试断言一条都不该改。** 要改才能过，说明行为变了；那是一次行为变更，得单独说明，不能伪装成等价重构。

## 发新版本

每个 crate **各发各的版**，tag 带 crate 名：`toexec-text-v0.1.0`、`toexec-fs-v0.2.0`。根 `Cargo.toml` 故意没有统一的 `version`——共享一个版本号会让没改过的 crate 跟着别人涨，tag 和代码就对不上了。

1. 改 `crates/<crate>/Cargo.toml` 里的 `version`，提交。
2. 跑上面那四条。
3. 打带说明的 tag 并推送：

   ```bash
   git tag -a toexec-fs-v0.3.0 -m "toexec-fs 0.3.0：<一句话说这一版变了什么>"
   git push origin main toexec-fs-v0.3.0
   ```

4. 去产品仓库把 `Cargo.toml` 里对应那一行的 `tag` 改成新的，跑产品自己的全量测试。
5. 更新 [README](../README.md) 的 crate 表里的"当前 tag"。

**一个 crate 依赖同仓的另一个时用 path**（现在只有 `toexec-mcp` → `toexec-text`）。后果是产品的 `Cargo.lock` 里会有两份 `toexec-text`：一份是产品自己钉的 `toexec-text-v…` tag，一份是 `toexec-mcp-v…` 那个 tag 下的。两份是同一套代码时只是多编一次；`toexec-text` 发了新版，产品升自己那一行不会顺带改 `toexec-mcp` 里那份，要等 `toexec-mcp` 也发一版。它们之间不交换类型，所以两份不会互相打架。

**已经推送的 tag 不要移动或重打。** 产品的 `Cargo.lock` 里记着 tag 指向的提交，tag 挪了之后，别人 `cargo update` 会悄悄拿到不同的代码。发错了就再发一个新版本号。

仓库里还有一个 `wk-text-v0.1.0`：这个仓库 2026-09-16 之前叫 workspace-kernel，那是改名前的 tag（当时 crate 叫 `wk-text`，实现文件 `line.rs` 和 `toexec-text-v0.1.0` 一字不差）。现在没有人引用它，留着只是因为已推送的 tag 不删。

## 和产品仓库本地联调

产品平时按 tag 引用（为什么不跟 `main`：共享库改了不会在某次 `cargo update` 之后突然改变产品行为，升级是显式的一步）。要同时改两边时，在产品的 `Cargo.toml` 里临时换成 path 依赖：

```toml
toexec-fs = { path = "../toexec/crates/toexec-fs" }
```

**别提交这一行。** GitHub 的 runner 只 checkout 一个仓库，旁边没有 `../toexec`，cargo 在解析 manifest 阶段就失败，报 `failed to read .../toexec/crates/toexec-fs/Cargo.toml`，两个产品的 CI 会一起红。

引用用 `https://` 而不是 SSH：这是公开仓库，匿名就能读，本地和 CI 都不用配凭据；SSH 别名只存在于你自己机器的 `~/.ssh/config` 里，写进 `Cargo.toml` 在 runner 上解析不了。
