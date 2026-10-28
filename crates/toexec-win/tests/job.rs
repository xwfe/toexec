//! Job Object 管住了 `taskkill /T` 管不住的那种进程：父进程起完它就退了，
//! 在 Windows 看来它没有活着的父进程。Git for Windows 的 bash 起的每个外部
//! 程序都是这样（ccnm P110 在 CI 上实测）。
//!
//! 三代进程都是这个测试程序自己，靠环境变量 `TOEXEC_WIN_JOB_ROLE` 分角色：
//! "父进程"起一个睡 20 秒、拿着标准输出的"孙进程"就退出；测试把"父进程"
//! 放进 Job，然后结束或丢掉 Job，看孙进程还在不在。
#![cfg(windows)]

use std::io::Read;
use std::process::{Command, Stdio};
use std::time::{Duration, Instant};

use toexec_win::Job;

const ROLE: &str = "TOEXEC_WIN_JOB_ROLE";

/// 起"父进程"，马上放进一个新 Job；等它退出（孙进程已经起好）。返回 Job
/// 和父进程的标准输出——孙进程拿着它，读到结束就说明孙进程没了。
fn orphaned_grandchild() -> (Job, std::process::ChildStdout) {
    let job = Job::kill_on_close().unwrap();
    let mut parent = Command::new(std::env::current_exe().unwrap())
        .args(["--exact", "job_roles", "--nocapture", "--test-threads=1"])
        .env(ROLE, "parent")
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::null())
        .spawn()
        .unwrap();
    job.assign(&parent).unwrap();
    let stdout = parent.stdout.take().unwrap();
    parent.wait().unwrap();
    // The grandchild is all that is left in it.
    let deadline = Instant::now() + Duration::from_secs(10);
    while job.active_processes().unwrap() != 1 {
        assert!(
            Instant::now() < deadline,
            "the grandchild never ran alone in the job"
        );
        std::thread::sleep(Duration::from_millis(50));
    }
    (job, stdout)
}

fn eof_within(mut stdout: std::process::ChildStdout, limit: Duration) -> Duration {
    let started = Instant::now();
    let mut rest = Vec::new();
    stdout.read_to_end(&mut rest).unwrap();
    let took = started.elapsed();
    assert!(
        took < limit,
        "the grandchild still held the pipe after {took:?}"
    );
    took
}

#[test]
fn terminating_the_job_ends_a_grandchild_whose_parent_is_gone() {
    if std::env::var_os(ROLE).is_some() {
        return;
    }
    let (job, stdout) = orphaned_grandchild();
    job.terminate().unwrap();
    eof_within(stdout, Duration::from_secs(5));
    assert_eq!(job.active_processes().unwrap(), 0);
}

#[test]
fn dropping_the_job_ends_it_too() {
    if std::env::var_os(ROLE).is_some() {
        return;
    }
    let (job, stdout) = orphaned_grandchild();
    drop(job);
    eof_within(stdout, Duration::from_secs(5));
}

/// 不是一条真的测试：被当成"父进程"或"孙进程"起时才干活。
#[test]
fn job_roles() {
    match std::env::var(ROLE).as_deref() {
        Ok("parent") => {
            // Not waited for: a grandchild that outlives its parent is the
            // whole point. Its stdout is the parent's, so the test's pipe.
            #[allow(clippy::zombie_processes)]
            Command::new(std::env::current_exe().unwrap())
                .args(["--exact", "job_roles", "--test-threads=1"])
                .env(ROLE, "grandchild")
                .stdin(Stdio::null())
                .stderr(Stdio::null())
                .spawn()
                .unwrap();
        }
        Ok("grandchild") => std::thread::sleep(Duration::from_secs(20)),
        _ => {}
    }
}
