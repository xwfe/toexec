//! 不让子进程继承本进程的标准句柄。

use std::os::windows::io::AsRawHandle;

use crate::sys::{HANDLE_FLAG_INHERIT, SetHandleInformation};

/// 之后起的子进程不再继承本进程的标准输入、输出、错误句柄。在 `main` 一开始
/// 调一次就够。
///
/// **为什么要做：** Windows 上 Rust 的 `Command` 起子进程时，会把父进程里
/// **所有**可继承的句柄都交出去，不只是给它指定的那三个
/// （rust-lang/rust#38227）。父进程的标准输出是一根管道时——经 ssh 跑的
/// MCP server、测试里的 `Command::output()`、脚本里的 `$(...)`——它起的
/// 后台子进程就拿走了这根管道的写端：父进程退出了，子进程不退，管道就不关，
/// 另一头永远等不到结束。gld 2026-09-25 第一次在 Windows CI 上真跑时就这样
/// 卡满了 20 分钟。
///
/// **不影响显式交给子进程的标准输入输出：** `Stdio::inherit()` 和管道，标准库
/// 都会另外复制一份可继承的句柄给子进程，所以清掉原句柄的继承位之后，照样
/// 能把它交给想交的子进程。
///
/// 经 Windows 自带 sshd 跑时这件事本来不致命：sshd 把会话放进一个
/// `KILL_ON_JOB_CLOSE` 的 Job，shell 一退出整棵树都结束（ccnm P106 实测）。
/// 没有那个 Job 的地方——CI、本机直接起——就得靠这里。
///
/// 某个句柄不存在或不能改（没有控制台、已被关掉）时跳过它：最坏是回到原来
/// 的行为，调用方对此也做不了什么，所以不报错。
pub fn stop_std_handles_being_inherited() {
    for handle in [
        std::io::stdin().as_raw_handle(),
        std::io::stdout().as_raw_handle(),
        std::io::stderr().as_raw_handle(),
    ] {
        if handle.is_null() {
            continue;
        }
        // SAFETY: `handle` 是本进程标准库持有的标准句柄，调用期间不会被关；
        // 只改它的继承标志，不读写它指向的对象。无效句柄时函数返回 0，不越界。
        unsafe {
            SetHandleInformation(handle, HANDLE_FLAG_INHERIT, 0);
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::sys::GetHandleInformation;

    fn inheritable(handle: std::os::windows::io::RawHandle) -> Option<bool> {
        let mut flags = 0u32;
        // SAFETY: 只读标志；`flags` 是本函数里的局部变量。
        let ok = unsafe { GetHandleInformation(handle, &mut flags) };
        (ok != 0).then_some(flags & HANDLE_FLAG_INHERIT != 0)
    }

    /// 一个本来可继承的句柄，调过之后不可继承。标准句柄在测试进程里不一定
    /// 可继承（取决于 cargo 怎么起的它），所以拿自己建的一个来验函数本身，
    /// 进程层面的效果在 `tests/inherit.rs`。
    #[test]
    fn the_flag_is_cleared_on_a_handle_that_had_it() {
        let file = std::fs::File::open(std::env::current_exe().unwrap()).unwrap();
        let handle = file.as_raw_handle();
        // SAFETY: 同上，只改本进程打开的这个文件句柄的继承标志。
        unsafe { SetHandleInformation(handle, HANDLE_FLAG_INHERIT, HANDLE_FLAG_INHERIT) };
        assert_eq!(inheritable(handle), Some(true));
        // SAFETY: 同上。
        unsafe { SetHandleInformation(handle, HANDLE_FLAG_INHERIT, 0) };
        assert_eq!(inheritable(handle), Some(false));

        stop_std_handles_being_inherited();
        for handle in [
            std::io::stdin().as_raw_handle(),
            std::io::stdout().as_raw_handle(),
            std::io::stderr().as_raw_handle(),
        ] {
            if let Some(flag) = inheritable(handle) {
                assert!(!flag, "a standard handle is still inheritable");
            }
        }
    }
}
