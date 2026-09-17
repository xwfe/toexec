//! gld 与 ccnm 共用的 SKILL.md 读取。
//!
//! 这个仓库（`toexec`）一个 crate 管一件事：这里只管「把一份 SKILL.md（或
//! `.claude/commands/*.md`）读明白」——拆出 frontmatter、读出里面的字段、把
//! `$ARGUMENTS` 这类占位符换掉、找出正文里要求执行的命令。
//!
//! **不在这里的**：去哪些目录找 skill、最多收多少个、目录怎么排版、经什么
//! 通道交给模型、注入的命令到底跑不跑。这些两个产品各不相同（ccnm 的目录
//! 要挤进 2048 个 UTF-16 码元，gld 是 50 条 × 250 字符），而且跑不跑命令是
//! 安全决定，不是解析细节。
//!
//! 三块纯机制：
//!
//! ```text
//! frontmatter::split / parse   拆出 frontmatter，读成键值
//! args::split / substitute     切参数，换 $ARGUMENTS、$0、$name、${CLAUDE_SKILL_DIR}
//! inject::find                 找出正文里的 !`命令` 和 ```! 代码块
//! ```
//!
//! 字段语义以 Claude Code 官方文档的 skills 一页和 Agent Skills 规范
//! （agentskills.io/specification）为准。

pub mod args;
pub mod frontmatter;
pub mod inject;

pub use frontmatter::{Frontmatter, ParseError, Value};
