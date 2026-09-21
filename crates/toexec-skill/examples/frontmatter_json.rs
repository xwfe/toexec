//! 把一批 SKILL.md 读成 JSON，一个文件一行。给差分测试用：同一批文件交给
//! Claude Code 自己的解析器（`evidence/x08-skill-frontmatter/host_oracle.js`），
//! 两边的输出逐个比对。
//!
//! ```text
//! find . -name SKILL.md | cargo run -q -p toexec-skill --example frontmatter_json
//! ```
//!
//! 读的是 stdin 里的路径，一行一个。输出的形状写死在这里，改了要同步
//! `compare.py`：
//!
//! - `front`：[`split`] 拆出来的 frontmatter 原文，没有就是 `null`
//! - `parse`：`{"ok": 值}` 或 `{"err": {"line": n, "kind": "…"}}`
//! - `reading`：`Yaml` / `Requoted` / `Lenient`，见 [`Reading`]
//! - `fields`：产品实际会取的那几个字段，经 `text` / `string` / `flag` / `words` 读出来；
//!   两个开关直接给出按宿主规则算出来的布尔值（见 `Frontmatter::flag` 的说明）
//! - `duplicates`：归一化之后重名的顶层键和行号
//!
//! 值的编码保留 JSON 表达不了的东西：映射是 `{"map": [[键, 值], …]}`（重复键和
//! 顺序都在），数字是 `{"num": "原文"}`（`1.10` 不会变成 `1.1`）。

use std::io::{self, BufRead, Write};

use toexec_skill::Value;
use toexec_skill::frontmatter::{self, Reading, split};

fn main() {
    let stdin = io::stdin();
    let mut out = io::BufWriter::new(io::stdout().lock());
    for path in stdin.lock().lines() {
        let path = path.expect("stdin");
        if path.is_empty() {
            continue;
        }
        let line = match std::fs::read(&path) {
            Ok(bytes) => match String::from_utf8(bytes) {
                Ok(raw) => one(&path, &raw),
                Err(_) => format!("{{\"path\":{},\"skip\":\"not utf-8\"}}", string(&path)),
            },
            Err(e) => format!(
                "{{\"path\":{},\"skip\":{}}}",
                string(&path),
                string(&e.to_string())
            ),
        };
        writeln!(out, "{line}").expect("stdout");
    }
}

fn one(path: &str, raw: &str) -> String {
    let (front, _) = split(raw);
    let parsed = frontmatter::parse(front.unwrap_or_default());
    let mut json = format!(
        "{{\"path\":{},\"front\":{},",
        string(path),
        front.map(string).unwrap_or_else(|| "null".into())
    );
    match &parsed {
        Ok(fm) => {
            let map = Value::Map(fm.entries().to_vec());
            json.push_str(&format!("\"parse\":{{\"ok\":{}}},", value(&map)));
            let reading = match fm.reading() {
                Reading::Yaml => "Yaml",
                Reading::Requoted => "Requoted",
                Reading::Lenient => "Lenient",
            };
            let duplicates: Vec<String> = fm
                .duplicates()
                .iter()
                .map(|group| {
                    let items: Vec<String> = group
                        .iter()
                        .map(|(k, line)| format!("[{},{line}]", string(k)))
                        .collect();
                    format!("[{}]", items.join(","))
                })
                .collect();
            json.push_str(&format!(
                "\"reading\":\"{reading}\",\"duplicates\":[{}],",
                duplicates.join(",")
            ));
            let text = |key| fm.text(key).map(string).unwrap_or_else(|| "null".into());
            // 宿主对 name、when_to_use、argument-hint 做的是 String(值)。
            let shown = |key| {
                fm.string(key)
                    .map(|s| string(&s))
                    .unwrap_or_else(|| "null".into())
            };
            let hidden_from_model = fm.flag("disable-model-invocation") == Some(true);
            let user_invocable =
                fm.get("user-invocable").is_none() || fm.flag("user-invocable") == Some(true);
            let words: Vec<String> = fm.words("arguments").iter().map(|w| string(w)).collect();
            json.push_str(&format!(
                "\"fields\":{{\"name\":{},\"description\":{},\"when_to_use\":{},\"argument_hint\":{},\"disable_model_invocation\":{},\"user_invocable\":{},\"arguments\":[{}]}}",
                shown("name"),
                text("description"),
                shown("when_to_use"),
                shown("argument-hint"),
                hidden_from_model,
                user_invocable,
                words.join(","),
            ));
        }
        Err(e) => json.push_str(&format!(
            "\"parse\":{{\"err\":{{\"line\":{},\"kind\":\"{:?}\"}}}}",
            e.line, e.kind
        )),
    }
    json.push('}');
    json
}

fn value(v: &Value) -> String {
    match v {
        Value::Null => "null".into(),
        Value::Bool(b) => b.to_string(),
        Value::Number(n) => format!("{{\"num\":{}}}", string(n)),
        Value::String(s) => string(s),
        Value::List(items) => {
            let items: Vec<String> = items.iter().map(value).collect();
            format!("[{}]", items.join(","))
        }
        Value::Map(entries) => {
            let entries: Vec<String> = entries
                .iter()
                .map(|(k, v)| format!("[{},{}]", string(k), value(v)))
                .collect();
            format!("{{\"map\":[{}]}}", entries.join(","))
        }
    }
}

fn string(s: &str) -> String {
    let mut out = String::with_capacity(s.len() + 2);
    out.push('"');
    for c in s.chars() {
        match c {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            c if (c as u32) < 0x20 => out.push_str(&format!("\\u{:04x}", c as u32)),
            c => out.push(c),
        }
    }
    out.push('"');
    out
}
