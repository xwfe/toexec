//! gld 与 ccnm 共用的执行原语。
//!
//! 目前只有文本读取（`line`）。别的模块按已证明的需要加，不先建空框架。
//!
//! 这里只放**两个产品都真的在用**的东西。两边的 `read_file` 契约不一样
//! （非法 UTF-8 一个报错一个有损替换，一个一定读到文件尾一个撞预算就停），
//! 那些差异是各自的对外契约，不在这里统一；具体对照见仓库里的
//! `evidence/v2-k/duplication-audit.md`。

mod line;

pub use line::{LineLimits, Terminator, next_line};
