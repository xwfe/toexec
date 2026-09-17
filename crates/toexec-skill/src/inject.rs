//! 正文里要求「加载时先执行」的命令。
//!
//! Claude Code 的 skill 可以写 `` !`git status` ``：加载 skill 时 CLI 先跑这条
//! 命令，把输出填在原处，再把正文交给模型。多行的写成以 ```` ```! ```` 开头的
//! 代码块。
//!
//! 这里**只找，不跑**。跑不跑、谁来跑、以什么身份跑，是产品的安全决定：ccnm
//! 第一版就不自动执行（一次「读 skill」的调用不该触发项目指定的命令），而是
//! 把找到的命令列给模型，让它自己决定要不要用 `exec_command` 跑。

/// 一条注入命令，以及它在正文的第几行（从 1 数）。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Injection {
    pub line: usize,
    pub command: String,
}

/// 找出正文里所有的注入命令，按出现顺序。
///
/// 普通代码块（不带 `!` 的 ```` ``` ````）里的内容不看：文档里举例写一个
/// `` !`cmd` `` 不是在要求执行它。
pub fn find(body: &str) -> Vec<Injection> {
    let mut out = Vec::new();
    // 在代码块里时，记着开头那串反引号有多长，以及这是不是一个 `!` 块。
    let mut fence: Option<(usize, bool, usize, String)> = None;
    for (i, line) in body.lines().enumerate() {
        let trimmed = line.trim_start();
        let ticks = trimmed.len() - trimmed.trim_start_matches('`').len();
        if let Some((open, run, start, mut text)) = fence.take() {
            if ticks >= open && trimmed[ticks..].trim().is_empty() {
                if run && !text.trim().is_empty() {
                    out.push(Injection {
                        line: start,
                        command: text.trim_end().to_string(),
                    });
                }
                continue;
            }
            if run {
                text.push_str(line);
                text.push('\n');
            }
            fence = Some((open, run, start, text));
            continue;
        }
        if ticks >= 3 {
            let run = trimmed[ticks..].starts_with('!');
            fence = Some((ticks, run, i + 1, String::new()));
            continue;
        }
        let mut rest = line;
        while let Some(at) = rest.find("!`") {
            let after = &rest[at + 2..];
            let Some(end) = after.find('`') else {
                break;
            };
            let command = after[..end].trim();
            if !command.is_empty() {
                out.push(Injection {
                    line: i + 1,
                    command: command.to_string(),
                });
            }
            rest = &after[end + 1..];
        }
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    fn commands(body: &str) -> Vec<(usize, String)> {
        find(body)
            .into_iter()
            .map(|i| (i.line, i.command))
            .collect()
    }

    #[test]
    fn inline_commands_are_found_with_their_line() {
        let body = "# PR summary\n\n- diff: !`gh pr diff`\n- files: !`gh pr diff --name-only` and !`git status`\n";
        assert_eq!(
            commands(body),
            [
                (3, "gh pr diff".to_string()),
                (4, "gh pr diff --name-only".to_string()),
                (4, "git status".to_string()),
            ]
        );
    }

    #[test]
    fn a_bang_fence_is_one_multi_line_command() {
        let body = "Context:\n\n```!\nnode --version\nnpm --version\n```\n\nThen do the work.\n";
        assert_eq!(
            commands(body),
            [(3, "node --version\nnpm --version".to_string())]
        );
    }

    /// 文档里举例写的，不是要执行的。
    #[test]
    fn an_ordinary_code_block_is_not_searched() {
        let body = "Write it like this:\n\n```markdown\n- status: !`git status`\n```\n\nReal one: !`date`\n";
        assert_eq!(commands(body), [(7, "date".to_string())]);
    }

    #[test]
    fn nothing_to_find() {
        assert!(find("plain text, a lone !, an unclosed !`git status\n").is_empty());
        assert!(find("").is_empty());
        assert!(find("```!\n\n```\n").is_empty());
    }
}
