//! 块状写法的读取器：键值、缩进嵌套、`- ` 列表、块标量、引号串。
//!
//! 同一套代码跑两种模式，区别只在几处 YAML 不许、而作者常写的地方（见
//! [`Mode`]）。为什么要分：宿主先严格读，读不了才换一种读法（见上一层的
//! `parse`），我们要知道自己现在读的这份，宿主那边算不算读成功了。

use super::{ErrorKind, MAX_DEPTH, ParseError, Value, flow};

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(super) enum Mode {
    /// 和 YAML 一样，下面这些都算读不了。
    Strict,
    /// 放过它们：
    ///
    /// - 普通标量里的 `: `（`description: Use when: the user asks`），包括续行里的；
    /// - 普通标量续行中间的注释行，跳过它接着读；
    /// - 比内容缩进得浅、但比键深的块标量行；
    /// - 以 `` ` `` `@` `%` 开头的值，当普通文字；
    /// - 后面还跟着别的文字的 `[…]` / `{…}`（`[draft] deploy`），当普通文字。
    Lenient,
}

/// 一组键值，和每个键在第几行（给 `duplicates` 报行号用）。
type Entries = (Vec<(String, Value)>, Vec<usize>);

struct Line {
    /// 在 frontmatter 里的行号，从 1 数。
    number: usize,
    indent: usize,
    /// 去掉缩进和行尾空白之后的内容。空行是空串。
    text: String,
    /// 原样的一行。只有块标量用它：`|` 里的行尾空白是内容。
    raw: String,
}

pub(super) struct Parser {
    lines: Vec<Line>,
    at: usize,
    mode: Mode,
}

impl Parser {
    pub(super) fn new(text: &str, mode: Mode) -> Result<Self, ParseError> {
        let mut lines = Vec::new();
        for (i, raw) in text.lines().enumerate() {
            let body = raw.trim_end();
            let indent = body.len() - body.trim_start_matches(' ').len();
            // 缩进里的 tab 不行；只有空白的行和注释行前面有 tab 没关系（YAML 如此，
            // 宿主也放行）。
            let content = body.trim_start_matches([' ', '\t']);
            if body[indent..].starts_with('\t') && !content.is_empty() && !content.starts_with('#')
            {
                return Err(ParseError {
                    line: i + 1,
                    kind: ErrorKind::TabIndent,
                });
            }
            let text = if content.starts_with('#') {
                content
            } else {
                &body[indent..]
            };
            lines.push(Line {
                number: i + 1,
                indent,
                text: text.to_string(),
                raw: raw.to_string(),
            });
        }
        Ok(Parser { lines, at: 0, mode })
    }

    /// 整份 frontmatter：顶层的键值，以及每个键在第几行。
    pub(super) fn document(mut self) -> Result<Entries, ParseError> {
        let parsed = match self.peek() {
            None => (Vec::new(), Vec::new()),
            Some(at) => {
                let indent = self.lines[at].indent;
                self.map(indent, 0)?
            }
        };
        if let Some(at) = self.peek() {
            return Err(self.error(at, ErrorKind::Unexpected));
        }
        Ok(parsed)
    }

    fn strict(&self) -> bool {
        self.mode == Mode::Strict
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

    /// 缩进为 `indent` 的一组 `key: value`，连同每个键的行号。
    fn map(&mut self, indent: usize, depth: usize) -> Result<Entries, ParseError> {
        let mut entries = Vec::new();
        let mut numbers = Vec::new();
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
                self.nested(indent, depth, true)?
            } else {
                self.inline(&rest, at, indent)?
            };
            entries.push((key, value));
            numbers.push(self.lines[at].number);
        }
        Ok((entries, numbers))
    }

    /// `key:` 或 `-` 后面什么都没写：值在下面几行，或者就是空。
    ///
    /// `list_may_align`：键下面的列表允许和键对齐（`a:\n- x`），`-` 下面的列表
    /// 不行——和它对齐的 `-` 是它的兄弟项，不是它的内容。
    fn nested(
        &mut self,
        owner_indent: usize,
        depth: usize,
        list_may_align: bool,
    ) -> Result<Value, ParseError> {
        let Some(at) = self.peek() else {
            return Ok(Value::Null);
        };
        if depth >= MAX_DEPTH {
            return Err(self.error(at, ErrorKind::TooDeep));
        }
        let line = &self.lines[at];
        let is_item = line.text == "-" || line.text.starts_with("- ");
        let deep_enough =
            line.indent > owner_indent || (list_may_align && line.indent == owner_indent);
        if is_item && deep_enough {
            let indent = line.indent;
            return Ok(Value::List(self.list(indent, depth + 1)?));
        }
        if line.indent <= owner_indent {
            return Ok(Value::Null);
        }
        let indent = line.indent;
        if split_key(&line.text)
            .map_err(|kind| self.error(at, kind))?
            .is_some()
        {
            return Ok(Value::Map(self.map(indent, depth + 1)?.0));
        }
        // 值从下一行才开始的普通标量。
        self.at = at + 1;
        let first = self.lines[at].text.clone();
        self.inline(&first, at, owner_indent)
    }

    fn list(&mut self, indent: usize, depth: usize) -> Result<Vec<Value>, ParseError> {
        let mut items = Vec::new();
        while let Some(at) = self.peek() {
            let line = &self.lines[at];
            if line.indent != indent || !(line.text == "-" || line.text.starts_with("- ")) {
                break;
            }
            let rest = line.text[1..].trim_start().to_string();
            if is_blank_or_comment(&rest) {
                self.at = at + 1;
                items.push(self.nested(indent, depth, false)?);
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
                items.push(Value::Map(self.map(inner, depth + 1)?.0));
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
            Some(q @ ('"' | '\'')) => {
                let joined = self
                    .until(rest, indent, Closer::quote(q))
                    .unwrap_or_else(|| rest.to_string());
                let (text, tail) = quoted(&joined).map_err(fail)?;
                if !is_blank_or_comment(tail) {
                    return Err(fail(ErrorKind::Unexpected));
                }
                Ok(Value::String(text))
            }
            Some(open @ ('[' | '{')) => {
                let start = self.at;
                let joined = self.until(rest, indent, Closer::bracket(open));
                let result = match &joined {
                    Some(text) => flow::whole(text, self.mode),
                    // 括号没配上（后面也没有能接上来的行）。
                    None => Err(ErrorKind::Unsupported),
                };
                match result {
                    Ok(value) => Ok(value),
                    Err(kind) if self.strict() => Err(fail(kind)),
                    // `argument-hint: [pr-number] [priority]` 是官方文档里的写法，
                    // 不是一个列表，是一句提示。
                    Err(_) => {
                        self.at = start;
                        self.plain(rest, at, indent)
                    }
                }
            }
            Some('&') | Some('*') | Some('!') => Err(fail(ErrorKind::Unsupported)),
            Some('%') | Some('@') | Some('`') if self.strict() => Err(fail(ErrorKind::Unsupported)),
            _ => self.plain(rest, at, indent),
        }
    }

    /// 一个值在这一行没写完（引号没闭合、括号没配上）：把缩进更深的后续行接上来，
    /// 直到收口。YAML 对跨行的引号串和流式集合就是这么折行的：行与行之间一个
    /// 空格，引号串里的空行变成换行。接不上（后面没有更深的行了）就是 `None`。
    ///
    /// 只扫一遍：0.1.0 每接一行都从头再判断一次收口，一个 1 MiB、引号一直不闭合
    /// 的 frontmatter 要几十秒。
    fn until(&mut self, first: &str, indent: usize, mut closer: Closer) -> Option<String> {
        let mut text = first.to_string();
        closer.feed(first);
        let mut at = self.at;
        let mut blanks = 0;
        while !closer.done() && at < self.lines.len() {
            let line = &self.lines[at];
            if line.text.is_empty() {
                blanks += 1;
            } else {
                if line.indent <= indent {
                    break;
                }
                let sep = if closer.is_quote() && blanks > 0 {
                    "\n".repeat(blanks)
                } else {
                    " ".to_string()
                };
                closer.feed(&sep);
                closer.feed(&line.text);
                text.push_str(&sep);
                text.push_str(&line.text);
                blanks = 0;
            }
            at += 1;
        }
        if closer.done() {
            self.at = at;
            return Some(text);
        }
        None
    }

    /// 普通标量，连同它缩进更深的续行。续行之间按 YAML 的规矩折成空格，空行
    /// 折成换行。
    fn plain(&mut self, first: &str, at: usize, indent: usize) -> Result<Value, ParseError> {
        let head = strip_comment(first);
        if self.strict() {
            check_plain(head, true).map_err(|kind| self.error(at, kind))?;
        }
        let mut text = head.to_string();
        // 这一行上已经有注释了：严格地说标量到这里就结束了。
        let mut ended = head.len() != first.trim_end().len();
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
            if line.text.starts_with('#') {
                // 续行中间的注释行。YAML 里它结束这个标量；宽松读时跳过它。
                ended = true;
                self.at += 1;
                continue;
            }
            if ended && self.strict() {
                return Err(self.error(self.at, ErrorKind::Unexpected));
            }
            let piece = strip_comment(&line.text);
            if self.strict() {
                check_plain(piece, false).map_err(|kind| self.error(self.at, kind))?;
            }
            ended = piece.len() != line.text.len();
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
            return Ok(Value::String(text));
        }
        Ok(typed(&text))
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
        let mut rows: Vec<(usize, usize, String)> = Vec::new();
        let mut base = explicit.map(|n| indent + n);
        while self.at < self.lines.len() {
            let line = &self.lines[self.at];
            if !line.text.is_empty() && line.indent <= indent {
                break;
            }
            // 内容的缩进由第一行有字的定。比它浅的 `#` 行不是内容，是块后面的
            // 注释，块到这里结束。
            if !line.text.is_empty() {
                match base {
                    None => base = Some(line.indent),
                    Some(b) if line.indent < b && line.text.starts_with('#') => break,
                    Some(_) => {}
                }
            }
            rows.push((self.at, line.indent, line.text.clone()));
            self.at += 1;
        }
        let base = base.unwrap_or(indent + 1);
        // 第一行有字的之前那些只有空白的行是空行，不是内容；它们比内容缩进得还深，
        // YAML 不许（看不出内容从哪一列开始）。
        let first = rows.iter().position(|(_, _, t)| !t.is_empty());
        if self.strict() {
            let too_deep = |(at, _, _): &&(usize, usize, String)| {
                let raw = &self.lines[*at].raw;
                raw.len() - raw.trim_start_matches(' ').len() > base
            };
            let leading = &rows[..first.unwrap_or(0)];
            let shallow = rows.iter().find(|(_, i, t)| !t.is_empty() && *i < base);
            if let Some((row, _, _)) = leading.iter().find(too_deep).or(shallow) {
                return Err(self.error(*row, ErrorKind::Unexpected));
            }
        }
        let rows: Vec<String> = rows
            .into_iter()
            .enumerate()
            .map(|(n, (at, i, t))| {
                // 内容开始之后，缩进够的行取原样：行尾空白和只有空白的行都是内容
                // （宿主就是这么读的）。
                let raw = &self.lines[at].raw;
                let spaces = raw.len() - raw.trim_start_matches(' ').len();
                if first.is_some_and(|f| n >= f) && spaces >= base {
                    raw[base..].to_string()
                } else if t.is_empty() {
                    String::new()
                } else {
                    format!("{}{t}", " ".repeat(i.saturating_sub(base)))
                }
            })
            .collect();
        // 一行都没有：什么方式收尾都是空串，`|+` 也不例外。
        if rows.is_empty() {
            return Ok(Value::String(String::new()));
        }
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

/// 严格模式下普通标量不许有的东西。`head`：这是不是写在键同一行上的那一段——
/// 开头的那些字符只在标量开头算数。
fn check_plain(text: &str, head: bool) -> Result<(), ErrorKind> {
    // `a: b: c`、`a: b:`：YAML 把这里的冒号读成又一层映射，不允许写在一行上。
    let mut chars = text.chars().peekable();
    while let Some(c) = chars.next() {
        if c == ':' && matches!(chars.peek(), None | Some(' ' | '\t')) {
            return Err(ErrorKind::Unexpected);
        }
    }
    let indicator = ["- ", "? ", ": "].iter().any(|p| text.starts_with(p));
    if head && (indicator || text.starts_with([',', ']', '}'])) {
        return Err(ErrorKind::Unexpected);
    }
    Ok(())
}

/// `>` 的折行：相邻的普通行用空格接起来，空行变成换行，缩进更深的行和它前后
/// 的换行原样保留，开头的空行也保留。
fn fold(rows: &[String]) -> String {
    let mut out = String::new();
    // 上一个有内容的行是不是缩进更深的那种；`None` 是还没有内容。
    let mut prev_more: Option<bool> = None;
    let mut blanks = 0;
    for row in rows {
        if row.is_empty() {
            blanks += 1;
            continue;
        }
        let more = row.starts_with(' ');
        match prev_more {
            None => out.push_str(&"\n".repeat(blanks)),
            Some(false) if !more => {
                if blanks == 0 {
                    out.push(' ');
                } else {
                    out.push_str(&"\n".repeat(blanks));
                }
            }
            Some(_) => out.push_str(&"\n".repeat(blanks + 1)),
        }
        out.push_str(row);
        prev_more = Some(more);
        blanks = 0;
    }
    // 末尾的空行留给调用方按 `-` / `+` 处理，个数和 `|` 用 join 接出来的一样：
    // 有内容时每个空行一个换行，全是空行时少一个（行与行之间才有换行）。
    let trailing = if prev_more.is_some() {
        blanks
    } else {
        blanks.saturating_sub(1)
    };
    out.push_str(&"\n".repeat(trailing));
    out
}

/// 一段一段喂进来的文字里，开头的那个引号或括号什么时候收口。
struct Closer {
    /// `Some(c)`：在找配对的括号，`c` 是开括号；`None`：整个就是一个引号串。
    bracket: Option<char>,
    depth: usize,
    quote: Option<char>,
    escaped: bool,
    /// 单引号串里刚看到一个 `'`：下一个字符还是 `'` 就是转义，不然就收口了。
    single_pending: bool,
    /// 引号外最后一个非空白字符。流式集合里引号只在标量开头才算数：`[it's]`
    /// 里的撇号是文字，当成引号的话括号就永远配不上了。
    last: char,
    started: bool,
    done: bool,
}

impl Closer {
    fn quote(q: char) -> Self {
        Closer::new(None, Some(q))
    }

    fn bracket(open: char) -> Self {
        Closer::new(Some(open), None)
    }

    fn new(bracket: Option<char>, quote: Option<char>) -> Self {
        Closer {
            bracket,
            depth: 0,
            quote,
            escaped: false,
            single_pending: false,
            last: ' ',
            started: false,
            done: false,
        }
    }

    fn is_quote(&self) -> bool {
        self.bracket.is_none()
    }

    fn done(&self) -> bool {
        self.done || (self.is_quote() && self.single_pending)
    }

    fn feed(&mut self, text: &str) {
        for c in text.chars() {
            if self.done {
                return;
            }
            if !self.started {
                // 第一个字符是开头的引号或括号本身。
                self.started = true;
                if let Some(open) = self.bracket {
                    self.depth = 1;
                    self.last = open;
                }
                continue;
            }
            if self.single_pending {
                self.single_pending = false;
                if c == '\'' {
                    continue;
                }
                // 前一个 `'` 是收口。
                self.quote = None;
                if self.is_quote() {
                    self.done = true;
                    return;
                }
            }
            match self.quote {
                Some('"') if self.escaped => self.escaped = false,
                Some('"') if c == '\\' => self.escaped = true,
                Some('"') if c == '"' => {
                    self.quote = None;
                    if self.is_quote() {
                        self.done = true;
                    }
                }
                Some('\'') if c == '\'' => self.single_pending = true,
                Some(_) => {}
                None => match (self.bracket, c) {
                    (_, '"' | '\'') if matches!(self.last, '[' | '{' | ',' | ':') => {
                        self.quote = Some(c)
                    }
                    (Some('['), '[') | (Some('{'), '{') => self.depth += 1,
                    (Some('['), ']') | (Some('{'), '}') => {
                        self.depth -= 1;
                        if self.depth == 0 {
                            self.done = true;
                        }
                    }
                    _ => {}
                },
            }
            if self.quote.is_none() && !c.is_whitespace() {
                self.last = c;
            }
        }
    }
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
            .filter(|rest| rest.is_empty() || rest.starts_with([' ', '\t']))
            .map(|rest| (key, rest.trim().to_string())));
    }
    if text.starts_with("- ") || text == "-" || text.starts_with('[') || text.starts_with('{') {
        return Ok(None);
    }
    // 第一个「后面是空白或行尾」的冒号。`http://x` 里的冒号不算。
    let mut from = 0;
    while let Some(off) = text[from..].find(':') {
        let at = from + off;
        let rest = &text[at + 1..];
        if rest.is_empty() || rest.starts_with([' ', '\t']) {
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
pub(super) fn quoted(text: &str) -> Result<(String, &str), ErrorKind> {
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
                // YAML 1.2 双引号串的全部转义（第 5.7 节）。
                let plain = match esc {
                    '0' => Some('\0'),
                    'a' => Some('\u{7}'),
                    'b' => Some('\u{8}'),
                    't' | '\t' => Some('\t'),
                    'n' => Some('\n'),
                    'v' => Some('\u{b}'),
                    'f' => Some('\u{c}'),
                    'r' => Some('\r'),
                    'e' => Some('\u{1b}'),
                    ' ' | '"' | '/' | '\\' => Some(esc),
                    'N' => Some('\u{85}'),
                    '_' => Some('\u{a0}'),
                    'L' => Some('\u{2028}'),
                    'P' => Some('\u{2029}'),
                    _ => None,
                };
                if let Some(p) = plain {
                    out.push(p);
                    continue;
                }
                let len = match esc {
                    'x' => 2,
                    'u' => 4,
                    'U' => 8,
                    _ => return Err(ErrorKind::BadQuote),
                };
                let hex: String = chars.by_ref().take(len).map(|(_, h)| h).collect();
                // from_str_radix 认一个开头的 `+`，`\x+1` 不能因此算合法。
                if hex.len() != len || !hex.bytes().all(|b| b.is_ascii_hexdigit()) {
                    return Err(ErrorKind::BadQuote);
                }
                let code = u32::from_str_radix(&hex, 16).map_err(|_| ErrorKind::BadQuote)?;
                out.push(char::from_u32(code).ok_or(ErrorKind::BadQuote)?);
            }
            _ => out.push(c),
        }
    }
    Err(ErrorKind::BadQuote)
}

/// 普通标量后面的 ` # 注释` 去掉。`#` 前面必须有空白，`C#` 不是注释。
pub(super) fn strip_comment(text: &str) -> &str {
    let mut prev_space = true;
    for (i, c) in text.char_indices() {
        if c == '#' && prev_space {
            return text[..i].trim_end();
        }
        prev_space = c == ' ' || c == '\t';
    }
    text.trim_end()
}

pub(super) fn is_blank_or_comment(tail: &str) -> bool {
    let tail = tail.trim_start();
    tail.is_empty() || tail.starts_with('#')
}

/// 单行普通标量按 YAML 1.2 核心模式定类型（和宿主的 Bun.YAML 一样）。
pub(super) fn typed(text: &str) -> Value {
    match text {
        "" | "~" | "null" | "Null" | "NULL" => Value::Null,
        "true" | "True" | "TRUE" => Value::Bool(true),
        "false" | "False" | "FALSE" => Value::Bool(false),
        _ if is_number(text) => Value::Number(text.to_string()),
        _ => Value::String(text.to_string()),
    }
}

/// YAML 1.2 核心模式的数：十进制整数和小数（可带符号、指数），`0o` 八进制，
/// `0x` 十六进制，`.inf` / `.nan`。`1_000`、`0b1` 不算——宿主也不算。
fn is_number(text: &str) -> bool {
    let digits = |s: &str| !s.is_empty() && s.bytes().all(|b| b.is_ascii_digit());
    if let Some(hex) = text.strip_prefix("0x") {
        return !hex.is_empty() && hex.bytes().all(|b| b.is_ascii_hexdigit());
    }
    if let Some(oct) = text.strip_prefix("0o") {
        return !oct.is_empty() && oct.bytes().all(|b| (b'0'..=b'7').contains(&b));
    }
    if matches!(text, ".nan" | ".NaN" | ".NAN") {
        return true;
    }
    let unsigned = text.strip_prefix(['-', '+']).unwrap_or(text);
    if matches!(unsigned, ".inf" | ".Inf" | ".INF") {
        return true;
    }
    let (mantissa, exponent) = match unsigned.find(['e', 'E']) {
        Some(i) => (&unsigned[..i], Some(&unsigned[i + 1..])),
        None => (unsigned, None),
    };
    let mantissa_ok = match mantissa.split_once('.') {
        None => digits(mantissa),
        // `5.` 和 `.5` 都算，单独一个 `.` 不算。
        Some((int, frac)) => {
            (digits(int) && (frac.is_empty() || digits(frac))) || (int.is_empty() && digits(frac))
        }
    };
    let exponent_ok = exponent.is_none_or(|e| digits(e.strip_prefix(['-', '+']).unwrap_or(e)));
    mantissa_ok && exponent_ok
}

/// 数的值，布尔字段要用（宿主把 `1`、`1.0` 都当 true）。
pub(super) fn number_value(text: &str) -> Option<f64> {
    if let Some(hex) = text.strip_prefix("0x") {
        return u64::from_str_radix(hex, 16).ok().map(|n| n as f64);
    }
    if let Some(oct) = text.strip_prefix("0o") {
        return u64::from_str_radix(oct, 8).ok().map(|n| n as f64);
    }
    let unsigned = text.strip_prefix(['-', '+']).unwrap_or(text);
    if matches!(
        unsigned,
        ".inf" | ".Inf" | ".INF" | ".nan" | ".NaN" | ".NAN"
    ) {
        return None;
    }
    text.parse().ok()
}
