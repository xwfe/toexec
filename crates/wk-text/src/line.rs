//! 有界地读一行。
//!
//! 标准库的 `BufRead::read_line` 和 `lines()` 都会把一整行读进内存。一个
//! 2 GB 的单行文件（压缩过的 JS、一行导出的 JSON）因此会先分配 2 GB，然后
//! 调用方才有机会说"太长了"。真实后果是 Runtime 先被 OOM 杀掉。
//!
//! 这里反过来：先说好一行最多留多少字节、最多往前走多少字节，超出的部分
//! 只数不存。

use std::io::BufRead;

/// 一行是怎么结束的。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Terminator {
    Crlf,
    Lf,
    /// 文件最后一行没有换行符，或者这一行还没走到头就撞上了 `scan_limit`。
    None,
}

/// 一次读行的两个上限。
#[derive(Debug, Clone, Copy)]
pub struct LineLimits {
    /// 这一行最多保留多少字节。剩下的读过去、计数，但不进内存。
    pub keep: usize,
    /// 累计走过多少字节就放弃当前行（`None` = 不限）。
    ///
    /// 到了上限就带着不完整的行返回 `Terminator::None`。调用方接着会因为
    /// 自己的预算拒绝这次读取，所以继续读一条可能永远不结束的行只是浪费时间。
    pub scan_limit: Option<u64>,
}

/// 读一行到 `raw`，最多留 `limits.keep` 字节，把走过的字节数累加到 `scanned`。
///
/// 返回 `None` 表示文件已经读完。**终结符不进 `raw`**——它可能在保留下来的
/// 那一截之后好几兆，所以单独报出来。保留的前缀不会停在半个字符上，这样被
/// 截断的行不会被误判成非法 UTF-8。
///
/// `raw` 由调用方清空和复用；`scanned` 跨行累计，也由调用方持有。
pub fn next_line<R: BufRead>(
    reader: &mut R,
    raw: &mut Vec<u8>,
    limits: LineLimits,
    scanned: &mut u64,
) -> std::io::Result<Option<Terminator>> {
    let mut started = false;
    let mut last = None;
    let mut cut = false;
    loop {
        let chunk = reader.fill_buf()?;
        if chunk.is_empty() {
            if !started {
                return Ok(None);
            }
            trim_cut(raw, cut);
            return Ok(Some(Terminator::None));
        }
        started = true;
        let newline = chunk.iter().position(|&b| b == b'\n');
        let body = &chunk[..newline.unwrap_or(chunk.len())];
        let room = limits.keep.saturating_sub(raw.len());
        cut |= body.len() > room;
        raw.extend_from_slice(&body[..body.len().min(room)]);
        // `\n` 前面那个字节可能是上一块的最后一个。
        let before_newline = body.last().copied().or(last);
        last = before_newline;
        let used = newline.map_or(chunk.len(), |at| at + 1);
        reader.consume(used);
        *scanned += used as u64;
        if newline.is_some() {
            let crlf = before_newline == Some(b'\r');
            // 被 cut 掉的行里根本没存到那个 `\r`，不能再 pop 一个字符走。
            if crlf && !cut {
                raw.pop();
            }
            trim_cut(raw, cut);
            return Ok(Some(if crlf {
                Terminator::Crlf
            } else {
                Terminator::Lf
            }));
        }
        if limits.scan_limit.is_some_and(|limit| *scanned > limit) {
            return Ok(Some(Terminator::None));
        }
    }
}

/// 把 `keep` 切口留下的半个字符去掉。
///
/// 只在真的截断过时做：没截断的行交给调用方原样判断编码，这里不替它决定
/// 非法 UTF-8 该怎么办。
fn trim_cut(raw: &mut Vec<u8>, cut: bool) {
    if !cut {
        return;
    }
    if let Err(e) = std::str::from_utf8(raw)
        && e.error_len().is_none()
    {
        raw.truncate(e.valid_up_to());
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::BufReader;

    const NO_LIMIT: LineLimits = LineLimits {
        keep: usize::MAX,
        scan_limit: None,
    };

    /// 读到头，把每一行和它的终结符收上来。
    fn all(data: &[u8], limits: LineLimits) -> (Vec<(String, Terminator)>, u64) {
        let mut reader = BufReader::new(data);
        let mut out = Vec::new();
        let mut raw = Vec::new();
        let mut scanned = 0;
        loop {
            raw.clear();
            match next_line(&mut reader, &mut raw, limits, &mut scanned).unwrap() {
                None => return (out, scanned),
                Some(end) => out.push((String::from_utf8_lossy(&raw).into_owned(), end)),
            }
        }
    }

    #[test]
    fn each_line_comes_back_with_its_own_terminator() {
        let (lines, scanned) = all(b"a\nb\r\nc", NO_LIMIT);
        assert_eq!(
            lines,
            vec![
                ("a".into(), Terminator::Lf),
                ("b".into(), Terminator::Crlf),
                ("c".into(), Terminator::None),
            ]
        );
        assert_eq!(scanned, 6);
    }

    #[test]
    fn an_empty_file_ends_at_once() {
        assert_eq!(all(b"", NO_LIMIT).0, vec![]);
    }

    #[test]
    fn a_bare_newline_is_still_a_line() {
        let (lines, _) = all(b"\n\n", NO_LIMIT);
        assert_eq!(
            lines,
            vec![
                (String::new(), Terminator::Lf),
                (String::new(), Terminator::Lf)
            ]
        );
    }

    #[test]
    fn what_is_past_keep_is_counted_not_stored() {
        let long = "x".repeat(1000);
        let data = format!("{long}\nshort\n");
        let (lines, scanned) = all(
            data.as_bytes(),
            LineLimits {
                keep: 10,
                scan_limit: None,
            },
        );
        assert_eq!(lines.len(), 2);
        assert_eq!(lines[0].0, "x".repeat(10));
        assert_eq!(lines[0].1, Terminator::Lf);
        assert_eq!(lines[1].0, "short");
        // 走过的字节是整份文件，不是留下来的那点。
        assert_eq!(scanned, data.len() as u64);
    }

    #[test]
    fn a_cut_never_leaves_half_a_character() {
        // "中" 是 3 字节，keep=10 正好落在第四个字的中间。
        let data = "中".repeat(100) + "\n";
        let (lines, _) = all(
            data.as_bytes(),
            LineLimits {
                keep: 10,
                scan_limit: None,
            },
        );
        assert_eq!(lines[0].0, "中".repeat(3));
        assert!(std::str::from_utf8(lines[0].0.as_bytes()).is_ok());
    }

    #[test]
    fn a_cut_crlf_line_does_not_lose_one_more_byte() {
        let long = "x".repeat(100);
        let (lines, _) = all(
            format!("{long}\r\nnext\n").as_bytes(),
            LineLimits {
                keep: 10,
                scan_limit: None,
            },
        );
        // 那个 `\r` 根本没进 raw，所以留下的正好是 10 个 x。
        assert_eq!(lines[0].0, "x".repeat(10));
        assert_eq!(lines[0].1, Terminator::Crlf);
        assert_eq!(lines[1].0, "next");
    }

    /// `\r` 和 `\n` 落在不同的 fill_buf 块里，仍然是 CRLF。
    #[test]
    fn a_crlf_split_across_buffers_is_still_crlf() {
        struct OneByte<'a>(&'a [u8]);
        impl std::io::Read for OneByte<'_> {
            fn read(&mut self, _: &mut [u8]) -> std::io::Result<usize> {
                unreachable!("BufRead 直接走 fill_buf")
            }
        }
        impl BufRead for OneByte<'_> {
            fn fill_buf(&mut self) -> std::io::Result<&[u8]> {
                Ok(&self.0[..self.0.len().min(1)])
            }
            fn consume(&mut self, amount: usize) {
                self.0 = &self.0[amount..];
            }
        }

        let mut reader = OneByte(b"ab\r\ncd\n");
        let mut raw = Vec::new();
        let mut scanned = 0;
        assert_eq!(
            next_line(&mut reader, &mut raw, NO_LIMIT, &mut scanned).unwrap(),
            Some(Terminator::Crlf)
        );
        assert_eq!(raw, b"ab");
    }

    /// 一条永远不结束的行：到了 scan_limit 就得停，不能把内存填满。
    #[test]
    fn an_endless_line_stops_at_the_scan_limit() {
        /// 只会吐 'x'，永远没有换行符。
        struct Endless {
            piece: [u8; Self::PIECE],
            pending: usize,
            served: u64,
        }
        impl Endless {
            const PIECE: usize = 64 * 1024;
            fn new() -> Self {
                Endless {
                    piece: [b'x'; Self::PIECE],
                    pending: 0,
                    served: 0,
                }
            }
        }
        impl std::io::Read for Endless {
            fn read(&mut self, _: &mut [u8]) -> std::io::Result<usize> {
                unreachable!("BufRead 直接走 fill_buf")
            }
        }
        impl BufRead for Endless {
            fn fill_buf(&mut self) -> std::io::Result<&[u8]> {
                assert!(
                    self.served <= 8 * 1024 * 1024,
                    "扫描上限没拦住，已经吐了 {} 字节",
                    self.served
                );
                if self.pending == 0 {
                    self.pending = Self::PIECE;
                }
                Ok(&self.piece[Self::PIECE - self.pending..])
            }
            fn consume(&mut self, amount: usize) {
                self.pending -= amount;
                self.served += amount as u64;
            }
        }

        let mut raw = Vec::new();
        let mut scanned = 0;
        let end = next_line(
            &mut Endless::new(),
            &mut raw,
            LineLimits {
                keep: 32 * 1024,
                scan_limit: Some(1024 * 1024),
            },
            &mut scanned,
        )
        .unwrap();
        assert_eq!(end, Some(Terminator::None));
        assert!(scanned > 1024 * 1024, "{scanned}");
        // 留下来的只有 keep 那么多，不是走过的那一兆。
        assert_eq!(raw.len(), 32 * 1024);
    }

    #[test]
    fn without_a_scan_limit_it_reads_to_the_newline() {
        let data = format!("{}\n", "x".repeat(300_000));
        let (lines, _) = all(
            data.as_bytes(),
            LineLimits {
                keep: 16,
                scan_limit: None,
            },
        );
        assert_eq!(lines.len(), 1);
        assert_eq!(lines[0].1, Terminator::Lf);
        assert_eq!(lines[0].0.len(), 16);
    }
}
