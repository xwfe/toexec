//! 随机变异的模糊测试：不崩、不卡、结果自洽。
//!
//! 从一批像样的 frontmatter 出发，随机插入/删除 YAML 里有意义的字符、复制和
//! 调换行、改缩进、截断，每个结果都交给 `split` + `parse` 和全部取值方法。
//! 只查三件事：不 panic；每个输入在时限内结束；报错的行号落在输入范围里、
//! 同一个输入读两遍结果一样。读得「对不对」不在这里查——那是差分测试的事
//! （`evidence/x08-skill-frontmatter/`，拿 Claude Code 自己的解析器当对照）。
//!
//! 为什么不用 cargo-fuzz：它要 nightly 和一个全局安装的工具，而这个 crate 的
//! 约定是零依赖、CI 只跑 stable。这里用固定种子的伪随机数，`cargo test` 每次跑
//! 同一批输入，出了问题能复现：
//!
//! ```text
//! TOEXEC_FUZZ_CASES=2000000 TOEXEC_FUZZ_SEED=7 cargo test --release -p toexec-skill --test fuzz
//! ```

use std::panic::{AssertUnwindSafe, catch_unwind};
use std::time::{Duration, Instant};

use toexec_skill::frontmatter::{parse, split};

const SEEDS: &[&str] = &[
    "name: deploy\ndescription: >\n  Deploy the service.\n  Use after tests pass.\n\n  Never on Fridays.\nuser-invocable: false\n",
    "a: \"say \\\"hi\\\"\\n\"\nb: 'it''s'\nc: Use when: the user asks\nd: see http://x.io/a\ne: C# # c\n",
    "arguments: [issue, branch]\nallowed-tools:\n  [\n    \"Read\",\n    Grep,\n  ]\nargument-hint: [pr] [priority]\n",
    "name: guarded\nhooks:\n  PreToolUse:\n    - matcher: \"Bash\"\n      hooks:\n        - type: command\n          command: \"./check.sh\"\n    - matcher: Edit\n",
    "a: |+\n  one\n\n    two\nb: >-\n\n  x\nc: |2\n   y\n",
    "metadata:\n  author: me\n  tags: {k: [1, 2], only}\nlist:\n- \n- x\n-\n  - y\n",
    "disable-model-invocation: yes\nDisable_Model_Invocation: 0\nuser-invocable: [true]\n",
    "description: `git` helper @me *Bold* &x !tag %y\nversion: 0x1F\nn: .inf\n",
    "description: Review code.\n  Use when: the user asks.\n  # c\n  more\nname: r\n",
    "\u{feff}k: \"\\u00e9\\x41\\N\\_\"\r\nq: 'multi\r\n  line'\r\n\tt: tab\r\n",
];

/// YAML 里有意义的字符，外加几个容易出边界问题的。
const ALPHABET: &[char] = &[
    ' ', ' ', ' ', '\t', '\n', '\n', '\r', ':', '-', '#', '\'', '"', '[', ']', '{', '}', ',', '|',
    '>', '&', '*', '!', '%', '@', '`', '?', '\\', '.', '0', '1', 'a', 'x', 'é', '中', '\u{2028}',
    '\u{85}', '\u{feff}', '\0',
];

/// xorshift64*：够用，而且不用加依赖。
struct Rng(u64);

impl Rng {
    fn next(&mut self) -> u64 {
        self.0 ^= self.0 >> 12;
        self.0 ^= self.0 << 25;
        self.0 ^= self.0 >> 27;
        self.0.wrapping_mul(0x2545_f491_4f6c_dd1d)
    }

    fn below(&mut self, n: usize) -> usize {
        (self.next() % n.max(1) as u64) as usize
    }
}

fn mutate(rng: &mut Rng, text: &str) -> String {
    let mut chars: Vec<char> = text.chars().collect();
    for _ in 0..=rng.below(6) {
        let at = rng.below(chars.len() + 1);
        match rng.below(7) {
            0 | 1 => chars.insert(at, ALPHABET[rng.below(ALPHABET.len())]),
            2 if !chars.is_empty() => {
                chars.remove(at.min(chars.len() - 1));
            }
            3 => {
                // 复制一行到别处
                let lines: Vec<String> = chars
                    .iter()
                    .collect::<String>()
                    .split('\n')
                    .map(str::to_string)
                    .collect();
                let line = &lines[rng.below(lines.len())];
                let mut out = lines.clone();
                out.insert(rng.below(lines.len() + 1), line.clone());
                chars = out.join("\n").chars().collect();
            }
            4 => {
                // 在行首加减缩进
                let start = chars[..at]
                    .iter()
                    .rposition(|&c| c == '\n')
                    .map_or(0, |i| i + 1);
                if rng.below(2) == 0 {
                    for _ in 0..rng.below(5) {
                        chars.insert(start, ' ');
                    }
                } else {
                    while chars.get(start) == Some(&' ') {
                        chars.remove(start);
                    }
                }
            }
            5 => chars.truncate(at),
            _ => {
                // 把一段重复很多遍：长行、深缩进、一长串括号
                let from = rng.below(chars.len() + 1);
                let to = (from + rng.below(8)).min(chars.len());
                let piece: Vec<char> = chars[from..to].to_vec();
                for _ in 0..rng.below(64) {
                    chars.splice(at..at, piece.iter().copied());
                }
            }
        }
    }
    chars.into_iter().collect()
}

fn check(text: &str) {
    let started = Instant::now();
    let first = parse(text);
    let spent = started.elapsed();
    assert!(spent < Duration::from_secs(1), "took {spent:?}");
    assert_eq!(parse(text), first, "not deterministic");
    match &first {
        Err(e) => {
            // 行号数的是 `str::lines` 的行；空输入也可能报第 1 行。
            let lines = text.lines().count().max(1);
            assert!(e.line >= 1 && e.line <= lines, "line {} of {lines}", e.line);
        }
        Ok(fm) => {
            for key in [
                "name",
                "description",
                "when_to_use",
                "argument-hint",
                "arguments",
                "disable-model-invocation",
                "user-invocable",
                "metadata",
                "",
            ] {
                let _ = (
                    fm.get(key),
                    fm.text(key),
                    fm.string(key),
                    fm.flag(key),
                    fm.words(key),
                );
            }
            for group in fm.duplicates() {
                assert!(group.len() > 1);
                assert!(group.windows(2).all(|w| w[0].1 <= w[1].1), "{group:?}");
            }
        }
    }
    let file = format!("---\n{text}---\nbody\n");
    let (front, body) = split(&file);
    if let Some(front) = front {
        assert!(file.contains(front) && file.ends_with(body));
    }
}

#[test]
fn random_mutations_neither_panic_nor_hang() {
    let cases: usize = std::env::var("TOEXEC_FUZZ_CASES")
        .ok()
        .and_then(|v| v.parse().ok())
        .unwrap_or(20_000);
    let seed: u64 = std::env::var("TOEXEC_FUZZ_SEED")
        .ok()
        .and_then(|v| v.parse().ok())
        .unwrap_or(0x5eed);
    let mut rng = Rng(seed | 1);
    for n in 0..cases {
        let mut text = SEEDS[rng.below(SEEDS.len())].to_string();
        // 变异可以叠加：一部分输入会离合法的 frontmatter 越来越远。
        for _ in 0..=rng.below(3) {
            text = mutate(&mut rng, &text);
        }
        if let Err(panic) = catch_unwind(AssertUnwindSafe(|| check(&text))) {
            let why = panic
                .downcast_ref::<String>()
                .map(String::as_str)
                .or_else(|| panic.downcast_ref::<&str>().copied())
                .unwrap_or("panic");
            panic!("case {n} (seed {seed}): {why}\ninput: {text:?}");
        }
    }
}
