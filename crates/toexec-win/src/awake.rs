//! 会话期间请求系统别睡。

use std::marker::PhantomData;

use crate::sys::{ES_CONTINUOUS, ES_SYSTEM_REQUIRED, SetThreadExecutionState};

/// 持有它的这段时间里，系统不因为空闲而睡；它被丢掉时撤回请求。
///
/// **为什么要：** 接着电源的 Windows 机器默认空闲一段时间就睡（ccnm 的测试机
/// 是 10 分钟），而经 ssh 进来的流量不算"在用"。一次长编译、一个等着的后台
/// 命令，跑到一半机器睡了，连接跟着断。ccnm P106 实测：从 sshd 会话里起的
/// 进程这样请求是认的，跑满一小时没睡。
///
/// **只请求，不改设置：** 不碰电源计划、不关显示器（只要
/// `ES_SYSTEM_REQUIRED`，不要 `ES_DISPLAY_REQUIRED`），请求一撤，系统照它
/// 原来的设置算空闲——可能马上就睡，这正是想要的。
///
/// **在哪个线程请求，就在哪个线程撤：** 这个状态是按线程记的，线程结束请求
/// 也就没了。所以它不能挪到别的线程（不是 `Send`），持有它的线程要活得和
/// 请求一样久——通常就是主线程，从会话开始拿到会话结束。
#[derive(Debug)]
pub struct KeepAwake {
    _same_thread: PhantomData<*const ()>,
}

impl KeepAwake {
    /// 请求别睡。
    ///
    /// 不报错：这个函数用返回 0 表示失败，可一个从没设过状态的线程，"之前的
    /// 状态"本来也可能是 0，两者分不开；而且失败了调用方也做不了什么，最坏
    /// 是机器照原来的设置睡。
    pub fn request() -> KeepAwake {
        // SAFETY: 只传标志位，不涉及指针。
        unsafe {
            SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED);
        }
        KeepAwake {
            _same_thread: PhantomData,
        }
    }
}

impl Drop for KeepAwake {
    fn drop(&mut self) {
        // SAFETY: 同上。只带 ES_CONTINUOUS 就是撤回本线程之前的请求。
        unsafe {
            SetThreadExecutionState(ES_CONTINUOUS);
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// 这个线程现在的状态。问的办法只有再设一次：设成"什么都不要"并拿回
    /// 之前的，再原样设回去。
    fn current() -> u32 {
        // SAFETY: 只传标志位。
        let state = unsafe { SetThreadExecutionState(ES_CONTINUOUS) };
        // SAFETY: 同上。
        unsafe { SetThreadExecutionState(state) };
        state
    }

    #[test]
    fn the_request_holds_while_kept_and_goes_with_it() {
        std::thread::spawn(|| {
            let before = current();
            assert_eq!(before & ES_SYSTEM_REQUIRED, 0, "{before:#x}");
            let awake = KeepAwake::request();
            assert_eq!(current(), ES_CONTINUOUS | ES_SYSTEM_REQUIRED);
            drop(awake);
            assert_eq!(current() & ES_SYSTEM_REQUIRED, 0);
        })
        .join()
        .unwrap();
    }
}
