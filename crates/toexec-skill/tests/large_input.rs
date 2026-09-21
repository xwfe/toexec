//! 巨大、畸形的 frontmatter 要在线性时间里读完（或报错）。
//!
//! 为什么要测：skill 目录是仓库作者写的，产品每次列目录都要把每个 SKILL.md
//! 读一遍（ccnm 的上限是 1 MiB 一个文件）。0.1.0 在跨行的引号串和 `[…]` 上
//! 是平方级的：416 KB、引号一直不闭合的 frontmatter 要 10 秒（release 构建），
//! 1 MiB 约一分钟——一个文件就能卡住整个 skill 扫描。
//!
//! 时限给得很宽（debug 构建、CI 机器慢），只为挡住平方级：现在 1 MiB 在
//! release 下是十几毫秒。

use std::time::{Duration, Instant};

use toexec_skill::frontmatter::{ErrorKind, parse, split};

const MIB: usize = 1024 * 1024;
const LIMIT: Duration = Duration::from_secs(10);

/// 把 `unit` 重复到约 1 MiB，前面加上 `head`。
fn big(head: &str, unit: &str) -> String {
    let mut text = head.to_string();
    text.push_str(&unit.repeat(MIB / unit.len()));
    text
}

fn timed<T>(name: &str, f: impl FnOnce() -> T) -> T {
    let started = Instant::now();
    let out = f();
    let spent = started.elapsed();
    assert!(spent < LIMIT, "{name}: {spent:?}");
    out
}

#[test]
fn an_unclosed_multi_line_string_is_read_once() {
    for (name, text) in [
        ("double quote", big("a: \"x\n", "  yyyyyyyyyy\n")),
        ("single quote", big("a: 'x\n", "  it''s yyyy\n")),
        ("bracket", big("a: [x,\n", "  yyyyyyyyyy,\n")),
        ("brace", big("a: {x: 1,\n", "  k: yyyyyy,\n")),
    ] {
        timed(name, || parse(&text)).ok();
    }
}

#[test]
fn a_closed_multi_line_string_is_read_once() {
    let mut text = big("a: \"x\n", "  yyyyyyyyyy\n");
    text.push_str("  z\"\n");
    let fm = timed("closed quote", || parse(&text)).expect("reads");
    assert!(fm.text("a").is_some_and(|a| a.ends_with('z')));
}

#[test]
fn long_lines_blocks_and_comments_are_linear() {
    for (name, text) in [
        ("one long line", big("description: ", "word ")),
        ("block scalar", big("a: |\n", "  line of text\n")),
        ("folded block", big("a: >\n", "  line\n\n")),
        ("plain continuation", big("a: x\n", "  more words\n")),
        ("comment lines", big("a: 1\n", "# comment\n")),
        ("blank lines", big("a: 1\n", "\n")),
        ("list items", big("a:\n", "  - item\n")),
        ("many keys", big("", "key: value\n")),
        (
            "long flow list",
            format!("a: [{}x]\n", "item, ".repeat(MIB / 6)),
        ),
    ] {
        timed(name, || parse(&text)).ok();
    }
}

/// `duplicates` 在一百万个同名键上也不能是平方级。
#[test]
fn many_repeated_keys_are_grouped_in_linear_time() {
    let text = big("", "k: v\n");
    let fm = timed("parse", || parse(&text)).expect("reads");
    let groups = timed("duplicates", || fm.duplicates());
    assert_eq!(groups.len(), 1);
    assert_eq!(fm.text("k"), Some("v"));
}

/// 嵌套太深要报 TooDeep，而不是把栈用光（深度有上限，递归不会跟着输入走）。
/// 缩进逐行加深的 1400 行合起来约 1 MiB。
#[test]
fn deep_nesting_stops_at_the_floor() {
    let keys: String = (0..1400)
        .map(|d| format!("{}k:\n", " ".repeat(d)))
        .collect();
    let items: String = "a:\n".to_string()
        + &(0..1400)
            .map(|d| format!("{}-\n", " ".repeat(d)))
            .collect::<String>();
    for (name, text) in [("keys", keys), ("items", items)] {
        let err = timed(name, || parse(&text)).expect_err(name);
        assert_eq!(err.kind, ErrorKind::TooDeep, "{name}");
    }
    // 流式的一样有上限。顶层的那种第二步会被宿主的规则加上引号，读成普通文字；
    // 这里只要它不爆栈、按时读完。
    for (name, text) in [
        ("flow lists", format!("a: {}\n", "[".repeat(100_000))),
        ("flow maps", format!("a: {}\n", "{k: ".repeat(100_000))),
        ("nested flow", format!("a:\n  b: {}\n", "[".repeat(100_000))),
    ] {
        timed(name, || parse(&text)).ok();
    }
}

#[test]
fn splitting_a_huge_file_is_linear() {
    let text = big("---\n", "k: v\n") + "---\nbody\n";
    let (front, body) = timed("split", || split(&text));
    assert!(front.is_some());
    assert_eq!(body, "body\n");
    let unclosed = big("---\n", "k: v\n");
    assert_eq!(timed("unclosed", || split(&unclosed)).0, None);
}
