//! 跟一个 MCP server 说话：握手、列工具、调工具。
//!
//! 只做转发要用的这几件：initialize、tools/list（带翻页）、tools/call。server
//! 反过来问的，只答 `ping`，别的回"没有这个方法"——这里没声明 sampling、roots、
//! elicitation，按协议 server 本来就不该问。
//!
//! 对面是谁都可能写的 server：实测（toexec `evidence/v4-mcp/machine-mcp/`）有说
//! 2025-06-18 的，也有还停在 2024-11-05 的（`@modelcontextprotocol/server-github`、
//! `server-puppeteer`），两个都得接。stdout 上混进一行不是 JSON 的日志，官方
//! TypeScript SDK 是报个错接着读，这里也跳过去接着读。

use std::time::{Duration, Instant};

use serde_json::{Value, json};

use crate::transport::{MAX_MESSAGE_BYTES, Recv, Transport};

/// 握手时报的版本。
pub const CLIENT_PROTOCOL: &str = "2025-06-18";

/// server 回哪个都接。这几版之间 tools/list 和 tools/call 的形状没变过，
/// 差别在 gld 不用的部分（批量请求、elicitation……）。
pub const KNOWN_PROTOCOLS: [&str; 4] = ["2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05"];

/// tools/list 最多翻这么多页。一页通常就是全部；翻不完的 server 是坏了。
const MAX_PAGES: usize = 20;

/// 跟 server 说话时出的岔子。每种一个变体，因为给模型的指示不一样：
/// 起不来是操作员的事，超时了可以再试，调用断在半路则不知道对面做了没有。
#[derive(Debug)]
pub enum Error {
    /// 进程起不来、地址连不上、配置不能用。
    Start(String),
    Timeout {
        during: String,
        waited: Duration,
    },
    /// 对面关了。`said` 是 stdio server 在 stderr 上留下的最后一段话。
    Closed {
        during: String,
        said: String,
    },
    /// HTTP server 回了一个错误状态码。
    Http {
        status: u16,
        said: String,
    },
    /// HTTP server 要登录（401）：多半是 OAuth，gld 替不了。
    NeedsLogin {
        status: u16,
    },
    /// 回来的东西不能用：不是 JSON-RPC、版本不认识、太大。
    Protocol(String),
    /// server 明确回了一个 JSON-RPC error（参数不对之类）。
    Refused {
        code: i64,
        message: String,
    },
    /// 同一条连接上另一个调用还没完，等过了头。这次调用没发出去。
    Busy {
        waited: Duration,
    },
}

impl Error {
    /// 这条连接还能不能接着用。不能用的，连接池会把它丢掉，下次重开。
    pub fn breaks_connection(&self) -> bool {
        !matches!(self, Error::Refused { .. } | Error::Busy { .. })
    }
}

impl std::fmt::Display for Error {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Error::Start(message) => write!(f, "{message}"),
            Error::Timeout { during, waited } => write!(
                f,
                "the server did not answer {during} within {:.0}s",
                waited.as_secs_f64()
            ),
            Error::Closed { during, said } if said.is_empty() => {
                write!(f, "the server went away during {during}")
            }
            Error::Closed { during, said } => {
                write!(f, "the server went away during {during}; it said: {said}")
            }
            Error::Http { status, said } if said.is_empty() => {
                write!(f, "the server answered HTTP {status}")
            }
            Error::Http { status, said } => write!(f, "the server answered HTTP {status}: {said}"),
            Error::NeedsLogin { status } => write!(
                f,
                "the server answered HTTP {status}: it wants a login (OAuth) this relay cannot perform, or its key in the config is wrong"
            ),
            Error::Protocol(message) => write!(f, "{message}"),
            Error::Refused { code, message } => write!(f, "the server refused ({code}): {message}"),
            Error::Busy { waited } => write!(
                f,
                "another call to this server was still running after {:.0}s, so this one was not sent",
                waited.as_secs_f64()
            ),
        }
    }
}

impl std::error::Error for Error {}

/// 一条握过手的连接。调用串行（`&mut self`）。
pub struct Client {
    transport: Box<dyn Transport>,
    next_id: u64,
    pub protocol: String,
    pub server_name: Option<String>,
    pub server_version: Option<String>,
    /// server 在握手里给模型的说明（Context7、DeepWiki 都有）。
    pub instructions: Option<String>,
}

impl Client {
    /// 握手。`me` 是报给 server 的客户端名和版本（会进它的日志）；`timeout`
    /// 包括进程冷启动——`npx -y` 第一次要下载包。
    pub fn connect(
        transport: Box<dyn Transport>,
        me: (&str, &str),
        timeout: Duration,
    ) -> Result<Client, Error> {
        let mut client = Client {
            transport,
            next_id: 1,
            protocol: String::new(),
            server_name: None,
            server_version: None,
            instructions: None,
        };
        let result = client.request(
            "initialize",
            json!({
                "protocolVersion": CLIENT_PROTOCOL,
                "capabilities": {},
                "clientInfo": { "name": me.0, "version": me.1 }
            }),
            timeout,
        )?;
        let version = result
            .get("protocolVersion")
            .and_then(Value::as_str)
            .unwrap_or_default();
        if !KNOWN_PROTOCOLS.contains(&version) {
            return Err(Error::Protocol(format!(
                "the server speaks MCP \"{version}\", which this client does not know (it knows {})",
                KNOWN_PROTOCOLS.join(", ")
            )));
        }
        client.protocol = version.to_string();
        let info = result.get("serverInfo");
        let text = |key: &str| {
            info.and_then(|info| info.get(key))
                .and_then(Value::as_str)
                .map(str::to_string)
        };
        client.server_name = text("name");
        client.server_version = text("version");
        client.instructions = result
            .get("instructions")
            .and_then(Value::as_str)
            .filter(|text| !text.trim().is_empty())
            .map(str::to_string);
        client.notify("notifications/initialized", json!({}), timeout)?;
        Ok(client)
    }

    /// 全部工具，翻完所有页。
    pub fn list_tools(&mut self, timeout: Duration) -> Result<Vec<Value>, Error> {
        let mut tools = Vec::new();
        let mut cursor: Option<String> = None;
        for _ in 0..MAX_PAGES {
            let params = match &cursor {
                Some(cursor) => json!({ "cursor": cursor }),
                None => json!({}),
            };
            let result = self.request("tools/list", params, timeout)?;
            match result.get("tools") {
                Some(Value::Array(page)) => tools.extend(page.iter().cloned()),
                _ => return Err(Error::Protocol("tools/list came back without tools".into())),
            }
            cursor = result
                .get("nextCursor")
                .and_then(Value::as_str)
                .filter(|next| !next.is_empty())
                .map(str::to_string);
            if cursor.is_none() {
                return Ok(tools);
            }
        }
        Err(Error::Protocol(format!(
            "tools/list still had more after {MAX_PAGES} pages"
        )))
    }

    /// 调一个工具，**原样**返回 server 的 result（含 `isError: true` 的——那是
    /// 工具说"没办成"，不是协议错误）。
    pub fn call_tool(
        &mut self,
        name: &str,
        arguments: Value,
        timeout: Duration,
    ) -> Result<Value, Error> {
        self.request(
            "tools/call",
            json!({ "name": name, "arguments": arguments }),
            timeout,
        )
    }

    pub fn close(&mut self) {
        self.transport.close();
    }

    fn request(&mut self, method: &str, params: Value, timeout: Duration) -> Result<Value, Error> {
        let id = self.next_id;
        self.next_id += 1;
        let deadline = Instant::now() + timeout;
        let line = json!({ "jsonrpc": "2.0", "id": id, "method": method, "params": params });
        self.transport
            .send(&line.to_string(), timeout)
            .map_err(|error| during(error, method))?;
        loop {
            let left = deadline.saturating_duration_since(Instant::now());
            let line = match self.transport.recv(left) {
                Recv::Line(line) => line,
                Recv::TooLong(bytes) => {
                    return Err(Error::Protocol(format!(
                        "the server sent a {bytes}-byte message during {method}, over gld's limit of {} bytes",
                        MAX_MESSAGE_BYTES
                    )));
                }
                Recv::Timeout => {
                    return Err(Error::Timeout {
                        during: method.into(),
                        waited: timeout,
                    });
                }
                Recv::Closed { said } => {
                    return Err(Error::Closed {
                        during: method.into(),
                        said,
                    });
                }
            };
            // 不是 JSON 的行是 server 往 stdout 打的日志，跳过。
            let Ok(message) = serde_json::from_str::<Value>(&line) else {
                continue;
            };
            let is_ours = message.get("id").and_then(Value::as_u64) == Some(id)
                && message.get("method").is_none();
            if !is_ours {
                if let (Some(asked), Some(request)) = (message.get("id"), message.get("method")) {
                    self.answer(asked.clone(), request.as_str().unwrap_or_default(), left)?;
                }
                continue;
            }
            if let Some(error) = message.get("error") {
                return Err(Error::Refused {
                    code: error.get("code").and_then(Value::as_i64).unwrap_or(0),
                    message: error
                        .get("message")
                        .and_then(Value::as_str)
                        .unwrap_or("no message")
                        .to_string(),
                });
            }
            return message
                .get("result")
                .cloned()
                .ok_or_else(|| Error::Protocol(format!("{method} came back with no result")));
        }
    }

    fn notify(&mut self, method: &str, params: Value, timeout: Duration) -> Result<(), Error> {
        let line = json!({ "jsonrpc": "2.0", "method": method, "params": params });
        self.transport
            .send(&line.to_string(), timeout)
            .map_err(|error| during(error, method))
    }

    /// server 反过来问的：`ping` 答空结果，别的答"没有这个方法"。
    fn answer(&mut self, id: Value, method: &str, timeout: Duration) -> Result<(), Error> {
        let reply = if method == "ping" {
            json!({ "jsonrpc": "2.0", "id": id, "result": {} })
        } else {
            json!({
                "jsonrpc": "2.0",
                "id": id,
                "error": { "code": -32601, "message": format!("not implemented by this client: {method}") }
            })
        };
        self.transport
            .send(&reply.to_string(), timeout)
            .map_err(|error| during(error, method))
    }
}

impl Drop for Client {
    fn drop(&mut self) {
        self.transport.close();
    }
}

/// 传输层不知道当时在干什么，这里补上。
fn during(error: Error, method: &str) -> Error {
    match error {
        Error::Closed { said, .. } => Error::Closed {
            during: method.into(),
            said,
        },
        Error::Timeout { waited, .. } => Error::Timeout {
            during: method.into(),
            waited,
        },
        other => other,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::scripted::{Scripted, plain_server, reply};

    const QUICK: Duration = Duration::from_secs(1);
    pub(crate) const ME: (&str, &str) = ("test", "0");

    #[test]
    fn an_older_protocol_version_is_accepted_and_the_server_is_described() {
        let client = Client::connect(Box::new(plain_server("2024-11-05")), ME, QUICK).unwrap();
        assert_eq!(client.protocol, "2024-11-05");
        assert_eq!(client.server_name.as_deref(), Some("demo"));
        assert_eq!(client.instructions.as_deref(), Some("use it well"));
    }

    #[test]
    fn a_version_nobody_knows_is_refused() {
        let Err(error) = Client::connect(Box::new(plain_server("1999-01-01")), ME, QUICK) else {
            panic!("should refuse");
        };
        assert!(error.to_string().contains("1999-01-01"), "{error}");
    }

    #[test]
    fn log_lines_pings_and_notifications_do_not_get_in_the_way() {
        let server = Scripted::new(|request| match request["method"].as_str().unwrap_or("") {
            "initialize" => vec![
                "Starting server v1...".into(),
                json!({ "jsonrpc": "2.0", "method": "notifications/message", "params": {} })
                    .to_string(),
                json!({ "jsonrpc": "2.0", "id": 99, "method": "ping" }).to_string(),
                json!({ "jsonrpc": "2.0", "id": 98, "method": "roots/list" }).to_string(),
                reply(request, json!({ "protocolVersion": "2025-06-18" })),
            ],
            _ => Vec::new(),
        });
        let sent = server.sent.clone();
        Client::connect(Box::new(server), ME, QUICK).expect("handshake");
        let sent = sent.lock().unwrap();
        let ping = sent.iter().find(|m| m["id"] == 99).expect("ping answered");
        assert_eq!(ping["result"], json!({}));
        let roots = sent.iter().find(|m| m["id"] == 98).expect("roots answered");
        assert_eq!(roots["error"]["code"], json!(-32601));
        assert_eq!(
            sent.last().unwrap()["method"],
            json!("notifications/initialized")
        );
    }

    #[test]
    fn every_page_of_tools_is_read() {
        let server = Scripted::new(|request| match request["method"].as_str().unwrap_or("") {
            "initialize" => vec![reply(request, json!({ "protocolVersion": "2025-06-18" }))],
            "tools/list" if request["params"]["cursor"] == json!("p2") => {
                vec![reply(request, json!({ "tools": [{ "name": "b" }] }))]
            }
            "tools/list" => vec![reply(
                request,
                json!({ "tools": [{ "name": "a" }], "nextCursor": "p2" }),
            )],
            _ => Vec::new(),
        });
        let mut client = Client::connect(Box::new(server), ME, QUICK).unwrap();
        let names: Vec<Value> = client
            .list_tools(QUICK)
            .unwrap()
            .into_iter()
            .map(|t| t["name"].clone())
            .collect();
        assert_eq!(names, [json!("a"), json!("b")]);
    }

    #[test]
    fn a_json_rpc_error_keeps_the_connection_and_a_hang_up_does_not() {
        let server = Scripted::new(|request| match request["method"].as_str().unwrap_or("") {
            "initialize" => vec![reply(request, json!({ "protocolVersion": "2025-06-18" }))],
            "tools/call" if request["params"]["name"] == json!("bad") => vec![
                json!({
                    "jsonrpc": "2.0", "id": request["id"],
                    "error": { "code": -32602, "message": "Invalid arguments" }
                })
                .to_string(),
            ],
            "tools/call" => vec!["<closed>".into()],
            _ => Vec::new(),
        });
        let mut client = Client::connect(Box::new(server), ME, QUICK).unwrap();
        let refused = client.call_tool("bad", json!({}), QUICK).unwrap_err();
        assert!(matches!(refused, Error::Refused { code: -32602, .. }));
        assert!(!refused.breaks_connection());
        let gone = client.call_tool("other", json!({}), QUICK).unwrap_err();
        assert!(gone.breaks_connection());
        assert!(gone.to_string().contains("bye"), "{gone}");
    }

    #[test]
    fn silence_is_a_timeout_not_a_hang() {
        let server = Scripted::new(|request| match request["method"].as_str().unwrap_or("") {
            "initialize" => vec![reply(request, json!({ "protocolVersion": "2025-06-18" }))],
            _ => Vec::new(),
        });
        let mut client = Client::connect(Box::new(server), ME, QUICK).unwrap();
        let error = client.call_tool("slow", json!({}), QUICK).unwrap_err();
        assert!(matches!(error, Error::Timeout { .. }), "{error}");
    }
}
