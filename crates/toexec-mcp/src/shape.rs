//! server 的结果和工具清单交给模型之前的整理。
//!
//! 实测（toexec `evidence/v4-mcp/machine-mcp/`）deepwiki 的 `read_wiki_contents`
//! 一次回 839 KB：407 KB 正文，外加一份内容相同的 `structuredContent`。所以：
//!
//! - 有文字内容时**不带** `structuredContent`：转发的工具没有 `outputSchema`，
//!   按协议客户端本来就不看它；而 Claude Code 在两者都有时只给模型看结构化那份
//!   （ccnm 2.1.260 实测），同一份东西发两遍还白白翻倍。只有它、没有文字时，把它
//!   转成文字。
//! - 文字超过上限的，**全文交给产品**（[`Shaped::long`]），由产品先交一段、把
//!   全文留着让模型接着读——这里不截断，也不替产品决定留在哪（gld 放内存，ccnm
//!   放它给命令输出用的留存目录）。
//! - 图片、音频、二进制资源原样带，单个超过上限的换成一句说明。
//!
//! 哪一段交多少、怎么接着读的说明怎么写，是产品的事；这里给切段用的
//! [`part_end`]。

use serde_json::{Value, json};

/// 整理用的两个上限。
#[derive(Debug, Clone, Copy)]
pub struct Limits {
    /// 文字加起来超过这么多，就整段交给产品分段。
    pub inline_bytes: usize,
    /// 单张图片 / 单段音频 / 单个二进制资源（base64 之后）最多这么大。
    pub max_media_bytes: usize,
}

/// 整理完的结果。
#[derive(Debug, Clone, PartialEq)]
pub struct Shaped {
    /// server 说"没办成"（`isError: true`）。那是它的结果，照样交。
    pub is_error: bool,
    /// 原样交出去的内容项。[`Shaped::long`] 有值时，这里只剩不是文字的那些。
    pub items: Vec<Value>,
    /// 文字太多时的全文（各文字项按顺序用换行接起来）。
    pub long: Option<String>,
}

/// 整理一个 `tools/call` 的结果。
pub fn shape(result: &Value, limits: &Limits) -> Shaped {
    let is_error = result.get("isError") == Some(&Value::Bool(true));
    let mut items: Vec<Value> = result
        .get("content")
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default();
    if !items.iter().any(|item| text_of(item).is_some())
        && let Some(structured) = result.get("structuredContent")
    {
        items.push(json!({ "type": "text", "text": structured.to_string() }));
    }
    let items: Vec<Value> = items
        .into_iter()
        .map(|item| limit_media(item, limits.max_media_bytes))
        .collect();
    let text_bytes: usize = items.iter().filter_map(text_of).map(str::len).sum();
    if text_bytes <= limits.inline_bytes {
        return Shaped {
            is_error,
            items,
            long: None,
        };
    }
    let long = items
        .iter()
        .filter_map(text_of)
        .collect::<Vec<_>>()
        .join("\n");
    Shaped {
        is_error,
        items: items
            .into_iter()
            .filter(|item| text_of(item).is_none())
            .collect(),
        long: Some(long),
    }
}

/// 一个内容项里的文字：文字项，或者带文字的内嵌资源。
pub fn text_of(item: &Value) -> Option<&str> {
    match item.get("type").and_then(Value::as_str) {
        Some("text") => item.get("text").and_then(Value::as_str),
        Some("resource") => item
            .get("resource")
            .and_then(|resource| resource.get("text"))
            .and_then(Value::as_str),
        _ => None,
    }
}

/// 太大的图片 / 音频 / 二进制资源换成一句说明。
fn limit_media(item: Value, max: usize) -> Value {
    let kind = item.get("type").and_then(Value::as_str).unwrap_or_default();
    let (data, mime) = match kind {
        "image" | "audio" => (item.get("data"), item.get("mimeType")),
        "resource" => {
            let resource = item.get("resource");
            (
                resource.and_then(|r| r.get("blob")),
                resource.and_then(|r| r.get("mimeType")),
            )
        }
        _ => return item,
    };
    let size = data.and_then(Value::as_str).map_or(0, str::len);
    if size <= max {
        return item;
    }
    let mime = mime.and_then(Value::as_str).unwrap_or("binary data");
    json!({
        "type": "text",
        "text": format!(
            "[left out a {kind} ({mime}) of {size} bytes (base64): over the limit of {max} bytes per item]"
        )
    })
}

/// 一个 server 的工具清单，放不进 `max_bytes` 时先去参数表，再去描述。返回
/// 清单和"去掉了什么"（给模型看的一句话，没去掉是 `None`）。
///
/// 实测最大的是 playwright：25 个工具 21 KB，64 KiB 的上限原样放得下。
pub fn fit_listing(tools: &[Value], max_bytes: usize) -> (Vec<Value>, Option<&'static str>) {
    let brief = |tool: &Value, with_schema: bool, with_description: bool| {
        let mut out = json!({ "name": tool["name"] });
        if let Some(title) = tool
            .get("title")
            .or_else(|| tool["annotations"].get("title"))
        {
            out["title"] = title.clone();
        }
        if with_description && let Some(description) = tool.get("description") {
            out["description"] = description.clone();
        }
        if with_schema {
            if let Some(schema) = tool.get("inputSchema") {
                out["inputSchema"] = schema.clone();
            }
            if let Some(annotations) = tool.get("annotations") {
                out["annotations"] = annotations.clone();
            }
        }
        out
    };
    let steps = [
        (true, true, None),
        (
            false,
            true,
            Some("input schemas, to fit; ask for one tool with tool=<name> to get its schema"),
        ),
        (
            false,
            false,
            Some("descriptions and input schemas, to fit; ask for one tool with tool=<name>"),
        ),
    ];
    let last = steps.len() - 1;
    for (step, (with_schema, with_description, omitted)) in steps.into_iter().enumerate() {
        let listed: Vec<Value> = tools
            .iter()
            .map(|tool| brief(tool, with_schema, with_description))
            .collect();
        // 最后一步不再量：只剩名字和标题还放不下，那是几千个工具，照样给。
        if step == last || Value::Array(listed.clone()).to_string().len() <= max_bytes {
            return (listed, omitted);
        }
    }
    unreachable!("the last step always returns")
}

/// `[start, end)` 这一段的结尾：不超过 `max` 字节，尽量停在换行后面，至少
/// 停在字符边界上，并且至少往前走一个字符。
pub fn part_end(text: &str, start: usize, max: usize) -> usize {
    let limit = start.saturating_add(max);
    if limit >= text.len() {
        return text.len();
    }
    let end = floor_boundary(text, limit);
    match text[start..end].rfind('\n') {
        // 离开头太近的换行不要：一行特别长时宁可在行中间断。
        Some(at) if at >= max / 2 => start + at + 1,
        _ if end > start => end,
        _ => ceil_boundary(text, start + 1),
    }
}

/// 不超过 `at` 的最后一个字符边界。
pub fn floor_boundary(text: &str, mut at: usize) -> usize {
    at = at.min(text.len());
    while !text.is_char_boundary(at) {
        at -= 1;
    }
    at
}

/// 不小于 `at` 的第一个字符边界。
pub fn ceil_boundary(text: &str, mut at: usize) -> usize {
    at = at.min(text.len());
    while !text.is_char_boundary(at) {
        at += 1;
    }
    at
}

/// 超过 `max` 字节就在字符边界上截断，并写明截在哪。
pub fn cut_text(text: &str, max: usize) -> String {
    if text.len() <= max {
        return text.to_string();
    }
    let end = floor_boundary(text, max);
    format!("{}… [cut at {end} of {} bytes]", &text[..end], text.len())
}

#[cfg(test)]
mod tests {
    use super::*;

    const LIMITS: Limits = Limits {
        inline_bytes: 100,
        max_media_bytes: 10,
    };

    #[test]
    fn a_duplicate_structured_copy_goes_and_a_lone_one_becomes_text() {
        let both = shape(
            &json!({ "content": [{ "type": "text", "text": "{\"n\":1}" }], "structuredContent": { "n": 1 } }),
            &LIMITS,
        );
        assert_eq!(both.items, [json!({ "type": "text", "text": "{\"n\":1}" })]);
        let lone = shape(
            &json!({ "content": [], "structuredContent": { "n": 2 } }),
            &LIMITS,
        );
        assert_eq!(lone.items, [json!({ "type": "text", "text": "{\"n\":2}" })]);
    }

    #[test]
    fn too_much_text_is_handed_over_whole_with_the_other_items() {
        let shaped = shape(
            &json!({ "isError": true, "content": [
                { "type": "text", "text": "a".repeat(80) },
                { "type": "image", "data": "abc", "mimeType": "image/png" },
                { "type": "resource", "resource": { "uri": "x", "text": "b".repeat(80) } }
            ] }),
            &LIMITS,
        );
        assert!(shaped.is_error);
        assert_eq!(shaped.long.as_deref().map(str::len), Some(161));
        assert_eq!(shaped.items.len(), 1, "只剩图片");
        assert_eq!(shaped.items[0]["type"], json!("image"));
    }

    #[test]
    fn an_oversized_image_is_replaced_by_a_note() {
        let shaped = shape(
            &json!({ "content": [{ "type": "image", "data": "a".repeat(11), "mimeType": "image/png" }] }),
            &LIMITS,
        );
        let note = shaped.items[0]["text"].as_str().unwrap();
        assert!(
            note.contains("image/png") && note.contains("11 bytes"),
            "{note}"
        );
    }

    #[test]
    fn parts_end_after_a_newline_or_on_a_character_boundary() {
        let text = "0123456789\n".repeat(10);
        assert_eq!(part_end(&text, 0, 30), 22, "停在换行后面");
        let wide = "é".repeat(10);
        let end = part_end(&wide, 0, 3);
        assert!(wide.is_char_boundary(end) && end <= 3);
        assert_eq!(part_end(&wide, 0, 1), 2, "比一个字符还小也要往前走");
        assert_eq!(part_end(&wide, 4, 1000), wide.len());
    }

    #[test]
    fn a_listing_too_big_drops_schemas_first_then_descriptions() {
        let tools: Vec<Value> = (0..50)
            .map(|i| json!({
                "name": format!("t{i}"),
                "description": "d".repeat(100),
                "inputSchema": { "type": "object", "properties": { "x": { "description": "s".repeat(400) } } }
            }))
            .collect();
        let (listed, omitted) = fit_listing(&tools, 64 * 1024);
        assert!(omitted.is_none());
        assert!(listed[0].get("inputSchema").is_some());
        let (listed, omitted) = fit_listing(&tools, 8 * 1024);
        assert!(omitted.unwrap().starts_with("input schemas"));
        assert!(listed[0].get("inputSchema").is_none() && listed[0].get("description").is_some());
        let (listed, omitted) = fit_listing(&tools, 100);
        assert!(omitted.unwrap().starts_with("descriptions"));
        assert!(listed[0].get("description").is_none());
    }

    #[test]
    fn cut_text_says_where() {
        assert_eq!(cut_text("short", 10), "short");
        assert_eq!(cut_text("abcdef", 3), "abc… [cut at 3 of 6 bytes]");
    }
}
