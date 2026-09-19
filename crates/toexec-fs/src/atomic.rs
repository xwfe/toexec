use std::fs::{self, File, Permissions};
use std::io::{self, Write};
use std::path::Path;

/// [`write_durable`] 是在哪一步失败的。
///
/// 报出来，是因为调用方对这几步的看法不一样：建不出文件通常是路径或权限
/// 的事，调用方改得了；刷盘失败是机器的事，改不了。ccnm 就按这个分界把
/// 前两步归成「参数问题」、后两步归成「内部错误」，两种错误码在它的 MCP
/// 协议里含义不同。共享库不替谁做这个判断，只把事实说清楚。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Step {
    /// 建文件（或截断一个已有的）。
    Create,
    /// 写内容。
    Write,
    /// 刷到盘上。
    Sync,
    /// 设权限。
    Permissions,
}

impl Step {
    /// 一句话说明这一步在干什么，给调用方拼错误消息用。
    pub fn as_str(self) -> &'static str {
        match self {
            Step::Create => "create",
            Step::Write => "write",
            Step::Sync => "flush",
            Step::Permissions => "set permissions on",
        }
    }
}

/// 写失败了，以及是哪一步。
#[derive(Debug)]
pub struct WriteError {
    pub step: Step,
    pub source: io::Error,
}

impl std::fmt::Display for WriteError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "cannot {} the file: {}", self.step.as_str(), self.source)
    }
}

impl std::error::Error for WriteError {
    fn source(&self) -> Option<&(dyn std::error::Error + 'static)> {
        Some(&self.source)
    }
}

impl From<WriteError> for io::Error {
    fn from(e: WriteError) -> io::Error {
        e.source
    }
}

/// 把 `bytes` 写进 `path`，**内容落盘之后才返回**；给了 `mode` 就设权限。
///
/// 为什么要 fsync：少了它，后面那次 `rename` 可能先持久化、内容还没有。
/// 断电之后留下的就是一个名字对、长度错的文件——比写失败更难发现，因为
/// 它看起来是成功的。
///
/// 为什么要传权限：补丁不该让一个脚本丢掉可执行位。调用方把目标原来的
/// 权限读出来传进来；不传就用新建文件的默认权限。
///
/// 目标已存在时会被截断重写。这个函数只管写一个文件，不管它是不是临时
/// 文件——临时文件的命名和清理是调用方的事。
pub fn write_durable(
    path: &Path,
    bytes: &[u8],
    mode: Option<&Permissions>,
) -> Result<(), WriteError> {
    let fail = |step: Step| move |source: io::Error| WriteError { step, source };
    let mut file = File::create(path).map_err(fail(Step::Create))?;
    file.write_all(bytes).map_err(fail(Step::Write))?;
    file.sync_all().map_err(fail(Step::Sync))?;
    drop(file);
    if let Some(mode) = mode {
        fs::set_permissions(path, mode.clone()).map_err(fail(Step::Permissions))?;
    }
    Ok(())
}

/// 一次 `rename` 把 `temp` 顶替成 `target`。
///
/// 一次，所以中间没有「目标不存在」的瞬间，也不会在目标的名字上留下一个
/// 写了一半的文件。两个路径必须在同一个文件系统上——通常的做法是把临时
/// 文件建在目标的同一个目录里。
///
/// **不可退让的一条：替换失败，旧目标原样还在。** 所以这里没有任何「先
/// 把目标删掉再试一次」的兜底：`rename` 失败就直接把错误交出去，磁盘上
/// 还是旧内容，临时文件也还在（清不清是调用方的事）。
///
/// Windows 走的是同一行代码。std 的 `rename` 文档写的是「目标已存在就
/// 顶替掉」，实现是 `MoveFileExW(.., MOVEFILE_REPLACE_EXISTING)`，撞上
/// `ERROR_ACCESS_DENIED`（比如目标是只读文件）再用
/// `SetFileInformationByHandle` + `FileRenameInfoEx` 重试一次；本库 MSRV
/// 1.89 前后的 1.85 和 1.98 源码都是这样。**这里以前写着「Windows 上
/// rename 到已存在的文件会失败」并因此先 `remove_file` 一次，那句话针对
/// 的是 C 的 `rename()` 和不带 flag 的 `MoveFileW`，对 std 不成立；而先
/// 删的写法一旦后面的 rename 再失败，旧文件就真没了。**
///
/// 仍然成立的边界，别当成「所有文件系统上都原子安全」：
///
/// - 目标正被别人以不允许删除的方式打开（Windows 上没带
///   `FILE_SHARE_DELETE`）时替换会失败。失败即不动旧文件，符合上面那条。
/// - Windows 上 `FileRenameInfoEx` 要 Windows 10 1607 以上、且文件系统
///   支持；不支持时走 `MoveFileExW`，那条路上 `target` 不能是目录。
/// - 「rename 这件事」在断电后是否可见，仍取决于有没有 fsync 父目录——
///   这里没做，见 crate 文档。
pub fn replace(temp: &Path, target: &Path) -> io::Result<()> {
    fs::rename(temp, target)
}

#[cfg(test)]
mod tests {
    use super::*;

    /// 临时目录，Drop 时删掉。这个 crate 不带 dev-dependencies，所以不用
    /// tempfile；测试要的只是一个没人碰的目录。
    struct Dir(std::path::PathBuf);

    impl Dir {
        fn new(name: &str) -> Self {
            let mut path = std::env::temp_dir();
            path.push(format!(
                "toexec-fs-{name}-{}-{:?}",
                std::process::id(),
                std::thread::current().id()
            ));
            let _ = fs::remove_dir_all(&path);
            fs::create_dir_all(&path).expect("建临时目录");
            Dir(path)
        }
        fn join(&self, name: &str) -> std::path::PathBuf {
            self.0.join(name)
        }
    }

    impl Drop for Dir {
        fn drop(&mut self) {
            let _ = fs::remove_dir_all(&self.0);
        }
    }

    #[test]
    fn what_was_written_is_what_comes_back() {
        let dir = Dir::new("write");
        let path = dir.join("a.txt");
        write_durable(&path, b"hello", None).expect("写");
        assert_eq!(fs::read(&path).expect("读"), b"hello");
    }

    #[test]
    fn writing_over_an_existing_file_truncates_it() {
        let dir = Dir::new("truncate");
        let path = dir.join("a.txt");
        fs::write(&path, b"a much longer original").expect("先写一份");
        write_durable(&path, b"short", None).expect("写");
        assert_eq!(fs::read(&path).expect("读"), b"short");
    }

    /// 补丁不该让一个脚本丢掉可执行位。
    #[cfg(unix)]
    #[test]
    fn the_mode_that_was_asked_for_is_the_mode_on_disk() {
        use std::os::unix::fs::PermissionsExt;
        let dir = Dir::new("mode");
        let path = dir.join("run.sh");
        write_durable(&path, b"#!/bin/sh\n", Some(&Permissions::from_mode(0o755))).expect("写");
        let got = fs::metadata(&path).expect("stat").permissions().mode();
        assert_eq!(got & 0o777, 0o755, "实际是 {:o}", got & 0o777);
    }

    #[cfg(unix)]
    #[test]
    fn without_a_mode_the_file_keeps_the_default_one() {
        use std::os::unix::fs::PermissionsExt;
        let dir = Dir::new("nomode");
        let path = dir.join("a.txt");
        write_durable(&path, b"x", None).expect("写");
        let got = fs::metadata(&path).expect("stat").permissions().mode();
        assert_eq!(got & 0o111, 0, "默认不该带可执行位，实际 {:o}", got & 0o777);
    }

    #[test]
    fn replacing_leaves_the_new_content_under_the_old_name() {
        let dir = Dir::new("replace");
        let target = dir.join("a.txt");
        let temp = dir.join(".tmp-a");
        fs::write(&target, b"old").expect("原文件");
        write_durable(&temp, b"new", None).expect("写临时");
        replace(&temp, &target).expect("替换");
        assert_eq!(fs::read(&target).expect("读"), b"new");
        assert!(!temp.exists(), "临时文件应该已经被 rename 走");
    }

    #[test]
    fn replacing_a_file_that_is_not_there_yet_also_works() {
        let dir = Dir::new("replace-new");
        let target = dir.join("a.txt");
        let temp = dir.join(".tmp-a");
        write_durable(&temp, b"new", None).expect("写临时");
        replace(&temp, &target).expect("替换");
        assert_eq!(fs::read(&target).expect("读"), b"new");
    }

    /// 替换时原文件的权限跟着临时文件走，不是跟着目标走——所以调用方必须
    /// 在 `write_durable` 那一步就把原权限传进来。这条把它钉死。
    #[cfg(unix)]
    #[test]
    fn the_replaced_file_has_the_temps_mode_not_the_targets() {
        use std::os::unix::fs::PermissionsExt;
        let dir = Dir::new("replace-mode");
        let target = dir.join("run.sh");
        let temp = dir.join(".tmp-run");
        fs::write(&target, b"old").expect("原文件");
        fs::set_permissions(&target, Permissions::from_mode(0o755)).expect("设原权限");

        write_durable(&temp, b"new", None).expect("写临时，没传权限");
        replace(&temp, &target).expect("替换");
        let got = fs::metadata(&target).expect("stat").permissions().mode();
        assert_eq!(got & 0o111, 0, "可执行位本来就会丢，实际 {:o}", got & 0o777);

        // 传了原权限就不会丢。
        let temp = dir.join(".tmp-run2");
        write_durable(&temp, b"newer", Some(&Permissions::from_mode(0o755))).expect("写临时");
        replace(&temp, &target).expect("替换");
        let got = fs::metadata(&target).expect("stat").permissions().mode();
        assert_eq!(got & 0o777, 0o755, "实际是 {:o}", got & 0o777);
    }

    #[test]
    fn a_write_that_cannot_happen_reports_why() {
        let dir = Dir::new("nodir");
        let path = dir.join("missing-dir").join("a.txt");
        let err = write_durable(&path, b"x", None).expect_err("父目录不存在，应该失败");
        assert_eq!(err.step, Step::Create, "{err}");
        assert_eq!(err.source.kind(), io::ErrorKind::NotFound, "{err}");
    }

    #[test]
    fn replacing_from_a_temp_that_is_not_there_reports_why() {
        let dir = Dir::new("notemp");
        let err =
            replace(&dir.join(".gone"), &dir.join("a.txt")).expect_err("临时文件不存在，应该失败");
        assert_eq!(err.kind(), io::ErrorKind::NotFound, "{err}");
    }

    /// 这条是 X01 的回归：**替换失败，旧目标必须原样还在。**
    ///
    /// 临时文件不在（调用方没写出来、或者被谁清掉了）是最容易构造的失败。
    /// 以前 Windows 分支会先 `remove_file(target)`——那一步成功，后面的
    /// rename 再失败，旧文件就没了。现在只有一次 rename，失败即什么都没动。
    #[test]
    fn a_failed_replace_leaves_the_old_target_and_does_not_eat_it() {
        let dir = Dir::new("keepold");
        let target = dir.join("a.txt");
        fs::write(&target, b"old").expect("原文件");

        let err = replace(&dir.join(".gone"), &target).expect_err("临时文件不存在，应该失败");
        assert_eq!(err.kind(), io::ErrorKind::NotFound, "{err}");
        assert!(target.exists(), "旧目标被吃掉了");
        assert_eq!(fs::read(&target).expect("读"), b"old", "旧内容被动过");
    }

    /// 只读的目标也要能被替换掉。
    ///
    /// 补丁的目标文件带只读属性不算稀奇（Windows 上 `attrib +R`、Unix 上
    /// 0o444）。以前 Windows 分支要先 `remove_file`，而 Windows 删只读文件
    /// 会 `ERROR_ACCESS_DENIED`，替换直接失败；现在 std 的 `rename` 撞到
    /// 这个错误会用 `FileRenameInfoEx` 重试，替换得以完成。Unix 上 rename
    /// 看的本来就是父目录的权限，目标自己只读没关系。
    #[test]
    fn a_read_only_target_still_gets_replaced() {
        let dir = Dir::new("readonly");
        let target = dir.join("a.txt");
        let temp = dir.join(".tmp-a");
        fs::write(&target, b"old").expect("原文件");
        let mut perms = fs::metadata(&target).expect("stat").permissions();
        perms.set_readonly(true);
        fs::set_permissions(&target, perms).expect("设成只读");

        write_durable(&temp, b"new", None).expect("写临时");
        let result = replace(&temp, &target);

        // 断言之前先把只读摘掉：万一这条在某个 Windows 文件系统上挂了，
        // Dir::drop 还得删得掉这个目录——那里删只读文件会 ACCESS_DENIED，
        // 不然临时目录里就留一份垃圾。Unix 不用，删文件看的是父目录权限。
        #[cfg(windows)]
        {
            #[allow(
                clippy::permissions_set_readonly_false,
                reason = "测试自己的临时目录，摘掉只读只是为了能删干净"
            )]
            if let Ok(meta) = fs::metadata(&target) {
                let mut perms = meta.permissions();
                perms.set_readonly(false);
                let _ = fs::set_permissions(&target, perms);
            }
        }

        if let Err(e) = result {
            panic!(
                "只读目标也该能替换，实际 {e}（raw os error {:?}）",
                e.raw_os_error()
            );
        }
        assert_eq!(fs::read(&target).expect("读"), b"new");
    }

    /// 目标被别人打开着、而且不许删（Windows 上没带 `FILE_SHARE_DELETE`）：
    /// 替换失败，旧内容一个字节都不能变。Unix 上没有这种占用语义——那里
    /// 打开的是 inode，rename 照样成功——所以这条只在 Windows 跑。
    #[cfg(windows)]
    #[test]
    fn a_target_held_open_without_share_delete_fails_and_keeps_its_content() {
        use std::os::windows::fs::OpenOptionsExt;

        let dir = Dir::new("locked");
        let target = dir.join("a.txt");
        let temp = dir.join(".tmp-a");
        fs::write(&target, b"old").expect("原文件");
        write_durable(&temp, b"new", None).expect("写临时");

        // share_mode(0)：别的人既不能读也不能写，更不能删。
        let held = fs::OpenOptions::new()
            .read(true)
            .share_mode(0)
            .open(&target)
            .expect("占住目标");

        let result = replace(&temp, &target);
        drop(held);
        if let Err(e) = &result {
            // 失败是预期的，留个痕迹好看是哪个错误码（共享冲突还是拒绝访问）。
            println!("被占住的目标：{e}（raw os error {:?}）", e.raw_os_error());
        }
        assert!(
            result.is_err(),
            "目标被独占打开着，替换居然成功了——那 POSIX 语义的 rename 绕过了共享模式，得重新看这条契约"
        );
        assert_eq!(fs::read(&target).expect("读"), b"old", "旧内容被动过");
        assert!(temp.exists(), "临时文件被动了");
    }

    /// 目标是一个目录：rename 一个文件盖到目录上必须失败，而且必须是报错，
    /// 不能把目录删掉。顺带钉住「失败之后临时文件还在原地」——清理是调用
    /// 方的事（两个产品的清理编排不一样），这里不替谁删。
    #[test]
    fn replacing_a_directory_fails_and_leaves_it_alone() {
        let dir = Dir::new("targetdir");
        let target = dir.join("adir");
        fs::create_dir(&target).expect("建目录");
        fs::write(target.join("inside.txt"), b"keep me").expect("目录里放个文件");
        let temp = dir.join(".tmp");
        write_durable(&temp, b"new", None).expect("写临时");

        replace(&temp, &target).expect_err("不该盖掉一个目录");
        assert!(target.is_dir(), "目录没了");
        assert_eq!(
            fs::read(target.join("inside.txt")).expect("读"),
            b"keep me",
            "目录里的文件被动了"
        );
        assert!(temp.exists(), "临时文件被动了，调用方就没法重试或者回收");
    }
}
