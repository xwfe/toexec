//! gld 与 ccnm 共用的文本读取原语。
//!
//! 这个仓库（`toexec`）一个 crate 管一件事：这里只管读文本。原子写入与
//! 回滚是下一个 crate，不塞进来。
//!
//! 这里只放**两个产品都真的在用**的东西。两边的 `read_file` 契约不一样
//! （非法 UTF-8 一个报错一个有损替换，一个一定读到文件尾一个撞预算就停），
//! 那些差异是各自的对外契约，不在这里统一；具体对照见仓库里的
//! `evidence/v2-k/duplication-audit.md`。

mod line;

pub use line::{LineLimits, Terminator, next_line};
