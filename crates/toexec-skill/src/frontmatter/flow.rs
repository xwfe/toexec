//! 流式写法：`[a, "b", [c]]`、`{k: v, only-key}`。
//!
//! 跨行的已经由调用方用空格接成一行交进来。可以嵌套（宿主的 Bun.YAML 读得了，
//! 0.1.0 这里不认，整个 skill 因此读不出来），深度和块状写法共用一个上限。

use super::parser::{Mode, is_blank_or_comment, quoted, typed};
use super::{ErrorKind, MAX_DEPTH, Value};

/// 读 `text` 开头的一个流式集合，返回它和后面剩下的部分。
pub(super) fn parse(text: &str, mode: Mode) -> Result<(Value, &str), ErrorKind> {
    let mut flow = Flow {
        text,
        at: 0,
        mode,
        depth: 0,
    };
    let value = flow.value()?;
    Ok((value, &text[flow.at..]))
}

/// `text` 整个就是一个流式集合（后面至多跟一个注释）。
pub(super) fn whole(text: &str, mode: Mode) -> Result<Value, ErrorKind> {
    let (value, rest) = parse(text, mode)?;
    if is_blank_or_comment(rest) {
        Ok(value)
    } else {
        Err(ErrorKind::Unexpected)
    }
}

struct Flow<'a> {
    text: &'a str,
    at: usize,
    mode: Mode,
    depth: usize,
}

impl<'a> Flow<'a> {
    fn rest(&self) -> &'a str {
        &self.text[self.at..]
    }

    fn peek(&self) -> Option<char> {
        self.rest().chars().next()
    }

    fn skip_space(&mut self) {
        let rest = self.rest();
        self.at += rest.len() - rest.trim_start_matches([' ', '\t']).len();
    }

    fn eat(&mut self, c: char) -> bool {
        if self.peek() == Some(c) {
            self.at += c.len_utf8();
            true
        } else {
            false
        }
    }

    fn value(&mut self) -> Result<Value, ErrorKind> {
        self.skip_space();
        match self.peek() {
            Some('[') => self.nest(|f| f.seq()),
            Some('{') => self.nest(|f| f.map()),
            Some('"') | Some('\'') => {
                let (text, tail) = quoted(self.rest())?;
                self.at = self.text.len() - tail.len();
                Ok(Value::String(text))
            }
            _ => Ok(typed(self.plain()?)),
        }
    }

    fn nest(
        &mut self,
        f: impl FnOnce(&mut Self) -> Result<Value, ErrorKind>,
    ) -> Result<Value, ErrorKind> {
        if self.depth >= MAX_DEPTH {
            return Err(ErrorKind::TooDeep);
        }
        self.depth += 1;
        let value = f(self);
        self.depth -= 1;
        value
    }

    /// 流式里的普通标量：到 `,` `]` `}` 或者「后面是空白或括号的冒号」为止。
    fn plain(&mut self) -> Result<&'a str, ErrorKind> {
        let rest = self.rest();
        if let Some(c) = rest.chars().next()
            && matches!(c, '&' | '*' | '!' | '%' | '@' | '`')
            && self.mode == Mode::Strict
        {
            return Err(ErrorKind::Unsupported);
        }
        let mut end = rest.len();
        let mut chars = rest.char_indices().peekable();
        while let Some((i, c)) = chars.next() {
            let next = chars.peek().map(|&(_, n)| n);
            let is_colon = c == ':' && matches!(next, None | Some(' ' | '\t' | ',' | ']' | '}'));
            if matches!(c, ',' | ']' | '}') || is_colon {
                end = i;
                break;
            }
        }
        self.at += end;
        Ok(rest[..end].trim_end_matches([' ', '\t']))
    }

    fn seq(&mut self) -> Result<Value, ErrorKind> {
        self.eat('[');
        let mut items = Vec::new();
        loop {
            self.skip_space();
            if self.eat(']') {
                return Ok(Value::List(items));
            }
            if self.peek() == Some(',') {
                // `[,a]`、`[a,,b]`：YAML 不许，宽松读时当作一个空项。
                if self.mode == Mode::Strict {
                    return Err(ErrorKind::Unexpected);
                }
                self.at += 1;
                items.push(Value::Null);
                continue;
            }
            let item = self.value()?;
            self.skip_space();
            // `[a: b]` 是一个只有一对的映射。
            let item = if self.eat(':') {
                let value = self.pair_value()?;
                Value::Map(vec![(key_text(&item), value)])
            } else {
                item
            };
            items.push(item);
            self.after_item(']')?;
            if self.eat(']') {
                return Ok(Value::List(items));
            }
        }
    }

    fn map(&mut self) -> Result<Value, ErrorKind> {
        self.eat('{');
        let mut entries = Vec::new();
        loop {
            self.skip_space();
            if self.eat('}') {
                return Ok(Value::Map(entries));
            }
            let key = match self.peek() {
                Some('"') | Some('\'') => {
                    let (text, tail) = quoted(self.rest())?;
                    self.at = self.text.len() - tail.len();
                    text
                }
                Some('[') | Some('{') => return Err(ErrorKind::Unsupported),
                Some(',') => return Err(ErrorKind::Unexpected),
                _ => self.plain()?.to_string(),
            };
            self.skip_space();
            // `{a, b}` 里只写了键：值是 null。
            let value = if self.eat(':') {
                self.pair_value()?
            } else {
                Value::Null
            };
            entries.push((key, value));
            self.after_item('}')?;
            if self.eat('}') {
                return Ok(Value::Map(entries));
            }
        }
    }

    /// `k:` 后面的值，可以是空的（`{a: }`、`[a: ]`）。
    fn pair_value(&mut self) -> Result<Value, ErrorKind> {
        self.skip_space();
        match self.peek() {
            Some(',') | Some(']') | Some('}') | None => Ok(Value::Null),
            _ => self.value(),
        }
    }

    /// 一项读完之后只能是 `,` 或收尾的括号。
    fn after_item(&mut self, close: char) -> Result<(), ErrorKind> {
        self.skip_space();
        if self.eat(',') || self.peek() == Some(close) {
            Ok(())
        } else {
            Err(ErrorKind::Unexpected)
        }
    }
}

/// 流式映射的键要是文字。宿主那边是 JS 的对象键，也就是 `String(键)`。
fn key_text(value: &Value) -> String {
    match value {
        Value::Null => "null".into(),
        Value::Bool(b) => b.to_string(),
        Value::Number(s) | Value::String(s) => s.clone(),
        Value::List(_) | Value::Map(_) => String::new(),
    }
}
