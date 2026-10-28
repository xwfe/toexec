//! Job Object：把一棵进程树装在一起，一次结束。

use std::ffi::c_void;
use std::io;
use std::os::windows::io::{AsRawHandle, FromRawHandle, OwnedHandle};

use crate::sys::{
    AssignProcessToJobObject, CreateJobObjectW, JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE,
    JOBOBJECT_BASIC_ACCOUNTING_INFORMATION, JOBOBJECT_EXTENDED_LIMIT_INFORMATION,
    JobObjectBasicAccountingInformation, JobObjectExtendedLimitInformation,
    QueryInformationJobObject, SetInformationJobObject, TerminateJobObject,
};

/// 一个 Job：放进去的进程，和它们之后起的所有进程，都在里面；结束它就是结束
/// 全部，它的句柄关掉（`Job` 被丢掉，或者持有它的进程不管怎么没的）也是。
///
/// **为什么要它，`taskkill /T` 不够：** taskkill 顺着父进程号找后代，父进程
/// 先退了的就找不到。Git for Windows 的 bash（MSYS2，底下是 Cygwin）每执行
/// 一个外部程序，都是另起一个 Windows 进程、原进程退出，所以 bash 起的
/// 每一个程序在 Windows 看来都没有活着的父进程——ccnm P110 在 CI 上实测，
/// `sleep 30 & sleep 30` 超时后 bash 被收掉，两个 `sleep.exe` 都还活着。
/// Job 不看父子关系：进程是在 Job 里的进程起的，就在 Job 里。
///
/// **不让成员脱离：** 不设 `JOB_OBJECT_LIMIT_BREAKAWAY_OK`，子进程带
/// `CREATE_BREAKAWAY_FROM_JOB` 起会失败，而不是悄悄跑出去。
///
/// **有一个空档：** 进程先起、后放进来（标准库的 `Command` 没法让进程挂起
/// 着起来再放），中间它自己起的进程会漏掉。放进来的那一步紧跟在 `spawn`
/// 后面，是几微秒的事，而一个程序起到能再起别的程序要几毫秒。
#[derive(Debug)]
pub struct Job {
    handle: OwnedHandle,
}

impl Job {
    /// 新建一个关掉句柄就结束全部成员的 Job（`JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`）。
    pub fn kill_on_close() -> io::Result<Job> {
        // SAFETY: 两个参数都给空指针：默认安全属性、不起名字。返回的句柄
        // 归这里所有，交给 OwnedHandle 负责关。
        let raw = unsafe { CreateJobObjectW(std::ptr::null_mut(), std::ptr::null()) };
        if raw.is_null() {
            return Err(io::Error::last_os_error());
        }
        // SAFETY: `raw` 是刚建出来、非空、没有别人持有的句柄。
        let job = Job {
            handle: unsafe { OwnedHandle::from_raw_handle(raw) },
        };
        let mut limits = JOBOBJECT_EXTENDED_LIMIT_INFORMATION::default();
        limits.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
        // SAFETY: 句柄有效；指针指向本函数里的结构体，长度就是它的大小，调用
        // 期间一直有效。
        let ok = unsafe {
            SetInformationJobObject(
                job.handle.as_raw_handle(),
                JobObjectExtendedLimitInformation,
                (&raw mut limits).cast::<c_void>(),
                size_of::<JOBOBJECT_EXTENDED_LIMIT_INFORMATION>() as u32,
            )
        };
        if ok == 0 {
            return Err(io::Error::last_os_error());
        }
        Ok(job)
    }

    /// 把一个进程放进来，比如刚 `spawn` 出来的 `std::process::Child`。进程
    /// 已经退了、或者已经在一个不允许嵌套的 Job 里时失败。
    pub fn assign(&self, process: &impl AsRawHandle) -> io::Result<()> {
        // SAFETY: 两个句柄在调用期间都有效：Job 归 self，进程句柄归调用方
        // 借给我们的那个对象。
        let ok = unsafe {
            AssignProcessToJobObject(self.handle.as_raw_handle(), process.as_raw_handle())
        };
        if ok == 0 {
            return Err(io::Error::last_os_error());
        }
        Ok(())
    }

    /// 结束里面所有进程，退出码 1。不等它们真的没了，要等就看
    /// [`active_processes`](Self::active_processes)。
    pub fn terminate(&self) -> io::Result<()> {
        // SAFETY: 句柄有效。
        let ok = unsafe { TerminateJobObject(self.handle.as_raw_handle(), 1) };
        if ok == 0 {
            return Err(io::Error::last_os_error());
        }
        Ok(())
    }

    /// 里面还有几个活着的进程。
    pub fn active_processes(&self) -> io::Result<u32> {
        let mut info = JOBOBJECT_BASIC_ACCOUNTING_INFORMATION::default();
        // SAFETY: 句柄有效；指针指向本函数里的结构体，长度是它的大小；不要
        // 返回长度，给空指针。
        let ok = unsafe {
            QueryInformationJobObject(
                self.handle.as_raw_handle(),
                JobObjectBasicAccountingInformation,
                (&raw mut info).cast::<c_void>(),
                size_of::<JOBOBJECT_BASIC_ACCOUNTING_INFORMATION>() as u32,
                std::ptr::null_mut(),
            )
        };
        if ok == 0 {
            return Err(io::Error::last_os_error());
        }
        Ok(info.ActiveProcesses)
    }
}
