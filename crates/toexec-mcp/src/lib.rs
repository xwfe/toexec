//! gld 与 ccnm 共用的"转发已经装好的 MCP server"那几块机制。
//!
//! 两个产品都把别人装好的 MCP server 转给自己的会话：gld 转给连上它服务的
//! Web AI（本机装的），ccnm 转给受管会话（项目那台机器上装的，外加项目自己的
//! `.mcp.json`）。读哪些配置、怎么跟 server 握手、连接活多久、结果太大怎么办，
//! 两边是同一件事，放在这里：
//!
//! ```text
//! installed::read       读 .mcp.json、~/.claude.json、~/.codex/config.toml 里装了哪些
//! client::Client        握手（接 2024-11-05 到 2025-11-25）、翻页列工具、调工具
//! child::ChildTransport 本机子进程当通道：有界读行、读能超时、stderr 留尾巴
//! pool::Pool            用到才开、按 server + 调用方分、闲了收、后台扫
//! shape                 结果去重复的 structuredContent、大文字整段交给产品、清单放不下先去参数表
//! kept                  一次交不完的长结果留在内存里，按段接着读（0.2.0）
//! sse                   streamable HTTP 回复里的 SSE 怎么拆（0.2.0；发请求仍是产品的事）
//! scripted              内存里的假 server，给产品写测试用
//! ```
//!
//! **不在这里的**：进程怎么起（环境变量、沙箱、进程组）和怎么杀、HTTP 请求
//! 怎么发（gld 用 reqwest、ccnm 用系统的 curl）、模型看到的工具叫什么、错误写成
//! 什么样、大结果留在哪（`kept` 是内存里的一种，ccnm 转项目那台机器上的 server
//! 时用的是自己的留存目录）。这些两个产品答案不同，而且多半是安全决定。
//!
//! 这是 toexec 里第一个有依赖的 crate：serde_json 和 toml。JSON-RPC 消息和两种
//! 配置文件格式就是它要处理的东西本身，手写解析器只会更糟；它仍然不碰 tokio、
//! 不碰任何产品的类型。

pub mod child;
pub mod client;
pub mod installed;
pub mod kept;
pub mod pool;
pub mod scripted;
pub mod shape;
pub mod sse;
pub mod transport;

pub use client::{Client, Error};
pub use installed::{Installed, Server};
pub use pool::{Open, Pool};
pub use transport::{Recv, Transport};
