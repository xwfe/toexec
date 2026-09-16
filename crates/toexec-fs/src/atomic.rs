use std::fs::{self, File, Permissions};
use std::io::{self, Write};
use std::path::Path;

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
pub fn write_durable(path: &Path, bytes: &[u8], mode: Option<&Permissions>) -> io::Result<()> {
    let mut file = File::create(path)?;
    file.write_all(bytes)?;
    file.sync_all()?;
    drop(file);
    if let Some(mode) = mode {
        fs::set_permissions(path, mode.clone())?;
    }
    Ok(())
}

/// 一次 `rename` 把 `temp` 顶替成 `target`。
///
/// 一次，所以中间没有「目标不存在」的瞬间，也不会在目标的名字上留下一个
/// 写了一半的文件。两个路径必须在同一个文件系统上——通常的做法是把临时
/// 文件建在目标的同一个目录里。
///
/// Windows 上 `rename` 到一个已存在的文件会失败，所以那里先删一次。这让
/// 替换在 Windows 上**不是**原子的：删和 rename 之间有一个窗口，目标不
/// 存在。std 没有提供跨平台的原子替换，要真做得调 `ReplaceFileW`。
pub fn replace(temp: &Path, target: &Path) -> io::Result<()> {
    #[cfg(windows)]
    {
        if target.exists() {
            fs::remove_file(target)?;
        }
    }
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
        assert_eq!(err.kind(), io::ErrorKind::NotFound, "{err}");
    }

    #[test]
    fn replacing_from_a_temp_that_is_not_there_reports_why() {
        let dir = Dir::new("notemp");
        let err =
            replace(&dir.join(".gone"), &dir.join("a.txt")).expect_err("临时文件不存在，应该失败");
        assert_eq!(err.kind(), io::ErrorKind::NotFound, "{err}");
    }

    /// 目标是一个目录：rename 一个文件盖到目录上必须失败，而且必须是报错，
    /// 不能把目录删掉。
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
    }
}
