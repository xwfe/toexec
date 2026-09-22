//! 一次交不完的长结果：全文先留在内存里，模型按段接着读。
//!
//! gld 转本机 server（`read_mcp_result`）和 ccnm 转 Agent 机器上的 server
//! （同名工具）都是这样：结果的文字超过产品一次交回的上限，先交第一段，全文
//! 放这里，末尾告诉模型用哪个引用、从哪个位置接着读。**不静默截断**：留不下
//! 的部分（单条超过 [`Limits::max_item`]）在 [`Stored::kept`] 里看得出来，
//! 产品要照实告诉模型。
//!
//! 留多久、留多少由产品定（[`Limits`]）；过期的在下一次 `put` 时清掉，总量
//! 超了先丢最早的。每条结果记着是谁调的（`owner`）：别人的引用和不存在的一样，
//! 不给猜引用的机会。

use std::collections::HashMap;
use std::collections::hash_map::RandomState;
use std::hash::BuildHasher;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use crate::shape::{ceil_boundary, floor_boundary, part_end};

/// 留多久、留多少。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Limits {
    /// 过了这么久就不能再读。
    pub keep_for: Duration,
    /// 单条最多留这么多字节，后面的丢掉。
    pub max_item: usize,
    /// 所有留着的加起来最多这么多字节，超了先丢最早的。
    pub max_total: usize,
}

/// 留下了什么。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Stored {
    /// 接着读时用的引用。
    pub reference: String,
    /// 原文多少字节。
    pub size: usize,
    /// 留下了多少字节：小于 `size` 说明后面的丢了。
    pub kept: usize,
}

pub struct Kept {
    limits: Limits,
    items: Mutex<HashMap<String, Item>>,
    next: AtomicU64,
    /// 每个进程每个 `Kept` 不一样，让引用在重启之后不会撞上以前的。
    salt: u32,
}

struct Item {
    owner: String,
    text: Arc<str>,
    at: Instant,
}

impl Kept {
    pub fn new(limits: Limits) -> Kept {
        Kept {
            limits,
            items: Mutex::new(HashMap::new()),
            next: AtomicU64::new(1),
            salt: RandomState::new().hash_one(0u8) as u32,
        }
    }

    pub fn limits(&self) -> Limits {
        self.limits
    }

    /// 留下 `text`（超过单条上限的部分在字符边界上丢掉），返回引用。
    pub fn put(&self, owner: &str, mut text: String) -> Stored {
        let size = text.len();
        if size > self.limits.max_item {
            text.truncate(floor_boundary(&text, self.limits.max_item));
        }
        let kept = text.len();
        let reference = format!(
            "r{:08x}{}",
            self.salt,
            self.next.fetch_add(1, Ordering::Relaxed)
        );
        let mut items = self.items.lock().unwrap_or_else(|p| p.into_inner());
        items.retain(|_, item| item.at.elapsed() < self.limits.keep_for);
        let mut total: usize = items.values().map(|item| item.text.len()).sum();
        while total + kept > self.limits.max_total {
            let Some(oldest) = items
                .iter()
                .min_by_key(|(_, item)| item.at)
                .map(|(key, _)| key.clone())
            else {
                break;
            };
            if let Some(gone) = items.remove(&oldest) {
                total -= gone.text.len();
            }
        }
        items.insert(
            reference.clone(),
            Item {
                owner: owner.to_string(),
                text: text.into(),
                at: Instant::now(),
            },
        );
        Stored {
            reference,
            size,
            kept,
        }
    }

    /// `owner` 自己留下、还没过期的那条。
    pub fn get(&self, owner: &str, reference: &str) -> Option<Arc<str>> {
        let items = self.items.lock().unwrap_or_else(|p| p.into_inner());
        items
            .get(reference)
            .filter(|item| item.owner == owner && item.at.elapsed() < self.limits.keep_for)
            .map(|item| item.text.clone())
    }
}

/// 从 `offset` 起最多 `max` 字节的一段，返回 `(开始, 结束)`：开始挪到字符
/// 边界上，结束尽量落在换行后面（[`part_end`]）。`offset` 超过全文长度时
/// 返回 `None`。
pub fn page(text: &str, offset: usize, max: usize) -> Option<(usize, usize)> {
    if offset > text.len() {
        return None;
    }
    let start = ceil_boundary(text, offset);
    Some((start, part_end(text, start, max)))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn limits(max_item: usize, max_total: usize) -> Limits {
        Limits {
            keep_for: Duration::from_secs(60),
            max_item,
            max_total,
        }
    }

    #[test]
    fn a_result_is_read_back_only_by_whoever_made_the_call() {
        let kept = Kept::new(limits(100, 1000));
        let stored = kept.put("alice", "hello".into());
        assert_eq!((stored.size, stored.kept), (5, 5));
        assert_eq!(
            kept.get("alice", &stored.reference).as_deref(),
            Some("hello")
        );
        assert!(kept.get("bob", &stored.reference).is_none());
        assert!(kept.get("alice", "r-nothing").is_none());
    }

    #[test]
    fn too_long_is_cut_on_a_char_boundary_and_says_so() {
        let kept = Kept::new(limits(4, 1000));
        let stored = kept.put("a", "ab中cd".into());
        assert_eq!((stored.size, stored.kept), (7, 2));
        assert_eq!(kept.get("a", &stored.reference).as_deref(), Some("ab"));
    }

    #[test]
    fn over_the_total_the_oldest_goes_first() {
        let kept = Kept::new(limits(10, 10));
        let first = kept.put("a", "12345".into());
        std::thread::sleep(Duration::from_millis(2));
        let second = kept.put("a", "67890".into());
        let third = kept.put("a", "xyz".into());
        assert!(kept.get("a", &first.reference).is_none());
        assert!(kept.get("a", &second.reference).is_some());
        assert!(kept.get("a", &third.reference).is_some());
        assert_ne!(second.reference, third.reference);
    }

    #[test]
    fn expired_results_are_gone() {
        let kept = Kept::new(Limits {
            keep_for: Duration::from_millis(1),
            max_item: 10,
            max_total: 100,
        });
        let stored = kept.put("a", "x".into());
        std::thread::sleep(Duration::from_millis(5));
        assert!(kept.get("a", &stored.reference).is_none());
    }

    #[test]
    fn a_page_starts_on_a_char_boundary_and_ends_after_a_newline() {
        let text = "line one\nline two\n中文";
        assert_eq!(page(text, 0, 12), Some((0, 9)));
        assert_eq!(page(text, 9, 100), Some((9, text.len())));
        // 落在"中"的中间：挪到它后面。
        assert_eq!(page(text, 19, 100), Some((21, text.len())));
        assert_eq!(page(text, text.len(), 10), Some((text.len(), text.len())));
        assert_eq!(page(text, text.len() + 1, 10), None);
    }
}
