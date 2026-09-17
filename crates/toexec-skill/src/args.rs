//! 调用 skill 时带的参数：切开，再填进正文。
//!
//! 语义对的是 Claude Code 2.1.273 打包代码里的实现（文档没写全，下面带 * 的
//! 几条是读代码读出来的）：
//!
//! ```text
//! $ARGUMENTS        调用时给的整串参数
//! $ARGUMENTS[N]     第 N 个（从 0 数）；没给第 N 个就原样留着 *
//! $N                同上的简写；后面紧跟字母数字就不算；没给第 N 个就原样留着 *
//! $name             frontmatter 的 arguments 里声明的第几个名字，就是第几个参数；
//!                   没给就换成空串 *；后面紧跟 `[` 或字母数字就不算 *
//! \$ARGUMENTS       前面加反斜杠：不换，输出时去掉反斜杠 *
//! ${CLAUDE_…}       环境给的值，见 [`Context`]
//! ```
//!
//! `$N` 越界时留着不换，是有实际意义的：skill 正文里经常有
//! `awk '{print $1}'` 这样的 shell 片段，调用时没给参数就把 `$1` 抹掉，脚本就
//! 坏了，而且坏得无声无息。
//!
//! 上面几种**一个都没换成**而调用时又给了参数，就在末尾补一行
//! `ARGUMENTS: <整串>`——不然参数就无声无息地丢了。

/// 按 shell 的习惯把一串参数切开：空白分隔，单引号或双引号括起来的算一个。
///
/// 只认引号，不认反斜杠、变量、通配符：这不是在执行命令，只是让
/// `/fix "login page" 42` 这种多词参数能算成一个。引号没闭合时，剩下的全算
/// 最后一个参数。
pub fn split(raw: &str) -> Vec<String> {
    let mut out = Vec::new();
    let mut current = String::new();
    let mut quote: Option<char> = None;
    let mut started = false;
    for c in raw.chars() {
        match quote {
            Some(q) if c == q => quote = None,
            Some(_) => current.push(c),
            None if c == '"' || c == '\'' => {
                quote = Some(c);
                started = true;
            }
            None if c.is_whitespace() => {
                if started {
                    out.push(std::mem::take(&mut current));
                    started = false;
                }
            }
            None => {
                current.push(c);
                started = true;
            }
        }
    }
    if started {
        out.push(current);
    }
    out
}

/// 环境给的那几个值。哪个是 `None`，对应的占位符就原样留着——留着一个看得见的
/// `${CLAUDE_SESSION_ID}`，比悄悄换成空串好查。
#[derive(Debug, Clone, Copy, Default)]
pub struct Context<'a> {
    /// `${CLAUDE_SKILL_DIR}`：SKILL.md 所在的目录。
    pub skill_dir: Option<&'a str>,
    /// `${CLAUDE_PROJECT_DIR}`：项目根。
    pub project_dir: Option<&'a str>,
    /// `${CLAUDE_SESSION_ID}`。
    pub session_id: Option<&'a str>,
}

/// 把占位符换掉。`raw` 是调用时给的整串参数，`names` 是 frontmatter 的
/// `arguments`。
///
/// 只扫一遍：换进去的参数值里即使有 `$0`，也不会再被换一次。
pub fn substitute(body: &str, raw: &str, names: &[String], context: Context<'_>) -> String {
    let raw = raw.trim();
    let positional = split(raw);
    let mut out = String::with_capacity(body.len() + raw.len());
    let mut used = false;
    let mut rest = body;
    while let Some(at) = rest.find('$') {
        let escaped = rest[..at].ends_with('\\');
        out.push_str(&rest[..at]);
        let tail = &rest[at + 1..];
        match placeholder(tail, raw, &positional, names, context) {
            // `\$0`：占位符本身原样输出，反斜杠去掉。
            Some(found) if escaped && found.argument => {
                out.pop();
                out.push('$');
                out.push_str(&tail[..found.len]);
                rest = &tail[found.len..];
            }
            Some(Found {
                text: Some(text),
                len,
                argument,
            }) => {
                used |= argument;
                out.push_str(&text);
                rest = &tail[len..];
            }
            // 写法是占位符，但没有第 N 个参数可换：原样留着。
            Some(_) | None => {
                out.push('$');
                rest = tail;
            }
        }
    }
    out.push_str(rest);
    if !raw.is_empty() && !used {
        out.push_str("\n\nARGUMENTS: ");
        out.push_str(raw);
    }
    out
}

struct Found {
    /// 换成什么。`None`：写法是占位符，但调用时没给到这个位置。
    text: Option<String>,
    /// 吃掉了 `$` 后面多少字节。
    len: usize,
    /// 是参数占位符（而不是 `${CLAUDE_…}`）。只有这一类能被 `\$` 转义，也只有
    /// 这一类算「参数用过了」。
    argument: bool,
}

/// `$` 后面这一段是不是占位符。
fn placeholder(
    tail: &str,
    raw: &str,
    positional: &[String],
    names: &[String],
    context: Context<'_>,
) -> Option<Found> {
    let word_char = |c: char| c.is_ascii_alphanumeric() || c == '_';
    let argument = |text: Option<String>, len: usize| Found {
        text,
        len,
        argument: true,
    };
    if let Some(inner) = tail.strip_prefix('{') {
        let end = inner.find('}')?;
        let value = match &inner[..end] {
            "CLAUDE_SKILL_DIR" => context.skill_dir,
            "CLAUDE_PROJECT_DIR" => context.project_dir,
            "CLAUDE_SESSION_ID" => context.session_id,
            _ => None,
        }?;
        return Some(Found {
            text: Some(value.to_string()),
            len: end + 2,
            argument: false,
        });
    }
    // 声明过的名字优先，长的先试：`$issue-number` 不该被 `$issue` 抢走。
    let mut declared: Vec<(usize, &String)> = names.iter().enumerate().collect();
    declared.sort_by_key(|(_, name)| std::cmp::Reverse(name.len()));
    for (n, name) in declared {
        if name.is_empty() || name.chars().all(|c| c.is_ascii_digit()) {
            continue;
        }
        // 声明过的名字没给值是空串，不是原样留着——和位置参数不一样。
        if let Some(after) = tail.strip_prefix(name.as_str())
            && !after.starts_with(|c: char| c == '[' || word_char(c))
        {
            return Some(argument(
                Some(positional.get(n).cloned().unwrap_or_default()),
                name.len(),
            ));
        }
    }
    if let Some(after) = tail.strip_prefix("ARGUMENTS") {
        if let Some(index) = after.strip_prefix('[') {
            let end = index.find(']')?;
            let n = index[..end].parse::<usize>().ok()?;
            let len = "ARGUMENTS".len() + end + 2;
            return Some(argument(positional.get(n).cloned(), len));
        }
        return Some(argument(Some(raw.to_string()), "ARGUMENTS".len()));
    }
    let digits = tail.len() - tail.trim_start_matches(|c: char| c.is_ascii_digit()).len();
    if digits > 0 && !tail[digits..].starts_with(word_char) {
        let n = tail[..digits].parse::<usize>().ok()?;
        return Some(argument(positional.get(n).cloned(), digits));
    }
    None
}

#[cfg(test)]
mod tests {
    use super::*;

    fn names(list: &[&str]) -> Vec<String> {
        list.iter().map(|s| s.to_string()).collect()
    }

    #[test]
    fn split_keeps_quoted_words_together() {
        assert_eq!(split("a  b\tc"), ["a", "b", "c"]);
        assert_eq!(split(r#"fix "login page" 42"#), ["fix", "login page", "42"]);
        // 引号没闭合：剩下的全算最后一个参数。
        assert_eq!(split("a 'b c"), ["a", "b c"]);
        assert_eq!(split(r#""" x"#), ["", "x"]);
        assert!(split("   ").is_empty());
    }

    #[test]
    fn the_whole_string_and_each_position() {
        let out = substitute(
            "All: $ARGUMENTS\nFirst: $ARGUMENTS[0], second: $1, missing: [$ARGUMENTS[5]] [$7]",
            r#"staging "two words""#,
            &[],
            Context::default(),
        );
        assert_eq!(
            out,
            "All: staging \"two words\"\nFirst: staging, second: two words, missing: [$ARGUMENTS[5]] [$7]"
        );
    }

    #[test]
    fn named_arguments_map_to_positions() {
        let out = substitute(
            "Fix #$issue on $branch ($issue-number, $issue_x, $issue[0], missing: [$third])",
            "42 main",
            &names(&["issue", "branch", "third"]),
            Context::default(),
        );
        assert_eq!(
            out,
            "Fix #42 on main (42-number, $issue_x, $issue[0], missing: [])"
        );
    }

    #[test]
    fn a_name_with_a_hyphen_wins_over_its_prefix() {
        let out = substitute(
            "$issue-number / $issue",
            "7 8",
            &names(&["issue-number", "issue"]),
            Context::default(),
        );
        assert!(out.starts_with("7 / 8"), "{out}");
    }

    /// 官方行为：一个占位符都没换成、而调用时给了参数，就补在末尾。
    #[test]
    fn unused_arguments_are_appended_not_dropped() {
        assert_eq!(
            substitute("Do the thing.", "quickly", &[], Context::default()),
            "Do the thing.\n\nARGUMENTS: quickly"
        );
        // 只用了 `$0` 也算用过了，不再补。
        assert_eq!(
            substitute("Do $0.", "it now", &[], Context::default()),
            "Do it."
        );
        assert_eq!(
            substitute("Do the thing.", "  ", &[], Context::default()),
            "Do the thing."
        );
        assert_eq!(
            substitute("Do $ARGUMENTS.", "it", &[], Context::default()),
            "Do it."
        );
    }

    #[test]
    fn context_values_fill_in_and_missing_ones_stay_visible() {
        let context = Context {
            skill_dir: Some(".claude/skills/deploy"),
            project_dir: Some("."),
            session_id: None,
        };
        assert_eq!(
            substitute(
                "run ${CLAUDE_SKILL_DIR}/scripts/go.sh in ${CLAUDE_PROJECT_DIR}; id ${CLAUDE_SESSION_ID}; ${HOME}",
                "",
                &[],
                context
            ),
            "run .claude/skills/deploy/scripts/go.sh in .; id ${CLAUDE_SESSION_ID}; ${HOME}"
        );
    }

    #[test]
    fn what_is_not_a_placeholder_is_left_alone() {
        let body = "cost: $5 off, $unknown, a lone $ and ${unclosed";
        assert_eq!(
            substitute(body, "", &names(&["issue"]), Context::default()),
            body
        );
    }

    /// 官方对 `$ARGUMENTS` 是不带词边界的全量替换，后面紧跟字母也照换。
    #[test]
    fn arguments_has_no_word_boundary() {
        assert_eq!(
            substitute("$ARGUMENTSX", "a b", &[], Context::default()),
            "a bX"
        );
    }

    /// skill 正文里的 shell 片段。没给参数时 `$1` 必须活下来。
    #[test]
    fn a_positional_placeholder_with_no_argument_behind_it_survives() {
        let body = "Run: awk '{print $1, $2}' file";
        assert_eq!(substitute(body, "", &[], Context::default()), body);
        // 给了一个参数：`$0` 才有东西可换，`$1`、`$2` 仍然没有。
        assert_eq!(
            substitute(body, "only", &[], Context::default()),
            "Run: awk '{print $1, $2}' file\n\nARGUMENTS: only"
        );
    }

    #[test]
    fn a_backslash_keeps_a_placeholder_literal() {
        assert_eq!(
            substitute(
                r"literal \$ARGUMENTS and \$0, real $0, price \$5",
                "x",
                &[],
                Context::default()
            ),
            r"literal $ARGUMENTS and $0, real x, price $5"
        );
    }

    #[test]
    fn a_digit_followed_by_a_word_character_is_not_positional() {
        assert_eq!(
            substitute("$0abc $0", "x", &[], Context::default()),
            "$0abc x"
        );
    }

    /// 参数值里的 `$0` 不会被再换一次：一遍扫完，换进去的内容不回头看。
    #[test]
    fn substituted_text_is_not_rescanned() {
        assert_eq!(
            substitute("$0 then $1", "$1 second", &[], Context::default()),
            "$1 then second"
        );
    }
}
