//! 本机子进程当通道：它的 stdin 发、stdout 收、stderr 留最后一段。
//!
//! **进程由产品起**，这里只接手一个已经起好的 [`Child`]（stdin、stdout、stderr
//! 都是管道）。怎么起是产品的安全决定，不同产品答案不同：环境变量留哪些、
//! 要不要套 OS 沙箱、放不放进自己的进程组、`PATH` 从哪来。同样，**怎么杀**也由
//! 产品给（[`Stop`]）：杀整个进程组要么靠 `killpg`（`unsafe`），要么起一个
//! `kill -- -<pgid>`，产品各有规矩，这里一个都不替它选。
//!
//! 这里管的是两边一样的部分：
//!
//! - **一行有上限**（[`MAX_MESSAGE_BYTES`]，用 `toexec-text` 的有界读行）：别人家的
//!   server 一行吐 2 GB 不能把产品吃掉；
//! - 读 stdout 能超时（一个线程读、channel 交），否则一个卡住的 server 把调用方
//!   永远挂住；
//! - stderr 只留最后 [`STDERR_KEEP`] 字节：server 起不来时原因多半在这里；
//! - 关的时候先关 stdin 让它读到 EOF 自己退，宽限期过了才动手。

use std::collections::VecDeque;
use std::io::{BufReader, Read, Write};
use std::process::{Child, ChildStdin, ChildStdout};
use std::sync::mpsc::{self, Receiver, RecvTimeoutError};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use crate::client::Error;
use crate::transport::{MAX_MESSAGE_BYTES, Recv, Transport};

/// stderr 留最后这么多字节。
pub const STDERR_KEEP: usize = 4 * 1024;

/// 关的时候等它自己退多久。MCP server 读到 stdin 的 EOF 就该退，正常几十毫秒。
pub const CLOSE_GRACE: Duration = Duration::from_secs(3);

/// 关连接时产品要做的收尾。
///
/// `exited` 为假：宽限期过了它还在，要杀（连同它的进程组）；杀完这里会
/// `wait` 它。为真：它自己退了，但它起的进程（`npx` 下面的 `node`、playwright
/// 开的浏览器）可能还在同一个进程组里，产品要不要清看它自己。
pub type Stop = Box<dyn FnMut(&mut Child, bool) + Send>;

pub struct ChildTransport {
    child: Child,
    /// 关掉之后是 `None`：让对面读到 EOF 就是靠丢掉它。
    stdin: Option<ChildStdin>,
    lines: Receiver<Recv>,
    stderr: Arc<Mutex<VecDeque<u8>>>,
    stop: Stop,
    closed: bool,
}

impl ChildTransport {
    /// 接手一个 stdin、stdout、stderr 都是管道的子进程。管道没接好是调用方的
    /// bug，报 [`Error::Start`]，并按 [`Stop`] 收掉它。
    pub fn new(mut child: Child, mut stop: Stop) -> Result<ChildTransport, Error> {
        let (Some(stdin), Some(stdout), Some(stderr)) =
            (child.stdin.take(), child.stdout.take(), child.stderr.take())
        else {
            stop(&mut child, false);
            let _ = child.wait();
            return Err(Error::Start(
                "the server process was started without piped stdin, stdout and stderr".into(),
            ));
        };
        let (tx, lines) = mpsc::channel();
        std::thread::spawn(move || pump_lines(stdout, tx));
        let kept = Arc::new(Mutex::new(VecDeque::new()));
        let sink = kept.clone();
        std::thread::spawn(move || pump_stderr(stderr, sink));
        Ok(ChildTransport {
            child,
            stdin: Some(stdin),
            lines,
            stderr: kept,
            stop,
            closed: false,
        })
    }

    /// 子进程的 pid（也是它的进程组号，如果产品把它放进了自己的组）。
    pub fn id(&self) -> u32 {
        self.child.id()
    }

    fn said(&self) -> String {
        let kept = self
            .stderr
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner());
        let bytes: Vec<u8> = kept.iter().copied().collect();
        String::from_utf8_lossy(&bytes).trim().to_string()
    }
}

impl Transport for ChildTransport {
    fn send(&mut self, line: &str, _timeout: Duration) -> Result<(), Error> {
        let Some(stdin) = self.stdin.as_mut() else {
            return Err(Error::Closed {
                during: "send".into(),
                said: String::new(),
            });
        };
        let written = stdin
            .write_all(line.as_bytes())
            .and_then(|()| stdin.write_all(b"\n"))
            .and_then(|()| stdin.flush());
        written.map_err(|_| Error::Closed {
            during: "send".into(),
            said: self.said(),
        })
    }

    fn recv(&mut self, timeout: Duration) -> Recv {
        match self.lines.recv_timeout(timeout) {
            Ok(Recv::Closed { .. }) | Err(RecvTimeoutError::Disconnected) => {
                // stdout 关了之后 stderr 可能还差最后一截没读完。
                std::thread::sleep(Duration::from_millis(50));
                Recv::Closed { said: self.said() }
            }
            Ok(other) => other,
            Err(RecvTimeoutError::Timeout) => Recv::Timeout,
        }
    }

    fn close(&mut self) {
        if self.closed {
            return;
        }
        self.closed = true;
        self.stdin.take();
        let deadline = Instant::now() + CLOSE_GRACE;
        let exited = loop {
            match self.child.try_wait() {
                Ok(Some(_)) => break true,
                Ok(None) if Instant::now() < deadline => {
                    std::thread::sleep(Duration::from_millis(20))
                }
                _ => break false,
            }
        };
        (self.stop)(&mut self.child, exited);
        if !exited {
            let _ = self.child.kill();
            let _ = self.child.wait();
        }
    }
}

impl Drop for ChildTransport {
    fn drop(&mut self) {
        self.close();
    }
}

fn pump_lines(stdout: ChildStdout, tx: mpsc::Sender<Recv>) {
    let mut reader = BufReader::new(stdout);
    let limits = toexec_text::LineLimits {
        keep: MAX_MESSAGE_BYTES,
        scan_limit: None,
    };
    let mut raw = Vec::new();
    let mut scanned = 0u64;
    loop {
        raw.clear();
        let before = scanned;
        let item = match toexec_text::next_line(&mut reader, &mut raw, limits, &mut scanned) {
            Ok(Some(_)) => {
                let length = scanned - before;
                // 行尾最多两个字节（`\r\n`）；比留下的多出更多，就是被截过。
                if length > raw.len() as u64 + 2 {
                    Recv::TooLong(length)
                } else {
                    Recv::Line(String::from_utf8_lossy(&raw).into_owned())
                }
            }
            Ok(None) | Err(_) => {
                let _ = tx.send(Recv::Closed {
                    said: String::new(),
                });
                return;
            }
        };
        if tx.send(item).is_err() {
            return;
        }
    }
}

fn pump_stderr(stderr: std::process::ChildStderr, sink: Arc<Mutex<VecDeque<u8>>>) {
    let mut reader = BufReader::new(stderr);
    let mut buf = [0u8; 1024];
    loop {
        match reader.read(&mut buf) {
            Ok(0) | Err(_) => return,
            Ok(n) => {
                let mut kept = sink.lock().unwrap_or_else(|poisoned| poisoned.into_inner());
                kept.extend(&buf[..n]);
                while kept.len() > STDERR_KEEP {
                    kept.pop_front();
                }
            }
        }
    }
}

#[cfg(all(test, unix))]
mod tests {
    use super::*;
    use std::process::{Command, Stdio};
    use std::sync::atomic::{AtomicBool, Ordering};

    fn start(script: &str, stop: Stop) -> ChildTransport {
        let child = Command::new("sh")
            .args(["-c", script])
            .env("GREETING", "hi")
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .spawn()
            .expect("spawn sh");
        ChildTransport::new(child, stop).expect("piped")
    }

    fn nothing() -> Stop {
        Box::new(|_, _| {})
    }

    #[test]
    fn a_line_goes_out_and_one_comes_back() {
        let mut child = start("read line; echo \"$GREETING:$line\"", nothing());
        child.send("ping", Duration::from_secs(1)).unwrap();
        match child.recv(Duration::from_secs(5)) {
            Recv::Line(line) => assert_eq!(line, "hi:ping"),
            _ => panic!("expected a line"),
        }
    }

    #[test]
    fn what_it_said_on_stderr_comes_with_the_close() {
        let mut child = start("echo 'missing API key' >&2; exit 1", nothing());
        match child.recv(Duration::from_secs(5)) {
            Recv::Closed { said } => assert!(said.contains("missing API key"), "{said}"),
            _ => panic!("expected closed"),
        }
    }

    /// `\r\n` 结尾的一行：两个字节的行尾不能被当成"截过"。
    #[test]
    fn a_crlf_line_is_not_mistaken_for_a_cut_one() {
        let mut child = start("printf 'abc\\r\\n'", nothing());
        match child.recv(Duration::from_secs(5)) {
            Recv::Line(line) => assert_eq!(line, "abc"),
            _ => panic!("expected a line"),
        }
    }

    #[test]
    fn a_server_that_exits_on_eof_is_not_killed() {
        let killed = Arc::new(AtomicBool::new(false));
        let seen = killed.clone();
        let mut child = start(
            "cat >/dev/null",
            Box::new(move |_, exited| {
                if !exited {
                    seen.store(true, Ordering::SeqCst);
                }
            }),
        );
        child.close();
        assert!(!killed.load(Ordering::SeqCst), "读到 EOF 就退的不该被杀");
    }

    /// 读到 EOF 不退的：宽限期过了交给产品杀，这里再 wait 它。
    #[test]
    fn a_server_that_ignores_eof_is_handed_to_the_stop_after_the_grace() {
        let asked = Arc::new(AtomicBool::new(false));
        let seen = asked.clone();
        let mut child = start(
            "trap '' TERM; while true; do sleep 1; done",
            Box::new(move |child, exited| {
                if !exited {
                    seen.store(true, Ordering::SeqCst);
                    let _ = child.kill();
                }
            }),
        );
        let started = Instant::now();
        child.close();
        assert!(asked.load(Ordering::SeqCst));
        assert!(started.elapsed() >= CLOSE_GRACE);
        assert!(started.elapsed() < CLOSE_GRACE + Duration::from_secs(5));
        assert!(child.child.try_wait().unwrap().is_some(), "已经收尸");
    }

    #[test]
    fn a_child_without_pipes_is_refused_and_stopped() {
        let child = Command::new("sh")
            .args(["-c", "sleep 30"])
            .spawn()
            .expect("spawn");
        let stopped = Arc::new(AtomicBool::new(false));
        let seen = stopped.clone();
        let Err(error) = ChildTransport::new(
            child,
            Box::new(move |child, _| {
                seen.store(true, Ordering::SeqCst);
                let _ = child.kill();
            }),
        ) else {
            panic!("should refuse");
        };
        assert!(error.to_string().contains("piped"), "{error}");
        assert!(stopped.load(Ordering::SeqCst));
    }
}
