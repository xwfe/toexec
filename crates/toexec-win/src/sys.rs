//! 用到的 kernel32 函数，照 Microsoft 文档里的签名自己声明。
//!
//! `HANDLE` 就是标准库的 `RawHandle`（`*mut c_void`），`BOOL` 是 `i32`，
//! `EXECUTION_STATE` 是 `u32`。

use std::ffi::c_void;

/// `HANDLE_FLAG_INHERIT`：句柄能不能被子进程继承。
pub(crate) const HANDLE_FLAG_INHERIT: u32 = 0x0000_0001;

/// `ES_SYSTEM_REQUIRED`：系统别因为空闲而睡。不管显示器。
pub(crate) const ES_SYSTEM_REQUIRED: u32 = 0x0000_0001;
/// `ES_CONTINUOUS`：这次设的状态一直有效，直到下一次带它的调用。
pub(crate) const ES_CONTINUOUS: u32 = 0x8000_0000;

#[link(name = "kernel32")]
unsafe extern "system" {
    /// <https://learn.microsoft.com/en-us/windows/win32/api/handleapi/nf-handleapi-sethandleinformation>
    pub(crate) fn SetHandleInformation(handle: *mut c_void, mask: u32, flags: u32) -> i32;
    /// <https://learn.microsoft.com/en-us/windows/win32/api/handleapi/nf-handleapi-gethandleinformation>
    #[cfg_attr(not(test), allow(dead_code))]
    pub(crate) fn GetHandleInformation(handle: *mut c_void, flags: *mut u32) -> i32;
    /// <https://learn.microsoft.com/en-us/windows/win32/api/winbase/nf-winbase-setthreadexecutionstate>
    ///
    /// 返回调用之前的状态；失败返回 0。
    pub(crate) fn SetThreadExecutionState(flags: u32) -> u32;
}
