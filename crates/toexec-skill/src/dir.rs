//! skill 目录里的其他文件：列出来、读一个。
//!
//! 一个 skill 常常不止 `SKILL.md`：正文写"跑 `scripts/fill.py`"、"字段见
//! `reference.md`"。skill 装在项目外面（`~/.claude/skills`、`~/.agents/skills`）
//! 时，产品的读文件工具只许读项目，模型就只能看着正文干瞪眼；为此把读文件放开
//! 到整个主目录又太宽。这里给的是中间那条路：**只在这个 skill 自己的目录里读**。
//!
//! 规则（gld 的 `get_skill` 先这么做，ccnm 的 `load_skill` 跟上时抽到这里）：
//!
//! - 只收普通的相对路径：`..`、绝对路径、盘符都拒绝；
//! - 解析完（跟着符号链接走）还得在 skill 目录里面；
//! - 路上任何一段以 `.` 开头就不读，列表里也不出现——skill 目录里的 `.env`
//!   多半是给脚本用的密钥，不是给模型看的；
//! - 有大小上限，只收 UTF-8 文本。
//!
//! skill 目录本身可以是符号链接（`~/.claude/skills/x -> ~/.agents/skills/x`
//! 是 skills CLI 的标准装法），所以先把它解析成真实路径，再按真实路径圈范围。
//!
//! 分两步（[`resolve`] 再 [`read_text`]），是因为产品要在中间插自己的检查：
//! gld 在这里拒绝它自己的数据目录。

use std::fmt;
use std::io;
use std::path::{Component, Path, PathBuf};

/// [`list`] 最多列多少个文件、往下看几层。
pub const MAX_LISTED_FILES: usize = 100;
pub const MAX_LISTED_DEPTH: usize = 4;

/// skill 目录里有哪些文件：相对路径（用 `/` 分隔），不含顶层的 `SKILL.md`、
/// 不含点开头的文件和目录、不含符号链接本身，按目录深度优先、同层按名字排。
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct Listing {
    pub files: Vec<String>,
    /// 超过 [`MAX_LISTED_FILES`]，后面的没列。
    pub more: bool,
}

pub fn list(dir: &Path) -> Listing {
    let mut listing = Listing::default();
    walk(dir, "", 1, &mut listing);
    listing
}

fn walk(dir: &Path, prefix: &str, depth: usize, out: &mut Listing) {
    if depth > MAX_LISTED_DEPTH || out.more {
        return;
    }
    let Ok(entries) = std::fs::read_dir(dir) else {
        return;
    };
    let mut entries: Vec<_> = entries.flatten().collect();
    entries.sort_by_key(|entry| entry.file_name());
    for entry in entries {
        let Some(name) = entry.file_name().to_str().map(str::to_string) else {
            continue;
        };
        if name.starts_with('.') {
            continue;
        }
        let Ok(kind) = entry.file_type() else {
            continue;
        };
        let relative = format!("{prefix}{name}");
        if kind.is_dir() {
            walk(&entry.path(), &format!("{relative}/"), depth + 1, out);
        } else if kind.is_file() && relative != "SKILL.md" {
            if out.files.len() == MAX_LISTED_FILES {
                out.more = true;
                return;
            }
            out.files.push(relative);
        }
        if out.more {
            return;
        }
    }
}

/// 一个文件为什么没读。措辞和错误码由产品定，这里只报是哪一种。
#[derive(Debug)]
pub enum ReadError {
    /// 不是普通的相对路径：带 `..`、绝对路径、盘符。
    NotRelative,
    /// 解析（跟着符号链接走）之后跑到 skill 目录外面了。
    Outside,
    /// 路上有一段以 `.` 开头。
    Hidden,
    NotFound,
    /// 是目录或别的什么，不是普通文件。
    NotAFile,
    TooLarge {
        size: u64,
        max: u64,
    },
    /// 不是 UTF-8。
    NotText,
    Io(io::Error),
}

impl fmt::Display for ReadError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::NotRelative => {
                f.write_str("not a plain relative path inside the skill's directory")
            }
            Self::Outside => f.write_str("leads outside the skill's directory"),
            Self::Hidden => f.write_str("is a hidden file"),
            Self::NotFound => f.write_str("does not exist in the skill's directory"),
            Self::NotAFile => f.write_str("is not a file"),
            Self::TooLarge { size, max } => write!(f, "is {size} bytes; the limit is {max}"),
            Self::NotText => f.write_str("is not text (not UTF-8)"),
            Self::Io(e) => write!(f, "could not be read: {e}"),
        }
    }
}

impl std::error::Error for ReadError {}

/// `relative` 在 `dir` 里对应的真实路径，过了"在目录里、不是点文件"两关。
/// 不看它是不是文件、有多大：那是 [`read_text`] 的事。
pub fn resolve(dir: &Path, relative: &str) -> Result<PathBuf, ReadError> {
    let plain = !relative.is_empty()
        && Path::new(relative)
            .components()
            .all(|part| matches!(part, Component::Normal(_) | Component::CurDir));
    if !plain {
        return Err(ReadError::NotRelative);
    }
    let dir = dir.canonicalize().map_err(|_| ReadError::NotFound)?;
    let resolved = dir
        .join(relative)
        .canonicalize()
        .map_err(|_| ReadError::NotFound)?;
    let Ok(rest) = resolved.strip_prefix(&dir) else {
        return Err(ReadError::Outside);
    };
    // 看的是解析之后的路径：指向 `.secret` 的 `visible.txt` 也算点文件。
    if rest
        .components()
        .any(|part| part.as_os_str().to_string_lossy().starts_with('.'))
    {
        return Err(ReadError::Hidden);
    }
    Ok(resolved)
}

/// 读一个已经 [`resolve`] 过的文件：必须是普通文件、不超过 `max` 字节、是 UTF-8。
pub fn read_text(path: &Path, max: u64) -> Result<String, ReadError> {
    let meta = std::fs::metadata(path).map_err(ReadError::Io)?;
    if !meta.is_file() {
        return Err(ReadError::NotAFile);
    }
    if meta.len() > max {
        return Err(ReadError::TooLarge {
            size: meta.len(),
            max,
        });
    }
    // 看过大小之后文件还可能在长：读的时候也只读到上限多一个字节。
    let mut bytes = Vec::new();
    std::fs::File::open(path)
        .and_then(|file| io::Read::read_to_end(&mut io::Read::take(file, max + 1), &mut bytes))
        .map_err(ReadError::Io)?;
    if bytes.len() as u64 > max {
        return Err(ReadError::TooLarge {
            size: bytes.len() as u64,
            max,
        });
    }
    String::from_utf8(bytes).map_err(|_| ReadError::NotText)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs;

    /// 测试目录：跑完就删。
    struct Temp(PathBuf);

    impl Temp {
        fn new(name: &str) -> Self {
            let dir = std::env::temp_dir()
                .join(format!("toexec-skill-dir-{}-{name}", std::process::id()));
            let _ = fs::remove_dir_all(&dir);
            fs::create_dir_all(&dir).unwrap();
            Self(dir.canonicalize().unwrap())
        }

        fn write(&self, rel: &str, text: &[u8]) {
            let path = self.0.join(rel);
            fs::create_dir_all(path.parent().unwrap()).unwrap();
            fs::write(path, text).unwrap();
        }
    }

    impl Drop for Temp {
        fn drop(&mut self) {
            let _ = fs::remove_dir_all(&self.0);
        }
    }

    #[test]
    fn the_listing_leaves_out_the_skill_file_hidden_names_and_links() {
        let t = Temp::new("list");
        let skill = t.0.join("skill");
        for rel in [
            "skill/SKILL.md",
            "skill/reference.md",
            "skill/scripts/run.sh",
            "skill/scripts/lib/util.py",
            "skill/.env",
            "skill/.cache/x",
            "skill/docs/SKILL.md",
            "skill/a/b/c/d/too-deep.txt",
            "skill/a/b/c/ok.txt",
        ] {
            t.write(rel, b"x");
        }
        #[cfg(unix)]
        std::os::unix::fs::symlink(t.0.join("skill/reference.md"), skill.join("link.md")).unwrap();
        let listing = list(&skill);
        assert_eq!(
            listing.files,
            [
                "a/b/c/ok.txt",
                "docs/SKILL.md",
                "reference.md",
                "scripts/lib/util.py",
                "scripts/run.sh"
            ]
        );
        assert!(!listing.more);
    }

    #[test]
    fn the_listing_stops_at_the_limit_and_says_so() {
        let t = Temp::new("many");
        for n in 0..(MAX_LISTED_FILES + 5) {
            t.write(&format!("f{n:03}.txt"), b"x");
        }
        let listing = list(&t.0);
        assert_eq!(listing.files.len(), MAX_LISTED_FILES);
        assert!(listing.more);
        assert_eq!(listing.files[0], "f000.txt");
    }

    #[test]
    fn only_plain_files_inside_the_directory_are_read() {
        let t = Temp::new("read");
        t.write("skill/SKILL.md", b"body");
        t.write("skill/scripts/run.sh", b"echo hi\n");
        t.write("skill/.env", b"TOKEN=x");
        t.write("skill/big.txt", &[b'a'; 100]);
        t.write("skill/blob.bin", &[0xff, 0xfe, 0x00]);
        t.write("other/secret.txt", b"no");
        fs::create_dir_all(t.0.join("skill/sub")).unwrap();
        let skill = t.0.join("skill");

        let path = resolve(&skill, "scripts/run.sh").unwrap();
        assert_eq!(read_text(&path, 1024).unwrap(), "echo hi\n");
        // `./` 在前面也还是这个目录里的那个文件。
        assert!(resolve(&skill, "./scripts/run.sh").is_ok());

        let kind = |rel: &str| match resolve(&skill, rel).and_then(|p| read_text(&p, 50)) {
            Ok(_) => "ok".to_string(),
            Err(e) => format!("{e:?}")
                .split([' ', '{', '('])
                .next()
                .unwrap()
                .to_string(),
        };
        assert_eq!(kind("../other/secret.txt"), "NotRelative");
        assert_eq!(
            kind(&t.0.join("other/secret.txt").to_string_lossy()),
            "NotRelative"
        );
        assert_eq!(kind(""), "NotRelative");
        assert_eq!(kind(".env"), "Hidden");
        assert_eq!(kind("./.env"), "Hidden");
        assert_eq!(kind("missing.md"), "NotFound");
        assert_eq!(kind("sub"), "NotAFile");
        assert_eq!(kind("big.txt"), "TooLarge");
        assert_eq!(kind("blob.bin"), "NotText");
    }

    #[cfg(unix)]
    #[test]
    fn a_link_that_leaves_the_directory_is_refused_and_a_linked_directory_is_followed() {
        let t = Temp::new("links");
        t.write("store/skill/SKILL.md", b"body");
        t.write("store/skill/notes.md", b"notes");
        t.write("store/skill/.secret", b"no");
        t.write("elsewhere/token.txt", b"no");
        let store = t.0.join("store/skill");
        std::os::unix::fs::symlink(t.0.join("elsewhere/token.txt"), store.join("token.txt"))
            .unwrap();
        std::os::unix::fs::symlink(store.join(".secret"), store.join("visible.txt")).unwrap();
        // skills CLI 的装法：~/.claude/skills/<name> 是指向 ~/.agents/skills/<name> 的链接。
        fs::create_dir_all(t.0.join("claude")).unwrap();
        let linked = t.0.join("claude/skill");
        std::os::unix::fs::symlink(&store, &linked).unwrap();

        assert!(matches!(
            resolve(&linked, "token.txt"),
            Err(ReadError::Outside)
        ));
        assert!(matches!(
            resolve(&linked, "visible.txt"),
            Err(ReadError::Hidden)
        ));
        let path = resolve(&linked, "notes.md").unwrap();
        assert_eq!(read_text(&path, 100).unwrap(), "notes");
        assert_eq!(list(&linked).files, ["notes.md"]);
    }
}
