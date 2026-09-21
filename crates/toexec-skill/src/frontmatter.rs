//! frontmatter：文件开头两条 `---` 之间的那一段 YAML。
//!
//! 为什么不用 YAML 库：两个产品都守着依赖数，而 skill 作者实际写的只是 YAML
//! 的一小块。这里读的是**块状**的那一半——键值、缩进嵌套、`- ` 列表、
//! `|` / `>` 块标量、引号（可以跨行）、`[a, b]` 和 `{k: v}`（可以跨行、嵌套）。
//! 锚点、别名、标签、多文档都不认。
//!
//! **读法照 Claude Code 的来**（2.1.278，静态读打包代码得出，差分测试在
//! `evidence/x08-skill-frontmatter/`）。宿主分两步：先用 Bun.YAML 严格读；读不了，
//! 把顶层那些带特殊字符的单行值加上双引号、行首 tab 换成空格，再读一次；还读不了，
//! 整个 frontmatter 当空的——skill 照样加载，名字、描述、开关全丢，只在调试日志里
//! 记一笔。这里前两步照做，结果和宿主一样；第三步不照做，改用宽松读法读出来，
//! 并在 [`Frontmatter::reading`] 里标明，由产品决定要不要告诉作者。
//!
//! 为什么第二步也要照做：它会改变**别的行**的读法。只要有一行不合规，所有带
//! `#` 的顶层值都会被加上引号——`description: PR #12 # 备注` 平时读成 `PR`，
//! 这时就读成 `PR #12 # 备注`。不照做的话，同一个文件在宿主和这里读出来不一样。
//!
//! 为什么连 `hooks:` 那种三层嵌套也要读得进来，哪怕没有人用它的值：读不进来
//! 的话，一个带 hooks 的 skill 会整个解析失败，连名字和描述都拿不到。要忽略
//! 一个字段，先得能跨过它。
//!
//! 之前 gld 自己的读法是逐行找 `key:` 前缀，`description: >` 这种多行写法
//! 读出来的描述就是一个 `>`。

use std::borrow::Cow;
use std::collections::HashMap;
use std::fmt;

mod flow;
mod parser;

use parser::{Mode, Parser, number_value};

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
    /// 认得出是什么、但这里不支持：锚点 `&x`、别名 `*x`、标签 `!x`。
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

/// 这份 frontmatter 在 Claude Code 那边读成了什么样。
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub enum Reading {
    /// 合规的 YAML：宿主读出来的和这里一样。
    #[default]
    Yaml,
    /// 不合规，但宿主给顶层的值加上引号之后读得了；这里照做了，结果一样。
    Requoted,
    /// 宿主读不了，会把整个 frontmatter 当成空的：skill 照样出现，但名字取目录名、
    /// 描述取正文第一行，`disable-model-invocation` 这类开关一律当没写。这里用
    /// 宽松读法读了出来（更接近作者本意，对开关也是更保守的一侧），值得告诉作者
    /// 修一修。
    Lenient,
}

/// 读好的 frontmatter。键的顺序和作者写的一致，重复的键都留着。
#[derive(Debug, Clone, Default, PartialEq)]
pub struct Frontmatter {
    entries: Vec<(String, Value)>,
    /// `entries` 里每个键在 frontmatter 的第几行。
    lines: Vec<usize>,
    reading: Reading,
}

impl Frontmatter {
    pub fn entries(&self) -> &[(String, Value)] {
        &self.entries
    }

    pub fn reading(&self) -> Reading {
        self.reading
    }

    /// 按键取值。
    ///
    /// 一个键写了不止一次时（包括只差大小写、`-` 和 `_` 的写法）：
    ///
    /// 1. 有和 `key` 一字不差的，取**最后**一个——宿主按原样查键，重复的键后写的
    ///    赢（Bun.YAML 的行为）。先写的赢会让 `disable-model-invocation` 写两遍时
    ///    两边读出相反的值。
    /// 2. 没有一字不差的，才取大小写、`-` / `_` 归一化后同名的最后一个。宿主不做
    ///    这一步（`Disable_Model_Invocation: true` 在它那里没有效果）；这里做，是
    ///    因为官方文档里 `when_to_use` 用下划线、`disable-model-invocation` 用连字符，
    ///    作者两种都会写，而对这两个开关，照作者的意思办是更保守的一侧。
    ///
    /// 用 [`Frontmatter::duplicates`] 找出这种写法告诉作者。
    pub fn get(&self, key: &str) -> Option<&Value> {
        let last = |matches: &dyn Fn(&str) -> bool| {
            self.entries
                .iter()
                .rev()
                .find(|(k, _)| matches(k))
                .map(|(_, v)| v)
        };
        last(&|k| k == key).or_else(|| last(&|k| same_key(k, key)))
    }

    /// 归一化之后重名的顶层键。每组按出现顺序给出原样的键和行号，没有就是空的。
    pub fn duplicates(&self) -> Vec<Vec<(&str, usize)>> {
        let mut groups: HashMap<String, Vec<usize>> = HashMap::new();
        for (i, (key, _)) in self.entries.iter().enumerate() {
            groups.entry(normal_key(key)).or_default().push(i);
        }
        let mut out: Vec<Vec<(&str, usize)>> = groups
            .into_values()
            .filter(|group| group.len() > 1)
            .map(|group| {
                group
                    .into_iter()
                    .map(|i| (self.entries[i].0.as_str(), self.lines[i]))
                    .collect()
            })
            .collect();
        out.sort_by_key(|group| group[0].1);
        out
    }

    /// 标量字段的文本，去掉首尾空白；空的当没有。
    pub fn text(&self, key: &str) -> Option<&str> {
        let text = self.get(key)?.as_text()?.trim();
        (!text.is_empty()).then_some(text)
    }

    /// 宿主拿来显示的文字：它对 `name`、`argument-hint`、`when_to_use` 做的是
    /// `String(值)`。和 [`Frontmatter::text`] 的区别在列表：官方文档的例子
    /// `argument-hint: [issue-number]` 在 YAML 里是个列表，宿主显示成
    /// `issue-number`（各项用逗号接起来）。布尔值是 `true` / `false`；映射没有。
    pub fn string(&self, key: &str) -> Option<String> {
        fn join(value: &Value) -> Option<Cow<'_, str>> {
            match value {
                Value::Null => Some(Cow::Borrowed("")),
                Value::Bool(b) => Some(Cow::Borrowed(if *b { "true" } else { "false" })),
                Value::Number(s) | Value::String(s) => Some(Cow::Borrowed(s)),
                Value::List(items) => {
                    let parts: Option<Vec<_>> = items.iter().map(join).collect();
                    Some(Cow::Owned(parts?.join(",")))
                }
                Value::Map(_) => None,
            }
        }
        let text = match self.get(key)? {
            Value::Null => return None,
            value => join(value)?,
        };
        let text = text.trim();
        (!text.is_empty()).then(|| text.to_string())
    }

    /// 布尔字段，认法和宿主一样：`true`/`yes`/`on`/`1` 和 `false`/`no`/`off`/`0`，
    /// 不分大小写，带不带引号都行。别的值是 `None`。
    ///
    /// 0.1.0 只认 true/false：`disable-model-invocation: yes` 在宿主里生效，在这里
    /// 读成没写，模型就能调用一个作者明确不让它调用的 skill。
    ///
    /// `None` 怎么算由字段决定，宿主的两个开关不对称：
    ///
    /// - `disable-model-invocation`：只有 `Some(true)` 算开。
    /// - `user-invocable`：**没写**才默认开；写了就只有 `Some(true)` 算开——空值、
    ///   `[]`、认不出的字，宿主都当 false，skill 从 `/` 菜单里消失。所以要写成
    ///   `get(k).is_none() || flag(k) == Some(true)`，不能写成 `flag(k) != Some(false)`。
    pub fn flag(&self, key: &str) -> Option<bool> {
        let word = match self.get(key)? {
            Value::Bool(b) => return Some(*b),
            Value::Number(n) => {
                let n = number_value(n)?;
                return (n == 1.0).then_some(true).or((n == 0.0).then_some(false));
            }
            Value::String(s) => s.trim().to_ascii_lowercase(),
            _ => return None,
        };
        match word.as_str() {
            "1" | "true" | "yes" | "on" => Some(true),
            "0" | "false" | "no" | "off" => Some(false),
            _ => None,
        }
    }

    /// `arguments` 这种「一串名字」字段，认法和宿主一样：YAML 列表，或者一个用
    /// 空白分开的字符串（文档原话 "a space-separated string or a YAML list"）。
    /// 逗号不算分隔符；纯数字的名字丢掉（会和 `$0`、`$1` 撞）；列表里不是字符串
    /// 的项也丢掉。
    ///
    /// `allowed-tools` 允许逗号分隔，但两个产品都不读它，这里也就不管。
    pub fn words(&self, key: &str) -> Vec<String> {
        let usable = |w: &str| !w.trim().is_empty() && !w.bytes().all(|b| b.is_ascii_digit());
        match self.get(key) {
            Some(Value::List(items)) => items
                .iter()
                .filter_map(|v| match v {
                    Value::String(s) if usable(s) => Some(s.clone()),
                    _ => None,
                })
                .collect(),
            Some(Value::String(s)) => s
                .split_whitespace()
                .filter(|w| usable(w))
                .map(str::to_string)
                .collect(),
            _ => Vec::new(),
        }
    }
}

fn normal_key(key: &str) -> String {
    key.chars()
        .map(|c| {
            if c == '_' {
                '-'
            } else {
                c.to_ascii_lowercase()
            }
        })
        .collect()
}

fn same_key(a: &str, b: &str) -> bool {
    normal_key(a) == normal_key(b)
}

/// 把文件拆成 frontmatter 原文和正文。
///
/// 没有 frontmatter（第一行不是 `---`，或者找不到结束的 `---`）时前一项是
/// `None`，正文是整个文件：一个只有正文的 `.claude/commands/x.md` 是合法的。
///
/// 收尾的 `---` 必须独占一行。宿主不要求（它用正则找第一个 `---`，值里写了
/// `A --- B` 就在那里截断，剩下的 frontmatter 算进正文），这里不跟：跟了只会
/// 让写在那之后的 `disable-model-invocation` 之类的开关跟着丢掉。
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

/// 读 frontmatter 原文（[`split`] 的第一项）。三步见模块开头；三步都读不了才报错，
/// 行号和分类取宽松读法那一次的。
pub fn parse(text: &str) -> Result<Frontmatter, ParseError> {
    let read = |text: &str, mode, reading| {
        let (entries, lines) = Parser::new(text, mode)?.document()?;
        Ok(Frontmatter {
            entries,
            lines,
            reading,
        })
    };
    if let Ok(fm) = read(text, Mode::Strict, Reading::Yaml) {
        return Ok(fm);
    }
    let requoted = requote(text);
    if requoted != text
        && let Ok(fm) = read(&requoted, Mode::Strict, Reading::Requoted)
    {
        return Ok(fm);
    }
    read(text, Mode::Lenient, Reading::Lenient)
}

/// 宿主第一次读失败后对原文的改写。逐行：
///
/// - 形如 `key: value` 的顶层行（键只含字母、`_`、`-`，冒号后至少一个空白），值
///   没有被同一种引号包住、也不是一个读得通的 `[…]`，而且含有
///   `` {}[]*&#!|>%@` `` 之一或 `: `，就改成 `key: "value"`（反斜杠和双引号转义）；
/// - 行首的每个 tab 换成两个空格。
///
/// 细节照宿主的正则：值里不能有 `\r`（宿主的 `.` 不匹配它，所以 CRLF 文件的行
/// 从来不被改写），键里不能有数字。
fn requote(text: &str) -> String {
    let mut out = String::with_capacity(text.len() + 16);
    for (i, line) in text.split('\n').enumerate() {
        if i > 0 {
            out.push('\n');
        }
        let line = requote_line(line).map_or(Cow::Borrowed(line), Cow::Owned);
        let tabs = line.len() - line.trim_start_matches('\t').len();
        out.push_str(&"  ".repeat(tabs));
        out.push_str(&line[tabs..]);
    }
    out
}

fn requote_line(line: &str) -> Option<String> {
    // JS 的 `\s`；和 Rust 的 is_whitespace 只差这两个。
    let js_space = |c: char| (c.is_whitespace() && c != '\u{85}') || c == '\u{feff}';
    let colon = line.find(':')?;
    let key = &line[..colon];
    if key.is_empty()
        || !key
            .bytes()
            .all(|b| b.is_ascii_alphabetic() || b == b'_' || b == b'-')
    {
        return None;
    }
    let after = &line[colon + 1..];
    if !after.starts_with(js_space) {
        return None;
    }
    let value = after.trim_start_matches(js_space);
    if value.is_empty() || value.contains(['\r', '\u{2028}', '\u{2029}']) {
        return None;
    }
    let wrapped =
        |q: char| value.len() >= q.len_utf8() && value.starts_with(q) && value.ends_with(q);
    if wrapped('"') || wrapped('\'') {
        return None;
    }
    if value.starts_with('[')
        && value.ends_with(']')
        && matches!(flow::whole(value, Mode::Strict), Ok(Value::List(_)))
    {
        return None;
    }
    let special = value.contains(|c: char| "{}[]*&#!|>%@`".contains(c)) || value.contains(": ");
    if !special {
        return None;
    }
    let escaped = value.replace('\\', "\\\\").replace('"', "\\\"");
    Some(format!("{key}: \"{escaped}\""))
}

/// 嵌套的上限，块状和流式共用。正常的 frontmatter 到不了。
const MAX_DEPTH: usize = 32;

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
            "a: \"say \\\"hi\\\"\\n\"\nb: 'it''s'\nd: see http://example.com/x\ne: C# projects # trailing comment\n",
        );
        assert_eq!(f.reading(), Reading::Yaml);
        assert_eq!(f.get("a"), Some(&Value::String("say \"hi\"\n".into())));
        assert_eq!(f.text("b"), Some("it's"));
        assert_eq!(f.text("d"), Some("see http://example.com/x"));
        assert_eq!(f.text("e"), Some("C# projects"));
    }

    /// 同一个 `e` 行，只因为多了一行 `c`（值里有 `: `，YAML 不许），读法就变了：
    /// 宿主给所有带特殊字符的顶层值加上引号重读，`#` 后面不再是注释。输入和
    /// 结果在 Claude Code 2.1.278 上核对过。
    #[test]
    fn one_bad_line_changes_how_the_host_reads_the_others() {
        let f = fm(
            "c: Use when: the user asks\ne: C# projects # trailing comment\nf: 'kept' # as is\n",
        );
        assert_eq!(f.reading(), Reading::Requoted);
        assert_eq!(f.text("c"), Some("Use when: the user asks"));
        assert_eq!(f.text("e"), Some("C# projects # trailing comment"));
        // 宿主只放过「首尾是同一种引号」的值；后面跟了注释就不算，整行照样加引号。
        assert_eq!(f.text("f"), Some("'kept' # as is"));
    }

    /// 认法照宿主：只按空白切，逗号是名字的一部分；纯数字和不是字符串的项丢掉。
    #[test]
    fn words_come_from_a_list_or_from_one_string() {
        let f = fm(
            "arguments: [issue, branch]\nspaced: issue  branch\ncommas: a, b\ndigits: a 1 b2 [3]\ntyped: [a, 1, true, \"2\", \"\", c]\nlisted:\n- a\n- b\n",
        );
        assert_eq!(f.words("arguments"), ["issue", "branch"]);
        assert_eq!(f.words("spaced"), ["issue", "branch"]);
        assert_eq!(f.words("commas"), ["a,", "b"]);
        assert_eq!(f.words("digits"), ["a", "b2", "[3]"]);
        assert_eq!(f.words("typed"), ["a", "c"]);
        assert_eq!(f.words("listed"), ["a", "b"]);
        assert!(f.words("missing").is_empty());
    }

    #[test]
    fn keys_match_across_dash_underscore_and_case() {
        let f = fm("when_to_use: after tests\nDisable-Model-Invocation: true\n");
        assert_eq!(f.text("when-to-use"), Some("after tests"));
        assert_eq!(f.flag("disable-model-invocation"), Some(true));
        assert!(f.duplicates().is_empty());
    }

    /// 写两遍时两边必须读出同一个值：宿主后写的赢。0.1.0 先写的赢，这个文件在
    /// 宿主里对模型隐藏、在这里却能被模型调用。
    #[test]
    fn a_repeated_key_is_read_the_way_the_host_reads_it() {
        let f = fm("disable-model-invocation: false\nname: a\ndisable-model-invocation: true\n");
        assert_eq!(f.flag("disable-model-invocation"), Some(true));
        assert_eq!(
            f.duplicates(),
            vec![vec![
                ("disable-model-invocation", 1),
                ("disable-model-invocation", 3)
            ]]
        );

        // 一字不差的写法优先，哪怕它写在前面：宿主只认这一种拼法。
        let f = fm("disable-model-invocation: true\nDisable_Model_Invocation: false\n");
        assert_eq!(f.flag("disable-model-invocation"), Some(true));
        assert_eq!(
            f.duplicates(),
            vec![vec![
                ("disable-model-invocation", 1),
                ("Disable_Model_Invocation", 2)
            ]]
        );

        // 没有一字不差的，才看归一化后同名的，也是后写的赢。
        let f = fm("Disable_Model_Invocation: false\nDISABLE-MODEL-INVOCATION: true\n");
        assert_eq!(f.flag("disable-model-invocation"), Some(true));
    }

    #[test]
    fn flags_take_the_words_the_host_takes() {
        let f = fm(
            "a: yes\nb: \"On\"\nc: 1\nd: 1.0\ne: no\nf: OFF\ng: 0\nh: \" TRUE \"\ni: maybe\nj: 2\nk: [true]\n",
        );
        for (key, want) in [
            ("a", Some(true)),
            ("b", Some(true)),
            ("c", Some(true)),
            ("d", Some(true)),
            ("e", Some(false)),
            ("f", Some(false)),
            ("g", Some(false)),
            ("h", Some(true)),
            ("i", None),
            ("j", None),
            ("k", None),
            ("missing", None),
        ] {
            assert_eq!(f.flag(key), want, "{key}");
        }
    }

    /// 官方文档的两个例子。第一个是 YAML 列表，宿主显示成 `issue-number`；第二个
    /// 不是合法的 YAML，宿主加引号重读，原样显示。0.1.0 把第二个读成一个只有
    /// 一项的列表 `["filename] [format"]`，提示就丢了。
    #[test]
    fn an_argument_hint_is_shown_the_way_the_host_shows_it() {
        let f = fm("argument-hint: [issue-number]\n");
        assert_eq!(f.text("argument-hint"), None);
        assert_eq!(f.string("argument-hint").as_deref(), Some("issue-number"));

        let f = fm("argument-hint: [filename] [format]\n");
        assert_eq!(f.reading(), Reading::Requoted);
        assert_eq!(f.text("argument-hint"), Some("[filename] [format]"));
        assert_eq!(
            f.string("argument-hint").as_deref(),
            Some("[filename] [format]")
        );

        let f = fm("a: [x, [y, z], 1, true, ~]\nb: {k: v}\nc: ~\nd: 7\n");
        assert_eq!(f.string("a").as_deref(), Some("x,y,z,1,true,"));
        assert_eq!(f.string("b"), None);
        assert_eq!(f.string("c"), None);
        assert_eq!(f.string("d").as_deref(), Some("7"));
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
    /// math-olympiad 的 SKILL.md）：格式化工具折出来的写法。引号串里的空行是一个
    /// 换行（0.1.0 把它吞了；宿主读出来有这个换行）。
    #[test]
    fn quoted_strings_and_flow_lists_may_span_lines() {
        let f = fm(
            "name: math\ndescription:\n  \"Solve competition problems with adversarial\n  verification. Activates on 'IMO', or\n\n  'Putnam'.\"\nallowed-tools:\n  [\n    \"Read\",\n    \"Bash(git *)\",\n    Grep,\n  ]\nafter: x\n",
        );
        assert_eq!(
            f.text("description"),
            Some(
                "Solve competition problems with adversarial verification. Activates on 'IMO', or\n'Putnam'."
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
    fn what_cannot_be_read_says_which_line() {
        for (text, line, kind) in [
            ("a: \"open\n", 1, ErrorKind::BadQuote),
            ("a: \"\\q\"\n", 1, ErrorKind::BadQuote),
            // from_str_radix 认开头的 `+`；YAML 不认。
            ("a: \"\\x+1\"\n", 1, ErrorKind::BadQuote),
            ("name: a\n\tb: c\n", 2, ErrorKind::TabIndent),
            ("name: a\njust some words\n", 2, ErrorKind::Unexpected),
            ("hooks:\n  - &x\n", 2, ErrorKind::Unsupported),
        ] {
            assert_eq!(parse(text), Err(ParseError { line, kind }), "{text:?}");
        }
    }

    /// 顶层的这些写法 YAML 读不了，宿主加引号重读，于是成了普通文字。0.1.0 在
    /// 这里整个报错，skill 连名字都拿不到。锚点 `&x 1` 例外：宿主（Bun.YAML）认
    /// 锚点，读成 `1`；这里不认，跟着第二步读成字面文字。
    #[test]
    fn what_the_host_rescues_by_quoting_is_plain_text_here_too() {
        for (text, key, want) in [
            ("description: `git` helper\n", "description", "`git` helper"),
            (
                "description: @claude does it\n",
                "description",
                "@claude does it",
            ),
            ("description: *Bold* first\n", "description", "*Bold* first"),
            ("ref: *x\n", "ref", "*x"),
            ("base: &x 1\n", "base", "&x 1"),
            ("list: [a,\nname: b\n", "list", "[a,"),
            ("tag: [draft] deploy\n", "tag", "[draft] deploy"),
        ] {
            let f = fm(text);
            assert_eq!(f.reading(), Reading::Requoted, "{text:?}");
            assert_eq!(f.text(key), Some(want), "{text:?}");
        }
        // 行首的 tab 在第二步里换成两个空格。
        let f = fm("metadata:\n\tauthor: me\n");
        assert_eq!(f.reading(), Reading::Requoted);
        assert_eq!(
            f.get("metadata"),
            Some(&Value::Map(vec![(
                "author".into(),
                Value::String("me".into())
            )]))
        );
    }

    #[test]
    fn flow_collections_may_nest() {
        let f = fm("a: [x, [y, z], {k: [1]}]\nb: {k: v, only}\nc: [p: q]\nd: [x,]\n");
        assert_eq!(f.reading(), Reading::Yaml);
        assert_eq!(
            f.get("a"),
            Some(&Value::List(vec![
                Value::String("x".into()),
                Value::List(vec![Value::String("y".into()), Value::String("z".into())]),
                Value::Map(vec![(
                    "k".into(),
                    Value::List(vec![Value::Number("1".into())])
                )]),
            ]))
        );
        assert_eq!(
            f.get("b"),
            Some(&Value::Map(vec![
                ("k".into(), Value::String("v".into())),
                ("only".into(), Value::Null),
            ]))
        );
        assert_eq!(
            f.get("c"),
            Some(&Value::List(vec![Value::Map(vec![(
                "p".into(),
                Value::String("q".into())
            )])]))
        );
        assert_eq!(
            f.get("d"),
            Some(&Value::List(vec![Value::String("x".into())]))
        );
    }

    /// 空的 `- ` 后面跟着和它对齐的 `- `，那是兄弟项。0.1.0 把后面的都当成了它的
    /// 内容：`[null, x]` 读成 `[[x]]`。
    #[test]
    fn an_empty_item_is_null_not_a_parent() {
        let f = fm("a:\n  -\n  - x\nb:\n- \n- y\nc:\n  -\n    - z\n");
        assert_eq!(
            f.get("a"),
            Some(&Value::List(vec![Value::Null, Value::String("x".into())]))
        );
        assert_eq!(
            f.get("b"),
            Some(&Value::List(vec![Value::Null, Value::String("y".into())]))
        );
        assert_eq!(
            f.get("c"),
            Some(&Value::List(vec![Value::List(vec![Value::String(
                "z".into()
            )])]))
        );
    }

    /// 值后面缩进的注释行不是值的一部分。0.1.0 把 `true` 读成了字符串 `"true "`。
    #[test]
    fn an_indented_comment_after_a_value_is_just_a_comment() {
        let f = fm("a: true\n  # why\nb: x\n  y\n  # tail\n");
        assert_eq!(f.reading(), Reading::Yaml);
        assert_eq!(f.get("a"), Some(&Value::Bool(true)));
        assert_eq!(f.get("b"), Some(&Value::String("x y".into())));
    }

    #[test]
    fn folded_blocks_keep_leading_blank_lines_and_more_indented_lines() {
        let f = fm("a: >\n\n  x\nb: >\n  one\n    two\n  three\n\n  four\n");
        assert_eq!(f.get("a"), Some(&Value::String("\nx\n".into())));
        assert_eq!(
            f.get("b"),
            Some(&Value::String("one\n  two\nthree\nfour\n".into()))
        );
    }

    #[test]
    fn every_yaml_escape_is_understood() {
        let f = fm("a: \"\\x41\\u00e9\\/\\ \\_\\N\\L\\P\\e\\a\\b\\v\\f\\0\"\n");
        assert_eq!(
            f.text("a"),
            Some("A\u{e9}/ \u{a0}\u{85}\u{2028}\u{2029}\u{1b}\u{7}\u{8}\u{b}\u{c}\0")
        );
    }

    /// 宿主（Bun.YAML）按 YAML 1.2 核心模式定数字；原文照旧保留。
    #[test]
    fn numbers_follow_the_yaml_core_schema() {
        let f = fm(
            "a: 0x1F\nb: 0o17\nc: 1e3\nd: -.inf\ne: .NaN\nf: +1\ng: 5.\nh: .5\ni: 1_000\nj: 0b1\nk: 12:30\nl: 1.2.3\n",
        );
        for key in ["a", "b", "c", "d", "e", "f", "g", "h"] {
            assert!(matches!(f.get(key), Some(Value::Number(_))), "{key}");
        }
        for key in ["i", "j", "k", "l"] {
            assert!(matches!(f.get(key), Some(Value::String(_))), "{key}");
        }
        assert_eq!(f.text("a"), Some("0x1F"));
    }

    /// 块标量里比内容缩进得浅的行：YAML 不许，宿主整个丢弃；这里宽松读出来。
    #[test]
    fn a_block_line_indented_too_little_is_only_lenient() {
        let f = fm("a: |\n    x\n  y\n");
        assert_eq!(f.reading(), Reading::Lenient);
        assert_eq!(f.get("a"), Some(&Value::String("x\ny\n".into())));
    }

    /// 严格的 YAML 在这里会报错（标量的续行里不许出现 `key: value`）。这里放过
    /// 它：作者把描述折到第二行、里面正好有个冒号，是常见写法，不该让整个 skill
    /// 读不出来。
    #[test]
    fn a_continuation_line_may_contain_a_colon() {
        let f = fm("description: Review code.\n  Use when: the user asks for a review.\nname: r\n");
        // 宿主两步都读不了，会把整个 frontmatter 当成空的。
        assert_eq!(f.reading(), Reading::Lenient);
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
