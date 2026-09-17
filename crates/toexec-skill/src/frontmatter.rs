//! frontmatter：文件开头两条 `---` 之间的那一段 YAML。
//!
//! 为什么不用 YAML 库：两个产品都守着依赖数，而 skill 作者实际写的只是 YAML
//! 的一小块。这里读的是**块状**的那一半——键值、缩进嵌套、`- ` 列表、
//! `|` / `>` 块标量、引号（可以跨行）、`[a, b]` 和 `{k: v}`（可以跨行，但不能
//! 再嵌套）。锚点、别名、标签、多文档都不认，遇到就报错并说是第几行，不猜。
//!
//! 「可以跨行」那两条是拿真实语料验出来的：本机 941 个 SKILL.md / 命令文件里，
//! Anthropic 官方插件就有两个这么写（格式化工具把长描述折成跨行的引号串，把
//! `allowed-tools` 折成每项一行的 `[ … ]`）。
//!
//! 为什么连 `hooks:` 那种三层嵌套也要读得进来，哪怕没有人用它的值：读不进来
//! 的话，一个带 hooks 的 skill 会整个解析失败，连名字和描述都拿不到。要忽略
//! 一个字段，先得能跨过它。
//!
//! 之前 gld 自己的读法是逐行找 `key:` 前缀，`description: >` 这种多行写法
//! 读出来的描述就是一个 `>`。

use std::fmt;

/// frontmatter 里的一个值。
///
/// 数字保留原文：skill 的版本号写成 `1.10` 时，读成浮点再写回去就成了 `1.1`。
#[derive(Debug, Clone, PartialEq)]
pub enum Value {
    Null,
    Bool(bool),
    Number(String),
    String(String),
    List(Vec<Value>),
    Map(Vec<(String, Value)>),
}

impl Value {
    /// 标量的文本形式；列表和映射没有。
    pub fn as_text(&self) -> Option<&str> {
        match self {
            Value::String(s) | Value::Number(s) => Some(s),
            _ => None,
        }
    }
}

/// 读不下去的原因。措辞由产品自己定，这里只给分类和行号。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ErrorKind {
    /// 用了 tab 缩进。YAML 不允许，而且一个 tab 算几格没有答案。
    TabIndent,
    /// 这一行既不是 `key: value`，也不是 `- item`，也不是上一个值的续行。
    Unexpected,
    /// 引号没有闭合，或者转义写错了。
    BadQuote,
    /// 认得出是什么、但这里不支持：锚点、别名、标签、嵌套的 `[...]` / `{...}`。
    Unsupported,
    /// 嵌套太深。正常的 frontmatter 到不了这里。
    TooDeep,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ParseError {
    /// frontmatter 里的第几行，从 1 数。
    pub line: usize,
    pub kind: ErrorKind,
}

impl fmt::Display for ParseError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        let what = match self.kind {
            ErrorKind::TabIndent => "tab used for indentation",
            ErrorKind::Unexpected => "unexpected content",
            ErrorKind::BadQuote => "unterminated or malformed quoted string",
            ErrorKind::Unsupported => "unsupported YAML construct",
            ErrorKind::TooDeep => "nesting too deep",
        };
        write!(f, "frontmatter line {}: {what}", self.line)
    }
}

impl std::error::Error for ParseError {}

/// 读好的 frontmatter。键的顺序和作者写的一致。
#[derive(Debug, Clone, Default, PartialEq)]
pub struct Frontmatter {
    entries: Vec<(String, Value)>,
}

impl Frontmatter {
    pub fn entries(&self) -> &[(String, Value)] {
        &self.entries
    }

    /// 按键取值。`-` 和 `_` 不分、大小写不分：官方文档里 `when_to_use` 用下划线、
    /// `disable-model-invocation` 用连字符，作者两种都会写。
    pub fn get(&self, key: &str) -> Option<&Value> {
        self.entries
            .iter()
            .find(|(k, _)| same_key(k, key))
            .map(|(_, v)| v)
    }

    /// 标量字段的文本，去掉首尾空白；空的当没有。
    pub fn text(&self, key: &str) -> Option<&str> {
        let text = self.get(key)?.as_text()?.trim();
        (!text.is_empty()).then_some(text)
    }

    /// 布尔字段。写成 `"true"` 这种带引号的也认——作者经常这么写。
    pub fn flag(&self, key: &str) -> Option<bool> {
        match self.get(key)? {
            Value::Bool(b) => Some(*b),
            Value::String(s) => match s.trim().to_ascii_lowercase().as_str() {
                "true" => Some(true),
                "false" => Some(false),
                _ => None,
            },
            _ => None,
        }
    }

    /// 「一串词」字段：`arguments`、`allowed-tools`。官方允许写成 YAML 列表，也
    /// 允许写成一个用空白或逗号分开的字符串。
    pub fn words(&self, key: &str) -> Vec<String> {
        match self.get(key) {
            Some(Value::List(items)) => items
                .iter()
                .filter_map(|v| v.as_text())
                .map(|s| s.trim().to_string())
                .filter(|s| !s.is_empty())
                .collect(),
            Some(v) => v
                .as_text()
                .map(|s| {
                    s.split(|c: char| c.is_whitespace() || c == ',')
                        .filter(|w| !w.is_empty())
                        .map(str::to_string)
                        .collect()
                })
                .unwrap_or_default(),
            None => Vec::new(),
        }
    }
}

fn same_key(a: &str, b: &str) -> bool {
    let norm = |c: char| {
        if c == '_' {
            '-'
        } else {
            c.to_ascii_lowercase()
        }
    };
    a.chars().map(norm).eq(b.chars().map(norm))
}

/// 把文件拆成 frontmatter 原文和正文。
///
/// 没有 frontmatter（第一行不是 `---`，或者找不到结束的 `---`）时前一项是
/// `None`，正文是整个文件：一个只有正文的 `.claude/commands/x.md` 是合法的。
pub fn split(raw: &str) -> (Option<&str>, &str) {
    let text = raw.strip_prefix('\u{feff}').unwrap_or(raw);
    let mut lines = text.split_inclusive('\n');
    let Some(first) = lines.next() else {
        return (None, text);
    };
    if first.trim_end() != "---" {
        return (None, text);
    }
    let start = first.len();
    let mut at = start;
    for line in lines {
        let bare = line.trim_end();
        if bare == "---" || bare == "..." {
            return (Some(&text[start..at]), &text[at + line.len()..]);
        }
        at += line.len();
    }
    (None, text)
}

/// 读 frontmatter 原文（[`split`] 的第一项）。
pub fn parse(text: &str) -> Result<Frontmatter, ParseError> {
    let mut p = Parser::new(text)?;
    let entries = match p.peek() {
        None => Vec::new(),
        Some(at) => {
            let indent = p.lines[at].indent;
            p.map(indent, 0)?
        }
    };
    if let Some(at) = p.peek() {
        return Err(p.error(at, ErrorKind::Unexpected));
    }
    Ok(Frontmatter { entries })
}

const MAX_DEPTH: usize = 32;

struct Line {
    /// 在 frontmatter 里的行号，从 1 数。
    number: usize,
    indent: usize,
    /// 去掉缩进和行尾空白之后的内容。空行是空串。
    text: String,
}

struct Parser {
    lines: Vec<Line>,
    at: usize,
}

impl Parser {
    fn new(text: &str) -> Result<Self, ParseError> {
        let mut lines = Vec::new();
        for (i, raw) in text.lines().enumerate() {
            let body = raw.trim_end();
            let indent = body.len() - body.trim_start_matches(' ').len();
            if body[indent..].starts_with('\t') {
                return Err(ParseError {
                    line: i + 1,
                    kind: ErrorKind::TabIndent,
                });
            }
            lines.push(Line {
                number: i + 1,
                indent,
                text: body[indent..].to_string(),
            });
        }
        Ok(Parser { lines, at: 0 })
    }

    fn error(&self, at: usize, kind: ErrorKind) -> ParseError {
        ParseError {
            line: self.lines[at].number,
            kind,
        }
    }

    /// 下一个有内容的行（跳过空行和整行注释），不前进。
    fn peek(&self) -> Option<usize> {
        (self.at..self.lines.len()).find(|&i| {
            let t = &self.lines[i].text;
            !t.is_empty() && !t.starts_with('#')
        })
    }

    /// 缩进为 `indent` 的一组 `key: value`。
    fn map(&mut self, indent: usize, depth: usize) -> Result<Vec<(String, Value)>, ParseError> {
        let mut entries = Vec::new();
        while let Some(at) = self.peek() {
            if self.lines[at].indent != indent {
                break;
            }
            let Some((key, rest)) =
                split_key(&self.lines[at].text).map_err(|kind| self.error(at, kind))?
            else {
                break;
            };
            self.at = at + 1;
            // `key:   # 注释` 后面同样什么都没写，值在下面几行。
            let value = if is_blank_or_comment(&rest) {
                self.nested(indent, depth)?
            } else {
                self.inline(&rest, at, indent)?
            };
            entries.push((key, value));
        }
        Ok(entries)
    }

    /// `key:` 后面什么都没写：值在下面几行，或者就是空。
    fn nested(&mut self, key_indent: usize, depth: usize) -> Result<Value, ParseError> {
        let Some(at) = self.peek() else {
            return Ok(Value::Null);
        };
        if depth >= MAX_DEPTH {
            return Err(self.error(at, ErrorKind::TooDeep));
        }
        let line = &self.lines[at];
        let is_item = line.text == "-" || line.text.starts_with("- ");
        // YAML 允许列表项和它的键对齐，不必再缩进。
        if is_item && line.indent >= key_indent {
            let indent = line.indent;
            return Ok(Value::List(self.list(indent, depth + 1)?));
        }
        if line.indent <= key_indent {
            return Ok(Value::Null);
        }
        let indent = line.indent;
        if split_key(&line.text)
            .map_err(|kind| self.error(at, kind))?
            .is_some()
        {
            return Ok(Value::Map(self.map(indent, depth + 1)?));
        }
        // 值从下一行才开始的普通标量。
        self.at = at + 1;
        let first = self.lines[at].text.clone();
        self.inline(&first, at, key_indent)
    }

    fn list(&mut self, indent: usize, depth: usize) -> Result<Vec<Value>, ParseError> {
        let mut items = Vec::new();
        while let Some(at) = self.peek() {
            let line = &self.lines[at];
            if line.indent != indent || !(line.text == "-" || line.text.starts_with("- ")) {
                break;
            }
            let rest = line.text[1..].trim_start().to_string();
            if rest.is_empty() {
                self.at = at + 1;
                items.push(self.nested(indent, depth)?);
                continue;
            }
            let is_map = split_key(&rest)
                .map_err(|kind| self.error(at, kind))?
                .is_some();
            if is_map {
                // `- key: value` 是一个映射，它的第一个键写在了破折号这一行。把这一行
                // 改写成「缩进到键该在的位置」，剩下的就是普通的映射。
                let inner = indent + (line.text.len() - rest.len());
                self.lines[at].indent = inner;
                self.lines[at].text = rest;
                self.at = at;
                if depth >= MAX_DEPTH {
                    return Err(self.error(at, ErrorKind::TooDeep));
                }
                items.push(Value::Map(self.map(inner, depth + 1)?));
            } else {
                self.at = at + 1;
                items.push(self.inline(&rest, at, indent)?);
            }
        }
        Ok(items)
    }

    /// 写在 `key:` 或 `- ` 同一行上的值。`at` 是那一行，`indent` 是键的缩进——
    /// 续行和块标量的内容都必须比它缩得更深。
    fn inline(&mut self, rest: &str, at: usize, indent: usize) -> Result<Value, ParseError> {
        let line = self.lines[at].number;
        let fail = move |kind| ParseError { line, kind };
        match rest.chars().next() {
            Some('|') | Some('>') => self.block(rest, at, indent),
            Some('"') | Some('\'') => {
                let joined = self.until(rest, indent, |text| quoted(text).is_ok());
                let (text, tail) = quoted(&joined).map_err(fail)?;
                if !is_blank_or_comment(tail) {
                    return Err(fail(ErrorKind::Unexpected));
                }
                Ok(Value::String(text))
            }
            Some('[') => {
                let joined = self.until(rest, indent, |text| closes(text, '[', ']'));
                flow_list(&joined).map_err(fail)
            }
            Some('{') => {
                let joined = self.until(rest, indent, |text| closes(text, '{', '}'));
                flow_map(&joined).map_err(fail)
            }
            Some('&') | Some('*') | Some('!') | Some('%') | Some('@') | Some('`') => {
                Err(fail(ErrorKind::Unsupported))
            }
            _ => Ok(self.plain(rest, indent)),
        }
    }

    /// 一个值在这一行没写完（引号没闭合、括号没配上）：把缩进更深的后续行用
    /// 空格接上来，直到 `done` 说写完了。YAML 对跨行的引号串和流式集合就是这么
    /// 折行的。接不上（后面没有更深的行了）就把已有的还回去，让调用方照常报错。
    fn until(&mut self, first: &str, indent: usize, done: impl Fn(&str) -> bool) -> String {
        let mut text = first.to_string();
        let mut at = self.at;
        while !done(&text) && at < self.lines.len() {
            let line = &self.lines[at];
            if !line.text.is_empty() {
                if line.indent <= indent {
                    break;
                }
                text.push(' ');
                text.push_str(&line.text);
            }
            at += 1;
        }
        if done(&text) {
            self.at = at;
            return text;
        }
        first.to_string()
    }

    /// 普通标量，连同它缩进更深的续行。续行之间按 YAML 的规矩折成空格，空行
    /// 折成换行。
    fn plain(&mut self, first: &str, indent: usize) -> Value {
        let mut text = strip_comment(first).to_string();
        let mut continued = false;
        let mut blank_run = 0;
        while self.at < self.lines.len() {
            let line = &self.lines[self.at];
            if line.text.is_empty() {
                blank_run += 1;
                self.at += 1;
                continue;
            }
            if line.indent <= indent {
                break;
            }
            let piece = strip_comment(&line.text);
            if blank_run > 0 {
                text.push_str(&"\n".repeat(blank_run));
            } else {
                text.push(' ');
            }
            text.push_str(piece);
            blank_run = 0;
            continued = true;
            self.at += 1;
        }
        // 数到的空行后面没有续行，就不是这个值的一部分；退回去不影响什么，因为
        // 空行本来就会被跳过。
        if continued {
            return Value::String(text);
        }
        typed(&text)
    }

    /// `|`（保留换行）和 `>`（折行）块标量。
    fn block(&mut self, header: &str, at: usize, indent: usize) -> Result<Value, ParseError> {
        let folded = header.starts_with('>');
        let mut chomp = ' ';
        let mut explicit = None;
        let flags = strip_comment(&header[1..]);
        for c in flags.chars() {
            match c {
                '-' | '+' if chomp == ' ' => chomp = c,
                '1'..='9' if explicit.is_none() => explicit = c.to_digit(10).map(|d| d as usize),
                _ => return Err(self.error(at, ErrorKind::Unexpected)),
            }
        }
        let mut rows: Vec<(usize, String)> = Vec::new();
        while self.at < self.lines.len() {
            let line = &self.lines[self.at];
            if !line.text.is_empty() && line.indent <= indent {
                break;
            }
            rows.push((line.indent, line.text.clone()));
            self.at += 1;
        }
        let base = explicit.map(|n| indent + n).unwrap_or_else(|| {
            rows.iter()
                .find(|(_, t)| !t.is_empty())
                .map(|(i, _)| *i)
                .unwrap_or(indent + 1)
        });
        let rows: Vec<String> = rows
            .into_iter()
            .map(|(i, t)| {
                if t.is_empty() {
                    String::new()
                } else {
                    format!("{}{t}", " ".repeat(i.saturating_sub(base)))
                }
            })
            .collect();
        let mut text = if folded { fold(&rows) } else { rows.join("\n") };
        let body_len = text.trim_end_matches('\n').len();
        match chomp {
            '-' => text.truncate(body_len),
            '+' => text.push('\n'),
            _ => {
                text.truncate(body_len);
                if !text.is_empty() {
                    text.push('\n');
                }
            }
        }
        Ok(Value::String(text))
    }
}

/// `>` 的折行：相邻的普通行用空格接起来，空行变成换行，缩进更深的行原样保留。
fn fold(rows: &[String]) -> String {
    let mut out = String::new();
    let mut prev_plain = false;
    for row in rows {
        let plain = !row.is_empty() && !row.starts_with(' ');
        if out.is_empty() {
            out.push_str(row);
        } else if plain && prev_plain {
            out.push(' ');
            out.push_str(row);
        } else {
            // 空行本身就是那个换行；紧跟在空行后面的普通行不再另起一个。
            let after_blank = out.ends_with('\n');
            if !(plain && after_blank) || row.is_empty() {
                out.push('\n');
            }
            out.push_str(row);
        }
        prev_plain = plain;
    }
    out
}

/// `text` 以 `open` 开头，它的括号配上了没有。引号里的括号不算。
fn closes(text: &str, open: char, close: char) -> bool {
    let mut depth = 0usize;
    let mut quote: Option<char> = None;
    let mut escaped = false;
    for c in text.chars() {
        match quote {
            Some('"') if escaped => escaped = false,
            Some('"') if c == '\\' => escaped = true,
            Some(q) if c == q => quote = None,
            Some(_) => {}
            None if c == '"' || c == '\'' => quote = Some(c),
            None if c == open => depth += 1,
            None if c == close => {
                depth = depth.saturating_sub(1);
                if depth == 0 {
                    return true;
                }
            }
            None => {}
        }
    }
    false
}

/// 把一行拆成键和键后面的内容。不是 `key: …` 的形状就是 `None`。
fn split_key(text: &str) -> Result<Option<(String, String)>, ErrorKind> {
    if text.starts_with('"') || text.starts_with('\'') {
        // 引号在这一行没闭合：不是键，是一个跨行的引号串的开头。报不报错留给读值
        // 的那一边——它知道后面还有没有能接上来的行。
        let Ok((key, tail)) = quoted(text) else {
            return Ok(None);
        };
        return Ok(tail
            .strip_prefix(':')
            .filter(|rest| rest.is_empty() || rest.starts_with(' '))
            .map(|rest| (key, rest.trim().to_string())));
    }
    if text.starts_with("- ") || text == "-" || text.starts_with('[') || text.starts_with('{') {
        return Ok(None);
    }
    // 第一个「后面是空格或行尾」的冒号。`http://x` 里的冒号不算。
    let mut from = 0;
    while let Some(off) = text[from..].find(':') {
        let at = from + off;
        let rest = &text[at + 1..];
        if rest.is_empty() || rest.starts_with(' ') {
            let key = text[..at].trim();
            if key.is_empty() {
                return Ok(None);
            }
            return Ok(Some((key.to_string(), rest.trim().to_string())));
        }
        from = at + 1;
    }
    Ok(None)
}

/// 读一个带引号的串，返回内容和引号后面剩下的部分。
fn quoted(text: &str) -> Result<(String, &str), ErrorKind> {
    let mut chars = text.char_indices();
    let (_, quote) = chars.next().ok_or(ErrorKind::BadQuote)?;
    let mut out = String::new();
    while let Some((i, c)) = chars.next() {
        if quote == '\'' {
            if c != '\'' {
                out.push(c);
            } else if text[i + 1..].starts_with('\'') {
                out.push('\'');
                chars.next();
            } else {
                return Ok((out, &text[i + 1..]));
            }
            continue;
        }
        match c {
            '"' => return Ok((out, &text[i + 1..])),
            '\\' => {
                let (_, esc) = chars.next().ok_or(ErrorKind::BadQuote)?;
                match esc {
                    'n' => out.push('\n'),
                    't' => out.push('\t'),
                    'r' => out.push('\r'),
                    '0' => out.push('\0'),
                    '"' | '\\' | '/' | ' ' => out.push(esc),
                    'u' | 'x' | 'U' => {
                        let len = match esc {
                            'x' => 2,
                            'u' => 4,
                            _ => 8,
                        };
                        let hex: String = chars.by_ref().take(len).map(|(_, h)| h).collect();
                        let code =
                            u32::from_str_radix(&hex, 16).map_err(|_| ErrorKind::BadQuote)?;
                        out.push(char::from_u32(code).ok_or(ErrorKind::BadQuote)?);
                    }
                    _ => return Err(ErrorKind::BadQuote),
                }
            }
            _ => out.push(c),
        }
    }
    Err(ErrorKind::BadQuote)
}

/// 单行的 `[a, "b", c]`。
fn flow_list(text: &str) -> Result<Value, ErrorKind> {
    let inner = flow_inner(text, '[', ']')?;
    Ok(Value::List(
        flow_items(inner)?.into_iter().map(|(v, _)| v).collect(),
    ))
}

/// 单行的 `{a: 1, b: "x"}`，值只能是标量。
fn flow_map(text: &str) -> Result<Value, ErrorKind> {
    let inner = flow_inner(text, '{', '}')?;
    let mut entries = Vec::new();
    for (_, raw) in flow_items(inner)? {
        let (key, rest) = split_key(&raw)?.ok_or(ErrorKind::Unsupported)?;
        let value = match rest.chars().next() {
            Some('"') | Some('\'') => Value::String(quoted(&rest)?.0),
            Some('[') | Some('{') => return Err(ErrorKind::Unsupported),
            _ => typed(&rest),
        };
        entries.push((key, value));
    }
    Ok(Value::Map(entries))
}

fn flow_inner(text: &str, open: char, close: char) -> Result<&str, ErrorKind> {
    let body = strip_comment(text);
    body.strip_prefix(open)
        .and_then(|b| b.strip_suffix(close))
        // 括号没配上（后面也没有能接上来的行）。
        .ok_or(ErrorKind::Unsupported)
}

/// 按逗号切开流式写法的各项，引号里的逗号不算。返回读好的标量和原文。
fn flow_items(inner: &str) -> Result<Vec<(Value, String)>, ErrorKind> {
    let mut items = Vec::new();
    let mut rest = inner.trim();
    while !rest.is_empty() {
        if rest.starts_with('[') || rest.starts_with('{') {
            return Err(ErrorKind::Unsupported);
        }
        let (value, raw, tail) = if rest.starts_with('"') || rest.starts_with('\'') {
            let (text, tail) = quoted(rest)?;
            let raw = &rest[..rest.len() - tail.len()];
            // 引号后面可能还有 `: value`（流式映射的带引号的键）。
            let end = tail.find(',').unwrap_or(tail.len());
            (
                Value::String(text),
                format!("{raw}{}", &tail[..end]),
                &tail[end..],
            )
        } else {
            let end = rest.find(',').unwrap_or(rest.len());
            let raw = rest[..end].trim();
            (typed(raw), raw.to_string(), &rest[end..])
        };
        items.push((value, raw));
        rest = tail.trim_start();
        match rest.strip_prefix(',') {
            Some(next) => rest = next.trim_start(),
            None if rest.is_empty() => {}
            None => return Err(ErrorKind::Unexpected),
        }
    }
    Ok(items)
}

/// 普通标量后面的 ` # 注释` 去掉。`#` 前面必须有空白，`C#` 不是注释。
fn strip_comment(text: &str) -> &str {
    let mut prev_space = true;
    for (i, c) in text.char_indices() {
        if c == '#' && prev_space {
            return text[..i].trim_end();
        }
        prev_space = c == ' ';
    }
    text.trim_end()
}

fn is_blank_or_comment(tail: &str) -> bool {
    let tail = tail.trim_start();
    tail.is_empty() || tail.starts_with('#')
}

/// 单行普通标量按 YAML 核心模式定类型。
fn typed(text: &str) -> Value {
    match text {
        "" | "~" | "null" | "Null" | "NULL" => Value::Null,
        "true" | "True" | "TRUE" => Value::Bool(true),
        "false" | "False" | "FALSE" => Value::Bool(false),
        _ => {
            let digits = text.strip_prefix(['-', '+']).unwrap_or(text);
            let numeric = !digits.is_empty()
                && digits.chars().all(|c| c.is_ascii_digit() || c == '.')
                && digits.chars().filter(|&c| c == '.').count() <= 1
                && digits.chars().any(|c| c.is_ascii_digit());
            if numeric {
                Value::Number(text.to_string())
            } else {
                Value::String(text.to_string())
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn fm(text: &str) -> Frontmatter {
        parse(text).unwrap_or_else(|e| panic!("{e}\n---\n{text}"))
    }

    #[test]
    fn split_finds_the_block_and_the_body() {
        let (front, body) = split("---\nname: a\n---\n# Title\nbody\n");
        assert_eq!(front, Some("name: a\n"));
        assert_eq!(body, "# Title\nbody\n");
    }

    #[test]
    fn a_file_without_frontmatter_is_all_body() {
        assert_eq!(split("# just a command\n"), (None, "# just a command\n"));
        // 开了头没收尾：不猜，整个当正文。
        assert_eq!(split("---\nname: a\n"), (None, "---\nname: a\n"));
        assert_eq!(split(""), (None, ""));
    }

    #[test]
    fn a_bom_and_crlf_do_not_hide_the_frontmatter() {
        let (front, body) = split("\u{feff}---\r\nname: a\r\n---\r\nbody\r\n");
        assert_eq!(fm(front.unwrap()).text("name"), Some("a"));
        assert_eq!(body, "body\r\n");
    }

    #[test]
    fn the_single_line_fields_everyone_writes() {
        let f = fm(
            "name: pdf\ndescription: Fill PDF forms. Use when the user has a PDF.\nlicense: Proprietary. LICENSE.txt has complete terms\n",
        );
        assert_eq!(f.text("name"), Some("pdf"));
        assert_eq!(
            f.text("description"),
            Some("Fill PDF forms. Use when the user has a PDF.")
        );
        assert_eq!(
            f.entries()
                .iter()
                .map(|(k, _)| k.as_str())
                .collect::<Vec<_>>(),
            ["name", "description", "license"]
        );
    }

    /// 这就是 gld 原来读成一个 `>` 的那种写法。
    #[test]
    fn a_folded_description_is_one_paragraph() {
        let f = fm(
            "name: deploy\ndescription: >\n  Deploy the service.\n  Use after tests pass.\n\n  Never on Fridays.\nuser-invocable: false\n",
        );
        assert_eq!(
            f.text("description"),
            Some("Deploy the service. Use after tests pass.\nNever on Fridays.")
        );
        assert_eq!(f.flag("user-invocable"), Some(false));
    }

    #[test]
    fn a_literal_block_keeps_its_lines_and_chomping_is_honoured() {
        let f = fm("a: |\n  one\n  two\n\nb: |-\n  one\nc: |+\n  one\n\nd: x\n");
        assert_eq!(f.get("a"), Some(&Value::String("one\ntwo\n".into())));
        assert_eq!(f.get("b"), Some(&Value::String("one".into())));
        assert_eq!(f.get("c"), Some(&Value::String("one\n\n".into())));
        assert_eq!(f.text("d"), Some("x"));
    }

    #[test]
    fn a_plain_value_may_run_on_to_indented_lines() {
        let f =
            fm("description: Review a pull request\n  against the team checklist.\nname: review\n");
        assert_eq!(
            f.text("description"),
            Some("Review a pull request against the team checklist.")
        );
        assert_eq!(f.text("name"), Some("review"));
    }

    #[test]
    fn quotes_escapes_and_colons_inside_values() {
        let f = fm(
            "a: \"say \\\"hi\\\"\\n\"\nb: 'it''s'\nc: Use when: the user asks\nd: see http://example.com/x\ne: C# projects # trailing comment\n",
        );
        assert_eq!(f.get("a"), Some(&Value::String("say \"hi\"\n".into())));
        assert_eq!(f.text("b"), Some("it's"));
        assert_eq!(f.text("c"), Some("Use when: the user asks"));
        assert_eq!(f.text("d"), Some("see http://example.com/x"));
        assert_eq!(f.text("e"), Some("C# projects"));
    }

    #[test]
    fn words_come_from_a_list_or_from_one_string() {
        let f = fm(
            "arguments: [issue, branch]\nallowed-tools: Read, Grep Glob\ntools:\n  - Bash(git *)\n  - Read\nother:\n- a\n- b\n",
        );
        assert_eq!(f.words("arguments"), ["issue", "branch"]);
        assert_eq!(f.words("allowed-tools"), ["Read", "Grep", "Glob"]);
        assert_eq!(f.words("tools"), ["Bash(git *)", "Read"]);
        assert_eq!(f.words("other"), ["a", "b"]);
        assert!(f.words("missing").is_empty());
    }

    #[test]
    fn keys_match_across_dash_underscore_and_case() {
        let f = fm("when_to_use: after tests\nDisable-Model-Invocation: true\n");
        assert_eq!(f.text("when-to-use"), Some("after tests"));
        assert_eq!(f.flag("disable-model-invocation"), Some(true));
    }

    #[test]
    fn scalars_are_typed_but_numbers_keep_their_spelling() {
        let f = fm("a: true\nb: \"true\"\nc: 1.10\nd: ~\ne:\nf: 1.2.3\n");
        assert_eq!(f.get("a"), Some(&Value::Bool(true)));
        assert_eq!(f.flag("b"), Some(true));
        assert_eq!(f.get("c"), Some(&Value::Number("1.10".into())));
        assert_eq!(f.get("d"), Some(&Value::Null));
        assert_eq!(f.get("e"), Some(&Value::Null));
        assert_eq!(f.get("f"), Some(&Value::String("1.2.3".into())));
    }

    #[test]
    fn metadata_is_a_map_and_flow_maps_work_on_one_line() {
        let f = fm(
            "metadata:\n  author: me\n  version: \"1.0\"\nextra: {a: 1, \"b c\": x}\nempty: {}\n",
        );
        assert_eq!(
            f.get("metadata"),
            Some(&Value::Map(vec![
                ("author".into(), Value::String("me".into())),
                ("version".into(), Value::String("1.0".into())),
            ]))
        );
        assert_eq!(
            f.get("extra"),
            Some(&Value::Map(vec![
                ("a".into(), Value::Number("1".into())),
                ("b c".into(), Value::String("x".into())),
            ]))
        );
        assert_eq!(f.get("empty"), Some(&Value::Map(vec![])));
    }

    /// 没有人用 hooks 的值，但读不过去的话，这个 skill 连名字都拿不到。
    #[test]
    fn a_hooks_block_is_read_past_not_choked_on() {
        let f = fm(
            "name: guarded\nhooks:\n  PreToolUse:\n    - matcher: \"Bash\"\n      hooks:\n        - type: command\n          command: \"./scripts/check.sh\"\n    - matcher: Edit\ndescription: after the hooks\n",
        );
        assert_eq!(f.text("name"), Some("guarded"));
        assert_eq!(f.text("description"), Some("after the hooks"));
        let Some(Value::Map(hooks)) = f.get("hooks") else {
            panic!("hooks is a map")
        };
        let Value::List(matchers) = &hooks[0].1 else {
            panic!("PreToolUse is a list")
        };
        assert_eq!(matchers.len(), 2);
        let Value::Map(first) = &matchers[0] else {
            panic!("a list item is a map")
        };
        assert_eq!(first[0], ("matcher".into(), Value::String("Bash".into())));
        assert!(matches!(&first[1].1, Value::List(inner) if inner.len() == 1));
    }

    /// 两种都来自 Anthropic 官方插件（plugin-dev 的 create-plugin 命令、
    /// math-olympiad 的 SKILL.md）：格式化工具折出来的写法。
    #[test]
    fn quoted_strings_and_flow_lists_may_span_lines() {
        let f = fm(
            "name: math\ndescription:\n  \"Solve competition problems with adversarial\n  verification. Activates on 'IMO', or\n\n  'Putnam'.\"\nallowed-tools:\n  [\n    \"Read\",\n    \"Bash(git *)\",\n    Grep,\n  ]\nafter: x\n",
        );
        assert_eq!(
            f.text("description"),
            Some(
                "Solve competition problems with adversarial verification. Activates on 'IMO', or 'Putnam'."
            )
        );
        assert_eq!(f.words("allowed-tools"), ["Read", "Bash(git *)", "Grep"]);
        assert_eq!(f.text("after"), Some("x"));
    }

    #[test]
    fn comments_and_blank_lines_are_skipped() {
        let f = fm("# a comment\n\nname: a   # why\n\n  # indented comment\ndescription: b\n");
        assert_eq!(f.text("name"), Some("a"));
        assert_eq!(f.text("description"), Some("b"));
    }

    #[test]
    fn what_is_not_supported_says_which_line() {
        for (text, line, kind) in [
            ("name: a\nbase: &x 1\n", 2, ErrorKind::Unsupported),
            ("name: a\nref: *x\n", 2, ErrorKind::Unsupported),
            ("list: [a,\nname: b\n", 1, ErrorKind::Unsupported),
            ("list: [a, [b]]\n", 1, ErrorKind::Unsupported),
            ("a: \"open\n", 1, ErrorKind::BadQuote),
            ("name: a\n\tb: c\n", 2, ErrorKind::TabIndent),
            ("name: a\njust some words\n", 2, ErrorKind::Unexpected),
        ] {
            assert_eq!(parse(text), Err(ParseError { line, kind }), "{text:?}");
        }
    }

    /// 严格的 YAML 在这里会报错（标量的续行里不许出现 `key: value`）。这里放过
    /// 它：作者把描述折到第二行、里面正好有个冒号，是常见写法，不该让整个 skill
    /// 读不出来。
    #[test]
    fn a_continuation_line_may_contain_a_colon() {
        let f = fm("description: Review code.\n  Use when: the user asks for a review.\nname: r\n");
        assert_eq!(
            f.text("description"),
            Some("Review code. Use when: the user asks for a review.")
        );
        assert_eq!(f.text("name"), Some("r"));
    }

    #[test]
    fn a_comment_after_the_colon_does_not_hide_the_nested_value() {
        let f = fm("metadata: # who wrote it\n  author: me\n");
        assert_eq!(
            f.get("metadata"),
            Some(&Value::Map(vec![(
                "author".into(),
                Value::String("me".into())
            )]))
        );
    }

    #[test]
    fn nesting_has_a_floor_under_it() {
        let mut text = String::new();
        for depth in 0..40 {
            text.push_str(&format!("{}k:\n", "  ".repeat(depth)));
        }
        assert_eq!(parse(&text).unwrap_err().kind, ErrorKind::TooDeep);
    }

    #[test]
    fn an_empty_frontmatter_is_empty() {
        assert!(fm("").entries().is_empty());
        assert!(fm("\n# only a comment\n").entries().is_empty());
    }
}
