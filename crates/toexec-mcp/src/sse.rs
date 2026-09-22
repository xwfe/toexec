//! Streamable HTTP 回复里的 SSE（`text/event-stream`）怎么拆。
//!
//! MCP 的 streamable HTTP 每条消息一次 POST，回复要么是一个 JSON，要么是一段
//! SSE：一个事件一条消息，自己那条回复前面可能先来几条通知或 server 发的请求。
//! 实测 deepwiki、exa 都回 SSE。怎么发请求（HTTP 客户端、代理、TLS）各产品不同
//! ——gld 用 reqwest，ccnm 用系统的 curl——拆事件这一段是同一件事，放这里。

use serde_json::Value;

/// 缓冲里第一个事件在哪结束：空行（`\n\n` 或 `\r\n\r\n`）。返回事件长度和
/// 分隔符长度。
pub fn event_end(buffer: &[u8]) -> Option<(usize, usize)> {
    let lf = buffer
        .windows(2)
        .position(|w| w == b"\n\n")
        .map(|at| (at, 2));
    let crlf = buffer
        .windows(4)
        .position(|w| w == b"\r\n\r\n")
        .map(|at| (at, 4));
    match (lf, crlf) {
        (Some(a), Some(b)) => Some(if a.0 <= b.0 { a } else { b }),
        (one, other) => one.or(other),
    }
}

/// 一个事件里所有 `data:` 行拼起来（SSE 规定多行 data 用换行连接）。
/// 没有 data（注释、只有 `event:` 的心跳）返回 `None`。
pub fn event_data(event: &[u8]) -> Option<String> {
    let text = String::from_utf8_lossy(event);
    let lines: Vec<&str> = text
        .lines()
        .filter_map(|line| line.strip_prefix("data:"))
        .map(|data| data.strip_prefix(' ').unwrap_or(data))
        .collect();
    if lines.is_empty() {
        None
    } else {
        Some(lines.join("\n"))
    }
}

/// 这段数据是不是 `id` 那条请求的回复（批量回复里有它也算）。等到它就可以
/// 不再读这条流：server 可能让流一直开着。
pub fn answers(data: &str, id: &Value) -> bool {
    let is_reply =
        |message: &Value| message.get("id") == Some(id) && message.get("method").is_none();
    match serde_json::from_str::<Value>(data) {
        Ok(Value::Array(batch)) => batch.iter().any(is_reply),
        Ok(message) => is_reply(&message),
        Err(_) => false,
    }
}

/// 要等回复的那条消息的 `id`：是请求（有 `method` 和 `id`）才有。通知和
/// 给 server 的回复都不等。
pub fn awaited_id(line: &str) -> Option<Value> {
    serde_json::from_str::<Value>(line)
        .ok()
        .filter(|message| message.get("method").is_some())
        .and_then(|message| message.get("id").cloned())
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn events_split_on_either_line_ending_and_join_their_data_lines() {
        let buffer = b"data: {\"a\":\ndata: 1}\r\n\r\nrest";
        let (end, skip) = event_end(buffer).unwrap();
        assert_eq!(skip, 4);
        assert_eq!(event_data(&buffer[..end]).as_deref(), Some("{\"a\":\n1}"));
        assert_eq!(event_data(b": comment"), None);
    }

    #[test]
    fn only_the_reply_to_that_id_ends_the_wait() {
        let id = json!(7);
        assert!(answers(r#"{"jsonrpc":"2.0","id":7,"result":{}}"#, &id));
        assert!(answers(r#"[{"method":"x"},{"id":7,"error":{}}]"#, &id));
        // server 发来的同号请求不是回复。
        assert!(!answers(r#"{"id":7,"method":"ping"}"#, &id));
        assert!(!answers(r#"{"id":8,"result":{}}"#, &id));
        assert!(!answers("not json", &id));
    }

    #[test]
    fn only_a_request_waits_for_a_reply() {
        assert_eq!(
            awaited_id(r#"{"jsonrpc":"2.0","id":3,"method":"tools/list"}"#),
            Some(json!(3))
        );
        assert_eq!(
            awaited_id(r#"{"jsonrpc":"2.0","method":"notifications/initialized"}"#),
            None
        );
        assert_eq!(awaited_id(r#"{"jsonrpc":"2.0","id":3,"result":{}}"#), None);
    }
}
