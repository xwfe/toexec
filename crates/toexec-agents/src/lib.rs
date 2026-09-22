//! gld 与 ccnm 共用的 `~/.agents/mcp.json` 读取（RFC-0001）。
//!
//! 这个 crate 只管「把这个文件读明白、回答某个工具 / 某个 skill 该不该给」。
//! **不在这里的**：文件去哪个 HOME 找（ccnm 读执行账号的，gld 读跑守护进程的
//! 那个账号的）、多久重读一次、报错怎么措辞、被关掉的工具调用时回什么——
//! 这些两个产品各不相同。
//!
//! 文件长这样，只有三类键有意义，别的一律不看：
//!
//! ```json
//! {
//!   "mcpServers": {
//!     "gld":  { "disabledTools": ["exec_command"], "skillOverrides": { "deploy": "off" } },
//!     "ccnm": { "enabledTools": ["read_file", "load_skill"] }
//!   },
//!   "skillOverrides": { "cheat-pass": "off", "vue": "name-only" }
//! }
//! ```
//!
//! 语义不是自造的：工具开关照 Codex 的 `enabled_tools` / `disabled_tools`，
//! skill 四档照 Claude Code 2.1.278 设置里的 `skillOverrides`。
//!
//! **只收窄**：这里回答的是「在产品自己的上限里还要不要再关掉」。产品先按
//! 自己的规则（gld 的 tool-profile、ccnm 的 external_mcp、skill 自己的
//! frontmatter）得出上限，再问这里。

use std::collections::{BTreeMap, BTreeSet};
use std::fmt;
use std::path::{Path, PathBuf};

use serde_json::{Map, Value};

/// 这个 home 下的 `.agents/mcp.json`。
pub fn path_in(home: &Path) -> PathBuf {
    home.join(".agents").join("mcp.json")
}

/// 一个 skill 给到哪一档。从严到宽排：比较大小就是比谁更宽。
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub enum SkillLevel {
    /// 模型和用户都用不了，按名字找也当没有。
    Off,
    /// 不进给模型的目录、模型不能自己加载；用户点名还能用。和 frontmatter
    /// 写 `disable-model-invocation: true` 是同一件事。
    UserInvocableOnly,
    /// 目录里只有名字、没有描述，省上下文；其余照常。
    NameOnly,
    /// 不写就是这一档。
    On,
}

impl SkillLevel {
    pub const ALL: [SkillLevel; 4] = [
        SkillLevel::On,
        SkillLevel::NameOnly,
        SkillLevel::UserInvocableOnly,
        SkillLevel::Off,
    ];

    /// 文件里的写法，和 Claude Code 的一字不差。
    pub fn as_str(self) -> &'static str {
        match self {
            SkillLevel::On => "on",
            SkillLevel::NameOnly => "name-only",
            SkillLevel::UserInvocableOnly => "user-invocable-only",
            SkillLevel::Off => "off",
        }
    }

    pub fn parse(text: &str) -> Option<SkillLevel> {
        SkillLevel::ALL
            .into_iter()
            .find(|level| level.as_str() == text)
    }

    /// 出现在给模型的目录里。
    pub fn listed(self) -> bool {
        self >= SkillLevel::NameOnly
    }

    /// 目录里带描述。
    pub fn described(self) -> bool {
        self == SkillLevel::On
    }

    /// 模型能自己加载。
    pub fn model_invocable(self) -> bool {
        self >= SkillLevel::NameOnly
    }

    /// 用户点名能用。
    pub fn user_invocable(self) -> bool {
        self != SkillLevel::Off
    }
}

impl fmt::Display for SkillLevel {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(self.as_str())
    }
}

/// 读不下去的原因。措辞由产品定，这里只给分类和位置。
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ErrorKind {
    /// 不是合法的 JSON。`line` / `column` 从 1 数。
    Syntax { line: usize, column: usize },
    /// 这里要一个对象（`{...}`）。
    NotObject,
    /// 这里要一个数组（`[...]`）。
    NotArray,
    /// 这里要一个字符串。
    NotString,
    /// 档位不是 `on` / `name-only` / `user-invocable-only` / `off` 之一。
    UnknownLevel(String),
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Error {
    /// 出错的位置，写成 `mcpServers.gld.disabledTools[1]` 这样；语法错误时为空。
    pub path: String,
    pub kind: ErrorKind,
}

impl fmt::Display for Error {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match &self.kind {
            ErrorKind::Syntax { line, column } => {
                write!(f, "not valid JSON at line {line}, column {column}")
            }
            ErrorKind::NotObject => write!(f, "{}: expected an object", self.path),
            ErrorKind::NotArray => write!(f, "{}: expected an array of tool names", self.path),
            ErrorKind::NotString => write!(f, "{}: expected a string", self.path),
            ErrorKind::UnknownLevel(level) => write!(
                f,
                "{}: \"{level}\" is not one of on, name-only, user-invocable-only, off",
                self.path
            ),
        }
    }
}

impl std::error::Error for Error {}

/// 读文件时的失败：读不到（不含"文件不存在"），或读到了但内容不对。
#[derive(Debug)]
pub enum LoadError {
    Io {
        path: PathBuf,
        error: std::io::Error,
    },
    Invalid {
        path: PathBuf,
        error: Error,
    },
}

impl fmt::Display for LoadError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            LoadError::Io { path, error } => write!(f, "cannot read {}: {error}", path.display()),
            LoadError::Invalid { path, error } => write!(f, "{}: {error}", path.display()),
        }
    }
}

impl std::error::Error for LoadError {}

#[derive(Debug, Clone, Default, PartialEq, Eq)]
struct ServerRules {
    /// `None` = 没写白名单，全都可以。
    enabled: Option<BTreeSet<String>>,
    disabled: BTreeSet<String>,
    skills: BTreeMap<String, SkillLevel>,
}

/// 整个文件读出来的规则。
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct Policy {
    servers: BTreeMap<String, ServerRules>,
    skills: BTreeMap<String, SkillLevel>,
}

impl Policy {
    /// 读文件。不存在就是空规则（什么都不收窄），这是唯一放行的失败。
    pub fn load(path: &Path) -> Result<Policy, LoadError> {
        match std::fs::read_to_string(path) {
            Ok(text) => Policy::parse(&text).map_err(|error| LoadError::Invalid {
                path: path.to_path_buf(),
                error,
            }),
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(Policy::default()),
            Err(error) => Err(LoadError::Io {
                path: path.to_path_buf(),
                error,
            }),
        }
    }

    pub fn parse(text: &str) -> Result<Policy, Error> {
        let root: Value = serde_json::from_str(text).map_err(|error| Error {
            path: String::new(),
            kind: ErrorKind::Syntax {
                line: error.line(),
                column: error.column(),
            },
        })?;
        let root = object(&root, "")?;
        let mut policy = Policy::default();
        if let Some(servers) = root.get("mcpServers") {
            for (name, entry) in object(servers, "mcpServers")? {
                let at = format!("mcpServers.{name}");
                policy
                    .servers
                    .insert(name.clone(), server_rules(object(entry, &at)?, &at)?);
            }
        }
        if let Some(skills) = root.get("skillOverrides") {
            policy.skills = overrides(skills, "skillOverrides")?;
        }
        Ok(policy)
    }

    /// 什么都没写：产品可以跳过所有判断。
    pub fn is_empty(&self) -> bool {
        self.skills.is_empty()
            && self.servers.values().all(|rules| {
                rules.enabled.is_none() && rules.disabled.is_empty() && rules.skills.is_empty()
            })
    }

    /// 某个产品看到的规则：`mcpServers.<name>` 加上顶层的 `skillOverrides`。
    pub fn for_server(&self, name: &str) -> Rules<'_> {
        Rules {
            server: self.servers.get(name),
            global: &self.skills,
        }
    }
}

/// 为什么一个工具不给。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ToolRule {
    /// 写了 `enabledTools`，它不在里面。
    NotEnabled,
    /// 在 `disabledTools` 里。
    Disabled,
}

/// 一个 skill 的档是从哪来的，给 doctor 和列表说明用。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum SkillSource {
    /// 两处都没写，默认 `on`。
    Default,
    /// 顶层的 `skillOverrides`。
    Global,
    /// `mcpServers.<产品>.skillOverrides`。
    Server,
}

#[derive(Debug, Clone, Copy)]
pub struct Rules<'a> {
    server: Option<&'a ServerRules>,
    global: &'a BTreeMap<String, SkillLevel>,
}

impl Rules<'_> {
    pub fn tool_allowed(&self, tool: &str) -> bool {
        self.tool_rule(tool).is_none()
    }

    /// 不给时说是哪条规则；给的话是 `None`。
    pub fn tool_rule(&self, tool: &str) -> Option<ToolRule> {
        let rules = self.server?;
        if rules
            .enabled
            .as_ref()
            .is_some_and(|enabled| !enabled.contains(tool))
        {
            Some(ToolRule::NotEnabled)
        } else if rules.disabled.contains(tool) {
            Some(ToolRule::Disabled)
        } else {
            None
        }
    }

    /// 有没有任何工具规则。没有的话产品不用逐个问。
    pub fn restricts_tools(&self) -> bool {
        self.server
            .is_some_and(|rules| rules.enabled.is_some() || !rules.disabled.is_empty())
    }

    /// 写在 `enabledTools` / `disabledTools` 里、却不是 `known` 之一的名字。
    ///
    /// 写错在 `disabledTools` 里的名字意味着用户想关的工具**还开着**，所以
    /// 产品要把它当成需要人看的问题报出来，而不只是忽略。
    pub fn unknown_tools(&self, known: &[&str]) -> Vec<String> {
        let Some(rules) = self.server else {
            return Vec::new();
        };
        let named: BTreeSet<&String> = rules
            .enabled
            .iter()
            .flatten()
            .chain(&rules.disabled)
            .collect();
        named
            .into_iter()
            .filter(|name| !known.contains(&name.as_str()))
            .cloned()
            .collect()
    }

    /// 这个 skill 的档：产品条目里写的优先，其次顶层，都没有是 `on`。
    pub fn skill_level(&self, skill: &str) -> SkillLevel {
        self.skill_rule(skill).0
    }

    pub fn skill_rule(&self, skill: &str) -> (SkillLevel, SkillSource) {
        if let Some(level) = self.server.and_then(|rules| rules.skills.get(skill)) {
            (*level, SkillSource::Server)
        } else if let Some(level) = self.global.get(skill) {
            (*level, SkillSource::Global)
        } else {
            (SkillLevel::On, SkillSource::Default)
        }
    }

    /// 对这个产品生效的全部 skill 覆盖（产品条目盖过顶层），给 doctor 找
    /// "写了但没有这个 skill"用。
    pub fn skill_overrides(&self) -> BTreeMap<&str, SkillLevel> {
        let mut merged: BTreeMap<&str, SkillLevel> = self
            .global
            .iter()
            .map(|(name, level)| (name.as_str(), *level))
            .collect();
        if let Some(rules) = self.server {
            for (name, level) in &rules.skills {
                merged.insert(name.as_str(), *level);
            }
        }
        merged
    }
}

fn object<'v>(value: &'v Value, at: &str) -> Result<&'v Map<String, Value>, Error> {
    value.as_object().ok_or_else(|| Error {
        path: at.to_string(),
        kind: ErrorKind::NotObject,
    })
}

fn server_rules(entry: &Map<String, Value>, at: &str) -> Result<ServerRules, Error> {
    Ok(ServerRules {
        enabled: entry
            .get("enabledTools")
            .map(|value| names(value, &format!("{at}.enabledTools")))
            .transpose()?,
        disabled: entry
            .get("disabledTools")
            .map(|value| names(value, &format!("{at}.disabledTools")))
            .transpose()?
            .unwrap_or_default(),
        skills: entry
            .get("skillOverrides")
            .map(|value| overrides(value, &format!("{at}.skillOverrides")))
            .transpose()?
            .unwrap_or_default(),
    })
}

fn names(value: &Value, at: &str) -> Result<BTreeSet<String>, Error> {
    let items = value.as_array().ok_or_else(|| Error {
        path: at.to_string(),
        kind: ErrorKind::NotArray,
    })?;
    items
        .iter()
        .enumerate()
        .map(|(index, item)| {
            item.as_str().map(str::to_string).ok_or_else(|| Error {
                path: format!("{at}[{index}]"),
                kind: ErrorKind::NotString,
            })
        })
        .collect()
}

fn overrides(value: &Value, at: &str) -> Result<BTreeMap<String, SkillLevel>, Error> {
    object(value, at)?
        .iter()
        .map(|(name, level)| {
            let here = format!("{at}.{name}");
            let text = level.as_str().ok_or_else(|| Error {
                path: here.clone(),
                kind: ErrorKind::NotString,
            })?;
            let level = SkillLevel::parse(text).ok_or_else(|| Error {
                path: here,
                kind: ErrorKind::UnknownLevel(text.to_string()),
            })?;
            Ok((name.clone(), level))
        })
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    const SAMPLE: &str = r#"{
      "mcpServers": {
        "gld": {
          "url": "http://127.0.0.1:28764/mcp",
          "disabledTools": ["exec_command"],
          "skillOverrides": { "deploy": "user-invocable-only", "vue": "on" }
        },
        "ccnm": { "enabledTools": ["read_file", "load_skill"], "disabledTools": ["load_skill"] },
        "context7": { "command": "npx", "args": ["-y", "@upstash/context7-mcp"] }
      },
      "skillOverrides": { "cheat-pass": "off", "vue": "name-only" },
      "somethingElse": 1
    }"#;

    #[test]
    fn tools_follow_codex_allow_then_deny() {
        let policy = Policy::parse(SAMPLE).unwrap();
        let gld = policy.for_server("gld");
        assert!(gld.restricts_tools());
        assert_eq!(gld.tool_rule("exec_command"), Some(ToolRule::Disabled));
        assert!(gld.tool_allowed("read_file"));

        // 白名单先取、黑名单再去掉：load_skill 两处都写了，结果是不给。
        let ccnm = policy.for_server("ccnm");
        assert!(ccnm.tool_allowed("read_file"));
        assert_eq!(ccnm.tool_rule("load_skill"), Some(ToolRule::Disabled));
        assert_eq!(ccnm.tool_rule("exec_command"), Some(ToolRule::NotEnabled));

        // 没写条目的产品什么都不收窄。
        let other = policy.for_server("nobody");
        assert!(!other.restricts_tools());
        assert!(other.tool_allowed("exec_command"));
    }

    #[test]
    fn an_empty_allow_list_gives_nothing() {
        let policy = Policy::parse(r#"{"mcpServers": {"gld": {"enabledTools": []}}}"#).unwrap();
        assert_eq!(
            policy.for_server("gld").tool_rule("read_file"),
            Some(ToolRule::NotEnabled)
        );
    }

    #[test]
    fn skill_levels_take_the_product_entry_over_the_top_level() {
        let policy = Policy::parse(SAMPLE).unwrap();
        let gld = policy.for_server("gld");
        assert_eq!(gld.skill_rule("vue"), (SkillLevel::On, SkillSource::Server));
        assert_eq!(
            gld.skill_rule("cheat-pass"),
            (SkillLevel::Off, SkillSource::Global)
        );
        assert_eq!(
            gld.skill_rule("deploy"),
            (SkillLevel::UserInvocableOnly, SkillSource::Server)
        );
        assert_eq!(
            gld.skill_rule("anything"),
            (SkillLevel::On, SkillSource::Default)
        );

        let ccnm = policy.for_server("ccnm");
        assert_eq!(ccnm.skill_level("vue"), SkillLevel::NameOnly);
        assert_eq!(ccnm.skill_level("deploy"), SkillLevel::On);
        assert_eq!(
            ccnm.skill_overrides().into_iter().collect::<Vec<_>>(),
            vec![
                ("cheat-pass", SkillLevel::Off),
                ("vue", SkillLevel::NameOnly)
            ]
        );
    }

    #[test]
    fn levels_mean_what_claude_code_says() {
        use SkillLevel::*;
        let table: Vec<_> = SkillLevel::ALL
            .into_iter()
            .map(|l| {
                (
                    l.as_str(),
                    l.listed(),
                    l.described(),
                    l.model_invocable(),
                    l.user_invocable(),
                )
            })
            .collect();
        assert_eq!(
            table,
            vec![
                ("on", true, true, true, true),
                ("name-only", true, false, true, true),
                ("user-invocable-only", false, false, false, true),
                ("off", false, false, false, false),
            ]
        );
        // 从严到宽：取两个里更严的就是 min。
        assert_eq!(On.min(UserInvocableOnly), UserInvocableOnly);
        assert!(Off < UserInvocableOnly && UserInvocableOnly < NameOnly && NameOnly < On);
        for level in SkillLevel::ALL {
            assert_eq!(SkillLevel::parse(level.as_str()), Some(level));
        }
        assert_eq!(SkillLevel::parse("hidden"), None);
    }

    #[test]
    fn unknown_tool_names_are_reported_not_fatal() {
        let policy = Policy::parse(
            r#"{"mcpServers": {"gld": {"enabledTools": ["read_file"], "disabledTools": ["exec_comand"]}}}"#,
        )
        .unwrap();
        let gld = policy.for_server("gld");
        assert_eq!(
            gld.unknown_tools(&["read_file", "exec_command"]),
            vec!["exec_comand"]
        );
        assert!(policy.for_server("ccnm").unknown_tools(&[]).is_empty());
    }

    #[test]
    fn a_broken_file_says_where() {
        let err = |text: &str| Policy::parse(text).unwrap_err();
        assert_eq!(
            err("{\n  \"mcpServers\": {,\n}"),
            Error {
                path: String::new(),
                kind: ErrorKind::Syntax {
                    line: 2,
                    column: 18
                }
            }
        );
        assert_eq!(err("[]").kind, ErrorKind::NotObject);
        assert_eq!(err(r#"{"mcpServers": []}"#).path, "mcpServers");
        assert_eq!(
            err(r#"{"mcpServers": {"gld": "x"}}"#),
            Error {
                path: "mcpServers.gld".into(),
                kind: ErrorKind::NotObject
            }
        );
        assert_eq!(
            err(r#"{"mcpServers": {"gld": {"disabledTools": "exec_command"}}}"#),
            Error {
                path: "mcpServers.gld.disabledTools".into(),
                kind: ErrorKind::NotArray
            }
        );
        assert_eq!(
            err(r#"{"mcpServers": {"gld": {"enabledTools": ["a", 1]}}}"#).path,
            "mcpServers.gld.enabledTools[1]"
        );
        let level = err(r#"{"skillOverrides": {"vue": "hidden"}}"#);
        assert_eq!(level.path, "skillOverrides.vue");
        assert_eq!(level.kind, ErrorKind::UnknownLevel("hidden".into()));
        assert!(level.to_string().contains("user-invocable-only"), "{level}");
        assert_eq!(
            err(r#"{"skillOverrides": {"vue": false}}"#).kind,
            ErrorKind::NotString
        );
    }

    #[test]
    fn nothing_written_restricts_nothing() {
        assert!(Policy::parse("{}").unwrap().is_empty());
        assert!(
            Policy::parse(r#"{"mcpServers": {"context7": {"command": "npx"}}}"#)
                .unwrap()
                .is_empty()
        );
        assert!(!Policy::parse(SAMPLE).unwrap().is_empty());
    }

    #[test]
    fn a_missing_file_is_an_empty_policy_and_nothing_else_is() {
        let dir = std::env::temp_dir().join(format!("toexec-agents-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        let path = path_in(&dir);
        assert!(path.ends_with(".agents/mcp.json"));
        assert_eq!(Policy::load(&path).unwrap(), Policy::default());

        std::fs::create_dir_all(path.parent().unwrap()).unwrap();
        std::fs::write(&path, r#"{"skillOverrides": {"a": "nope"}}"#).unwrap();
        let err = Policy::load(&path).unwrap_err();
        assert!(matches!(err, LoadError::Invalid { .. }), "{err}");
        assert!(err.to_string().contains("mcp.json"), "{err}");

        // 是个目录而不是文件：读不了，不能当成"没有"。
        std::fs::remove_file(&path).unwrap();
        std::fs::create_dir(&path).unwrap();
        assert!(matches!(Policy::load(&path), Err(LoadError::Io { .. })));
        std::fs::remove_dir_all(&dir).unwrap();
    }
}
