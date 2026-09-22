//! 内存里的 MCP server，给产品写测试用：按收到的请求现编回复，不起进程。
//!
//! 放在公开的模块里而不是 `#[cfg(test)]` 下，是因为 gld 和 ccnm 的测试都要它，
//! 而一个 crate 的测试代码别的 crate 看不见。它不起进程、不碰网络，留在正式
//! 构建里也只是几十行没人调的代码。

use std::collections::VecDeque;
use std::sync::{Arc, Mutex};
use std::time::Duration;

use serde_json::{Value, json};

use crate::client::Error;
use crate::transport::{Recv, Transport};

/// 收到一条请求，回哪几行。
type Answer = Box<dyn FnMut(&Value) -> Vec<String> + Send>;

/// 内存里的 server：按收到的请求现编回复，不起进程。
pub struct Scripted {
    pub sent: Arc<Mutex<Vec<Value>>>,
    pending: VecDeque<Recv>,
    answer: Answer,
}

impl Scripted {
    pub fn new(answer: impl FnMut(&Value) -> Vec<String> + Send + 'static) -> Scripted {
        Scripted {
            sent: Arc::new(Mutex::new(Vec::new())),
            pending: VecDeque::new(),
            answer: Box::new(answer),
        }
    }
}

impl Transport for Scripted {
    fn send(&mut self, line: &str, _timeout: Duration) -> Result<(), Error> {
        let message: Value = serde_json::from_str(line).expect("the client sends JSON");
        self.sent.lock().unwrap().push(message.clone());
        for reply in (self.answer)(&message) {
            self.pending.push_back(if reply == "<closed>" {
                Recv::Closed { said: "bye".into() }
            } else {
                Recv::Line(reply)
            });
        }
        Ok(())
    }
    fn recv(&mut self, _timeout: Duration) -> Recv {
        self.pending.pop_front().unwrap_or(Recv::Timeout)
    }
    fn close(&mut self) {}
}

pub fn reply(request: &Value, result: Value) -> String {
    json!({ "jsonrpc": "2.0", "id": request["id"], "result": result }).to_string()
}

/// 一个最普通的 server：握手回 `version`，两个工具，调用回显参数。
pub fn plain_server(version: &'static str) -> Scripted {
    Scripted::new(
        move |request| match request["method"].as_str().unwrap_or("") {
            "initialize" => vec![reply(
                request,
                json!({ "protocolVersion": version, "serverInfo": { "name": "demo", "version": "1.0" }, "instructions": "use it well" }),
            )],
            "tools/list" => vec![reply(
                request,
                json!({ "tools": [
                { "name": "echo", "description": "Echo back", "inputSchema": { "type": "object" } },
                { "name": "drop_table", "description": "Dangerous", "inputSchema": { "type": "object" } }
            ] }),
            )],
            "tools/call" => vec![reply(
                request,
                json!({ "content": [{ "type": "text", "text": request["params"]["arguments"].to_string() }] }),
            )],
            _ => Vec::new(),
        },
    )
}
