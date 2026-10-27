//! 进程层面的效果：父进程的标准输出是一根管道时，它起的后台子进程还拿不拿
//! 着这根管道。拿着，管道的另一头就要等子进程退出才读到结束——这就是 gld
//! 在 Windows CI 上卡满 20 分钟的那种挂法。
//!
//! 三代进程都是这个测试程序自己，靠环境变量 `TOEXEC_WIN_ROLE` 分角色：
//! 测试本身读"父进程"的标准输出直到结束；"父进程"起一个睡 10 秒的"孙进程"
//! （三路都是 null，什么都不想交给它）就退出。
#![cfg(windows)]

use std::io::Read;
use std::process::{Command, Stdio};
use std::time::{Duration, Instant};

const ROLE: &str = "TOEXEC_WIN_ROLE";
const SLEEP: Duration = Duration::from_secs(10);

/// 起一代"父进程"，读它的标准输出直到结束，返回花了多久。
fn eof_after_parent_exits(stop_inheritance: bool) -> Duration {
    let mut parent = Command::new(std::env::current_exe().unwrap())
        .args(["--exact", "parent_role", "--nocapture", "--test-threads=1"])
        .env(
            ROLE,
            if stop_inheritance {
                "parent-stop"
            } else {
                "parent"
            },
        )
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::null())
        .spawn()
        .unwrap();
    let started = Instant::now();
    let mut out = Vec::new();
    parent.stdout.take().unwrap().read_to_end(&mut out).unwrap();
    let took = started.elapsed();
    parent.wait().unwrap();
    took
}

#[test]
fn a_background_grandchild_no_longer_holds_the_parents_output() {
    if std::env::var_os(ROLE).is_some() {
        return;
    }
    let took = eof_after_parent_exits(true);
    assert!(
        took < Duration::from_secs(5),
        "the grandchild still held the pipe: EOF after {took:?}"
    );
}

/// 对照：不调的话，孙进程拿着管道，结束要等它睡完。它证明上面那条测的
/// 确实是这件事，而不是碰巧快。
#[test]
fn without_it_the_grandchild_holds_the_pipe() {
    if std::env::var_os(ROLE).is_some() {
        return;
    }
    let took = eof_after_parent_exits(false);
    assert!(
        took >= SLEEP - Duration::from_secs(2),
        "EOF after {took:?}: the grandchild did not hold the pipe, so the other test proves nothing"
    );
}

/// 不是一条真的测试：被当成"父进程"或"孙进程"起时才干活。
#[test]
fn parent_role() {
    match std::env::var(ROLE).as_deref() {
        Ok("parent-stop") => {
            toexec_win::stop_std_handles_being_inherited();
            spawn_grandchild();
        }
        Ok("parent") => spawn_grandchild(),
        Ok("grandchild") => std::thread::sleep(SLEEP),
        _ => {}
    }
}

// Not waited for on purpose: the whole point is a grandchild that outlives
// the parent, the way a background command outlives the call that started it.
#[allow(clippy::zombie_processes)]
fn spawn_grandchild() {
    Command::new(std::env::current_exe().unwrap())
        .args(["--exact", "parent_role", "--test-threads=1"])
        .env(ROLE, "grandchild")
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .spawn()
        .unwrap();
}
