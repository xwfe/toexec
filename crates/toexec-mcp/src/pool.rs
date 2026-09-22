//! 开着的 server 连接：用到才开、一个 server 一个调用方一条、闲了收掉。
//!
//! 四条规矩（gld 连远端 ccnm 的 bridge 也是这一套）：
//!
//! 1. **按需打开，不预热。**一起来就把全部 server 拉起来，等于替没用它的人先付
//!    启动的钱（`npx` 冷启动要下载包）。
//! 2. **按"server + 调用方"分连接。**playwright 这种 server 有状态（一个浏览器、
//!    打开的页面），两个调用方共用一条就能看到对方的页面。只有一个调用方的产品
//!    （ccnm 的一个会话）传同一个名字就行。
//! 3. **等 server 回话时不拿全局锁。**表的锁只用来找到槽位，一个卡住的 server
//!    不能把别的 server 一起卡住。
//! 4. **关连接不在任何锁里做。**关 stdio server 要等它退出（最多几秒）。
//!
//! 怎么把连接开出来（起进程、连地址）由产品给（[`Open`]）：环境、沙箱、代理都是
//! 产品的决定。

use std::collections::HashMap;
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use serde_json::Value;

use crate::client::{Client, Error};
use crate::installed::Server;
use crate::transport::Transport;

/// 握手（含冷启动）默认等多久。`npx -y` 第一次跑要下载包，实测缓存过的
/// 0.5–1.6 秒；Codex 的 `startup_timeout_sec` 写了就用它的。
pub const STARTUP_TIMEOUT: Duration = Duration::from_secs(30);
/// 一次调用默认等多久。Codex 的 `tool_timeout_sec` 写了就用它的。
pub const CALL_TIMEOUT: Duration = Duration::from_secs(60);
/// 闲多久收掉。有状态的 server（playwright 的浏览器）收掉就没了状态，下次
/// 调用重开一个新的。
pub const IDLE_AFTER: Duration = Duration::from_secs(5 * 60);

/// 怎么把一条通道开出来，连同握手时报给 server 的名字。
pub trait Open: Send + Sync {
    fn open(&self, server: &Server) -> Result<Box<dyn Transport>, Error>;
    /// 报给 server 的客户端名和版本。
    fn me(&self) -> (&str, &str);
}

/// 一条握过手的连接，连同握手时读到的工具表。
pub struct Live {
    pub client: Client,
    /// 过滤前的全部工具（`tools/list` 原样）。
    pub tools: Vec<Value>,
    pub started_in: Duration,
    /// 开这条连接时的配置。配置一变就是另一个 server 了，旧连接作废。
    config: String,
    last_used: Instant,
}

type Slot = Arc<Mutex<Option<Live>>>;
type Key = (String, String);

pub struct Pool {
    idle_after: Duration,
    slots: Mutex<HashMap<Key, Slot>>,
}

impl Pool {
    pub fn new() -> Pool {
        Pool {
            idle_after: IDLE_AFTER,
            slots: Mutex::new(HashMap::new()),
        }
    }

    /// 闲多久收掉，默认 [`IDLE_AFTER`]。
    pub fn idle_after(mut self, after: Duration) -> Pool {
        self.idle_after = after;
        self
    }

    /// 拿到 `server` 给 `caller` 的那条连接（没有就开），在它上面做 `work`。
    ///
    /// `work` 失败且错误说明连接坏了（超时、断了），这条连接就丢掉，下次重开。
    /// 别的调用正占着这条连接时，最多等 `wait` 就报忙。
    pub fn with<R>(
        &self,
        server: &Server,
        caller: &str,
        opener: &dyn Open,
        wait: Duration,
        work: impl FnOnce(&mut Live) -> Result<R, Error>,
    ) -> Result<R, Error> {
        self.reap();
        let slot = {
            let mut slots = self.slots.lock().unwrap_or_else(|p| p.into_inner());
            slots
                .entry((server.name.clone(), caller.to_string()))
                .or_default()
                .clone()
        };
        let mut guard = lock_within(&slot, wait).ok_or(Error::Busy { waited: wait })?;
        let config = format!("{:?}", server.transport);
        let stale = guard.as_ref().is_some_and(|live| live.config != config);
        if stale {
            // 在槽位锁里关：别的调用本来就要等这个槽位。
            *guard = None;
        }
        if guard.is_none() {
            *guard = Some(connect(opener, server, config)?);
        }
        let live = guard.as_mut().expect("just opened");
        let outcome = work(live);
        match &outcome {
            Err(error) if error.breaks_connection() => *guard = None,
            _ => live.last_used = Instant::now(),
        }
        outcome
    }

    /// 已经开着的那条连接上有哪些工具。没开着、或者正被占着，都是 `None`——
    /// 这只是给列表看的，不值得为它等。
    pub fn known_tools(&self, server: &str, caller: &str) -> Option<Vec<Value>> {
        let slot = self
            .slots
            .lock()
            .unwrap_or_else(|p| p.into_inner())
            .get(&(server.to_string(), caller.to_string()))
            .cloned()?;
        let guard = slot.try_lock().ok()?;
        guard.as_ref().map(|live| live.tools.clone())
    }

    /// 只留这几个 server 的连接：操作员关掉的，正开着的连接跟着收。
    pub fn retain(&self, servers: &[String]) {
        let gone: Vec<Slot> = {
            let mut slots = self.slots.lock().unwrap_or_else(|p| p.into_inner());
            let keys: Vec<Key> = slots
                .keys()
                .filter(|(server, _)| !servers.contains(server))
                .cloned()
                .collect();
            keys.iter().filter_map(|key| slots.remove(key)).collect()
        };
        close(gone);
    }

    pub fn close_all(&self) {
        let all: Vec<Slot> = self
            .slots
            .lock()
            .unwrap_or_else(|p| p.into_inner())
            .drain()
            .map(|(_, slot)| slot)
            .collect();
        close(all);
    }

    /// 每 `every` 收一次闲着的连接，直到这个池子没了。
    ///
    /// 光靠下一次调用时顺手收不够：用完一次就再没人调的话，`npx` 起的 node、
    /// playwright 开的浏览器会一直挂到服务停掉。
    pub fn sweep(pool: &Arc<Pool>, every: Duration) {
        let pool = Arc::downgrade(pool);
        std::thread::spawn(move || {
            loop {
                std::thread::sleep(every);
                match pool.upgrade() {
                    Some(pool) => pool.reap(),
                    None => return,
                }
            }
        });
    }

    /// 收掉闲着的连接。正在用的（有人持有槽位、或者锁拿不到）不算闲。
    fn reap(&self) {
        let idle: Vec<Slot> = {
            let mut slots = self.slots.lock().unwrap_or_else(|p| p.into_inner());
            let keys: Vec<Key> = slots
                .iter()
                .filter(|(_, slot)| is_idle(slot, self.idle_after))
                .map(|(key, _)| key.clone())
                .collect();
            keys.iter().filter_map(|key| slots.remove(key)).collect()
        };
        close(idle);
    }
}

/// 不进连接池，起一次、握手、列工具（试一个 server 起不起得来）。丢掉返回值
/// 就关了。
pub fn try_once(opener: &dyn Open, server: &Server) -> Result<Live, Error> {
    connect(opener, server, String::new())
}

fn connect(opener: &dyn Open, server: &Server, config: String) -> Result<Live, Error> {
    let startup = server.startup_timeout.unwrap_or(STARTUP_TIMEOUT);
    let started = Instant::now();
    let transport = opener.open(server)?;
    let mut client = Client::connect(transport, opener.me(), startup)?;
    let tools = client.list_tools(startup)?;
    Ok(Live {
        client,
        tools,
        started_in: started.elapsed(),
        config,
        last_used: Instant::now(),
    })
}

impl Default for Pool {
    fn default() -> Self {
        Pool::new()
    }
}

fn is_idle(slot: &Slot, after: Duration) -> bool {
    if Arc::strong_count(slot) > 1 {
        return false;
    }
    match slot.try_lock() {
        Ok(guard) => guard
            .as_ref()
            .is_none_or(|live| live.last_used.elapsed() >= after),
        Err(_) => false,
    }
}

fn lock_within(slot: &Slot, wait: Duration) -> Option<std::sync::MutexGuard<'_, Option<Live>>> {
    let deadline = Instant::now() + wait;
    loop {
        match slot.try_lock() {
            Ok(guard) => return Some(guard),
            Err(std::sync::TryLockError::Poisoned(poisoned)) => return Some(poisoned.into_inner()),
            Err(std::sync::TryLockError::WouldBlock) if Instant::now() < deadline => {
                std::thread::sleep(Duration::from_millis(20))
            }
            Err(std::sync::TryLockError::WouldBlock) => return None,
        }
    }
}

/// 丢掉就是关掉（`Client` 的 `Drop`）。放在所有锁外面做。
fn close(slots: Vec<Slot>) {
    for slot in slots {
        if let Ok(mut guard) = slot.lock() {
            guard.take();
        }
    }
}

#[cfg(test)]
pub(crate) mod tests {
    use super::*;
    use crate::installed::{Source, Transport as Config};
    use crate::scripted::plain_server;
    use std::collections::BTreeMap;
    use std::sync::atomic::{AtomicUsize, Ordering};

    pub(crate) struct Counting(pub(crate) Arc<AtomicUsize>);

    impl Open for Counting {
        fn open(&self, _server: &Server) -> Result<Box<dyn Transport>, Error> {
            self.0.fetch_add(1, Ordering::SeqCst);
            Ok(Box::new(plain_server("2025-06-18")))
        }
        fn me(&self) -> (&str, &str) {
            ("test", "0")
        }
    }

    pub(crate) fn server(name: &str, command: &str) -> Server {
        Server {
            name: name.into(),
            source: Source::Claude,
            transport: Config::Stdio {
                command: command.into(),
                args: Vec::new(),
                env: BTreeMap::new(),
                cwd: None,
            },
            off_in_source: false,
            enabled_tools: None,
            disabled_tools: Vec::new(),
            startup_timeout: None,
            tool_timeout: None,
            missing_env: Vec::new(),
        }
    }

    const WAIT: Duration = Duration::from_secs(1);

    fn counting() -> (Counting, Arc<AtomicUsize>) {
        let opened = Arc::new(AtomicUsize::new(0));
        (Counting(opened.clone()), opened)
    }

    #[test]
    fn a_connection_is_opened_once_per_caller_and_again_when_the_config_changes() {
        let (opener, opened) = counting();
        let pool = Pool::new();
        let demo = server("demo", "a");
        for _ in 0..3 {
            pool.with(&demo, "alice", &opener, WAIT, |live| {
                assert_eq!(live.tools.len(), 2);
                Ok(())
            })
            .unwrap();
        }
        assert_eq!(opened.load(Ordering::SeqCst), 1, "同一个人复用一条");
        pool.with(&demo, "bob", &opener, WAIT, |_| Ok(())).unwrap();
        assert_eq!(opened.load(Ordering::SeqCst), 2, "换一个人另开一条");
        pool.with(&server("demo", "b"), "alice", &opener, WAIT, |_| Ok(()))
            .unwrap();
        assert_eq!(opened.load(Ordering::SeqCst), 3, "配置变了就重开");
        assert!(pool.known_tools("demo", "alice").is_some());
        assert!(pool.known_tools("demo", "carol").is_none());
    }

    #[test]
    fn a_broken_connection_is_dropped_and_a_refusal_is_not() {
        let (opener, opened) = counting();
        let pool = Pool::new();
        let demo = server("demo", "a");
        let _ = pool.with(&demo, "alice", &opener, WAIT, |_| -> Result<(), Error> {
            Err(Error::Refused {
                code: -32602,
                message: "bad".into(),
            })
        });
        assert!(pool.known_tools("demo", "alice").is_some());
        let _ = pool.with(&demo, "alice", &opener, WAIT, |_| -> Result<(), Error> {
            Err(Error::Timeout {
                during: "tools/call".into(),
                waited: WAIT,
            })
        });
        assert!(pool.known_tools("demo", "alice").is_none());
        pool.with(&demo, "alice", &opener, WAIT, |_| Ok(()))
            .unwrap();
        assert_eq!(opened.load(Ordering::SeqCst), 2);
    }

    #[test]
    fn idle_and_turned_off_connections_are_closed() {
        let (opener, _) = counting();
        let pool = Pool::new().idle_after(Duration::ZERO);
        pool.with(&server("a", "a"), "alice", &opener, WAIT, |_| Ok(()))
            .unwrap();
        // 下一次进来时先收闲着的：a 已经闲了"零秒"。
        pool.with(&server("b", "b"), "alice", &opener, WAIT, |_| Ok(()))
            .unwrap();
        assert!(pool.known_tools("a", "alice").is_none());

        let pool = Pool::new();
        pool.with(&server("a", "a"), "alice", &opener, WAIT, |_| Ok(()))
            .unwrap();
        pool.retain(&["b".to_string()]);
        assert!(pool.known_tools("a", "alice").is_none());
    }

    /// 没人再来调用，闲着的也要收：后台每隔一会儿看一次。
    #[test]
    fn nobody_calling_again_still_gets_idle_connections_closed() {
        let (opener, _) = counting();
        let pool = Arc::new(Pool::new().idle_after(Duration::from_millis(50)));
        pool.with(&server("a", "a"), "alice", &opener, WAIT, |_| Ok(()))
            .unwrap();
        Pool::sweep(&pool, Duration::from_millis(20));
        let deadline = Instant::now() + Duration::from_secs(3);
        while pool.known_tools("a", "alice").is_some() && Instant::now() < deadline {
            std::thread::sleep(Duration::from_millis(20));
        }
        assert!(pool.known_tools("a", "alice").is_none());
    }

    #[test]
    fn a_second_call_waits_for_the_first_and_then_gives_up() {
        let pool = Arc::new(Pool::new());
        let demo = server("demo", "a");
        let (started_tx, started_rx) = std::sync::mpsc::channel();
        let busy = {
            let pool = pool.clone();
            let demo = demo.clone();
            std::thread::spawn(move || {
                let (opener, _) = counting();
                pool.with(&demo, "alice", &opener, WAIT, |_| {
                    started_tx.send(()).unwrap();
                    std::thread::sleep(Duration::from_millis(500));
                    Ok(())
                })
            })
        };
        started_rx.recv().unwrap();
        let (opener, _) = counting();
        let error = pool
            .with(&demo, "alice", &opener, Duration::from_millis(50), |_| {
                Ok(())
            })
            .unwrap_err();
        assert!(matches!(error, Error::Busy { .. }), "{error}");
        busy.join().unwrap().unwrap();
    }
}
