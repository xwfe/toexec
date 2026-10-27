//! Windows 上起子进程时，有两件事绕不开系统 API：
//!
//! - [`stop_std_handles_being_inherited`]：本进程的标准输入、输出、错误句柄
//!   不再交给之后起的子进程；
//! - [`KeepAwake`]：持有它的这段时间里请求系统别睡。
//!
//! 两件都要 `unsafe`，而 ccnm 整个 workspace 禁止 `unsafe`，所以放在这里
//! （ccnm 设计文档 `docs/plan/windows-runtime.md` 的 D7，用户 2026-10-24
//! 定）。这个 crate 不碰执行模型——怎么起进程、怎么等、怎么杀，仍是各产品
//! 自己的事——所以不违反"进程与输出不共享"，理由见
//! `evidence/v2-k/duplication-audit.md` 那一节末尾。
//!
//! 在 macOS、Linux 上这个 crate 是空的：那里没有这两件事要做（Unix 的
//! `Command` 只把指定的三个描述符交给子进程，`fork` 之后其余的照 `CLOEXEC`
//! 关掉；防睡眠也不归执行层管）。产品按 `cfg(windows)` 引用它。

#[cfg(windows)]
mod awake;
#[cfg(windows)]
mod handles;
#[cfg(windows)]
mod sys;

#[cfg(windows)]
pub use awake::KeepAwake;
#[cfg(windows)]
pub use handles::stop_std_handles_being_inherited;
