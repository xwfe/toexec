//! 一条通往 MCP server 的通道：一次发一条 JSON-RPC 消息，一次收一条。
//!
//! 协议逻辑（握手、翻页、谁回的是哪条）在 [`crate::client`]，通道只搬字节。
//! 本机子进程那种在 [`crate::child`]；HTTP 的由产品自己实现（要 HTTP 客户端和
//! 异步运行时，这里不背这两个依赖）。

use std::time::Duration;

use crate::client::Error;

/// 一条消息最多收这么大。实测 deepwiki 的 `read_wiki_contents` 一条回复
/// 839 KB；给到 32 MiB，正常的回复远到不了，吐个没完的 server 也撑不爆
/// 内存。超了这条回复作废，调用方拿到的是"太大"，不是半截 JSON。
pub const MAX_MESSAGE_BYTES: usize = 32 * 1024 * 1024;

/// 收一条的结果。
pub enum Recv {
    Line(String),
    /// 这条比 [`MAX_MESSAGE_BYTES`] 大，读过去了，没留。
    TooLong(u64),
    Timeout,
    /// 对面关了。`said` 是它在 stderr 上留下的最后一段话。
    Closed {
        said: String,
    },
}

pub trait Transport: Send {
    /// 发一条消息。HTTP 在这一步就把回复收回来了，所以要知道等多久。
    fn send(&mut self, line: &str, timeout: Duration) -> Result<(), Error>;
    /// 收下一条，最多等 `timeout`。
    fn recv(&mut self, timeout: Duration) -> Recv;
    /// 收工：之后这条通道就废了。调几次都行。
    fn close(&mut self);
}
