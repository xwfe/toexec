//! 用到的 kernel32 函数和结构体，照 Microsoft 文档里的签名自己声明。
//!
//! `HANDLE` 就是标准库的 `RawHandle`（`*mut c_void`），`BOOL` 是 `i32`，
//! `EXECUTION_STATE` 是 `u32`，`JOBOBJECTINFOCLASS` 是 `i32`。结构体和字段
//! 用文档里的原名，好对照。

#![allow(non_camel_case_types, non_snake_case, non_upper_case_globals)]

use std::ffi::c_void;

/// `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`：最后一个句柄关掉时结束全部成员。
pub(crate) const JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE: u32 = 0x0000_2000;
/// `JOBOBJECTINFOCLASS` 里用到的两个。
pub(crate) const JobObjectBasicAccountingInformation: i32 = 1;
pub(crate) const JobObjectExtendedLimitInformation: i32 = 9;

/// <https://learn.microsoft.com/en-us/windows/win32/api/winnt/ns-winnt-jobobject_basic_limit_information>
#[repr(C)]
#[derive(Default)]
#[allow(dead_code)] // Laid out for Windows to fill; most fields are not read here.
pub(crate) struct JOBOBJECT_BASIC_LIMIT_INFORMATION {
    pub PerProcessUserTimeLimit: i64,
    pub PerJobUserTimeLimit: i64,
    pub LimitFlags: u32,
    pub MinimumWorkingSetSize: usize,
    pub MaximumWorkingSetSize: usize,
    pub ActiveProcessLimit: u32,
    pub Affinity: usize,
    pub PriorityClass: u32,
    pub SchedulingClass: u32,
}

/// <https://learn.microsoft.com/en-us/windows/win32/api/winnt/ns-winnt-io_counters>
#[repr(C)]
#[derive(Default)]
#[allow(dead_code)] // Laid out for Windows to fill; most fields are not read here.
pub(crate) struct IO_COUNTERS {
    pub ReadOperationCount: u64,
    pub WriteOperationCount: u64,
    pub OtherOperationCount: u64,
    pub ReadTransferCount: u64,
    pub WriteTransferCount: u64,
    pub OtherTransferCount: u64,
}

/// <https://learn.microsoft.com/en-us/windows/win32/api/winnt/ns-winnt-jobobject_extended_limit_information>
#[repr(C)]
#[derive(Default)]
#[allow(dead_code)] // Laid out for Windows to fill; most fields are not read here.
pub(crate) struct JOBOBJECT_EXTENDED_LIMIT_INFORMATION {
    pub BasicLimitInformation: JOBOBJECT_BASIC_LIMIT_INFORMATION,
    pub IoInfo: IO_COUNTERS,
    pub ProcessMemoryLimit: usize,
    pub JobMemoryLimit: usize,
    pub PeakProcessMemoryUsed: usize,
    pub PeakJobMemoryUsed: usize,
}

/// <https://learn.microsoft.com/en-us/windows/win32/api/winnt/ns-winnt-jobobject_basic_accounting_information>
#[repr(C)]
#[derive(Default)]
#[allow(dead_code)] // Laid out for Windows to fill; most fields are not read here.
pub(crate) struct JOBOBJECT_BASIC_ACCOUNTING_INFORMATION {
    pub TotalUserTime: i64,
    pub TotalKernelTime: i64,
    pub ThisPeriodTotalUserTime: i64,
    pub ThisPeriodTotalKernelTime: i64,
    pub TotalPageFaultCount: u32,
    pub TotalProcesses: u32,
    pub ActiveProcesses: u32,
    pub TotalTerminatedProcesses: u32,
}

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
    /// <https://learn.microsoft.com/en-us/windows/win32/api/jobapi2/nf-jobapi2-createjobobjectw>
    ///
    /// 失败返回空指针。
    pub(crate) fn CreateJobObjectW(attributes: *mut c_void, name: *const u16) -> *mut c_void;
    /// <https://learn.microsoft.com/en-us/windows/win32/api/jobapi2/nf-jobapi2-setinformationjobobject>
    pub(crate) fn SetInformationJobObject(
        job: *mut c_void,
        class: i32,
        info: *mut c_void,
        length: u32,
    ) -> i32;
    /// <https://learn.microsoft.com/en-us/windows/win32/api/jobapi2/nf-jobapi2-queryinformationjobobject>
    pub(crate) fn QueryInformationJobObject(
        job: *mut c_void,
        class: i32,
        info: *mut c_void,
        length: u32,
        returned: *mut u32,
    ) -> i32;
    /// <https://learn.microsoft.com/en-us/windows/win32/api/jobapi2/nf-jobapi2-assignprocesstojobobject>
    pub(crate) fn AssignProcessToJobObject(job: *mut c_void, process: *mut c_void) -> i32;
    /// <https://learn.microsoft.com/en-us/windows/win32/api/jobapi2/nf-jobapi2-terminatejobobject>
    pub(crate) fn TerminateJobObject(job: *mut c_void, exit_code: u32) -> i32;
}
