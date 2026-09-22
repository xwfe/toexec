//! 已经装好的 MCP server：直接读 Claude Code 和 Codex 自己的配置，以及项目自己的 `.mcp.json`。
//!
//! 只读，不改，也不让用户在产品里再抄一份清单——抄一份迟早和原件对不上。
//!
//! | 读哪儿 | 读什么 | 谁用 |
//! | --- | --- | --- |
//! | 项目根下的 `.mcp.json` | `mcpServers`（Claude Code 的 project 级） | ccnm（项目在那台机器上） |
//! | `~/.claude.json` | 顶层 `mcpServers`（Claude Code 的 user 级） | gld、ccnm |
//! | `$CODEX_HOME/config.toml`，没设就是 `~/.codex/config.toml` | `[mcp_servers.*]` | gld、ccnm |
//!
//! `~/.claude.json` 里 `projects.<路径>.mcpServers`（Claude Code 的 local 级）不读：
//! 那是某个人在某个目录下的私人设置，别的客户端不该继承。
//!
//! **同名时先列的赢**：项目的 > `~/.claude.json` > Codex，被盖掉的进
//! [`Installed::shadowed`]。项目压过 user 级是 Claude Code 的规矩（local > project
//! > user）；Claude 压过 Codex 没有哪边更权威，只是得定一个、并且说出来。
//!
//! 值里的 `${VAR}` / `${VAR:-默认值}` 照 Claude Code 的规矩展开（`.mcp.json` 和
//! `~/.claude.json`；Codex 不展开，它用 `bearer_token_env_var`、`env_http_headers`
//! 从环境里取）。`~` 不展开：原生客户端也不展开，由 server 自己处理（Filesystem
//! 就是这么做的）。

use std::collections::BTreeMap;
use std::path::{Path, PathBuf};
use std::time::Duration;

use serde_json::Value as Json;

/// 这条配置是从哪个文件读来的。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Source {
    /// 项目根下的 `.mcp.json`。
    Project,
    Claude,
    Codex,
}

impl Source {
    /// 给人看的文件名。Codex 设了 `CODEX_HOME` 时实际读的是那里，这里仍写
    /// 惯用的路径：它是一个标签，不是用来打开文件的。
    pub fn file(self) -> &'static str {
        match self {
            Source::Project => ".mcp.json",
            Source::Claude => "~/.claude.json",
            Source::Codex => "~/.codex/config.toml",
        }
    }
}

/// 怎么连这个 server。
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Transport {
    /// 本机起一个进程，经 stdin/stdout 说话。
    Stdio {
        command: String,
        args: Vec<String>,
        /// 在继承来的环境之上**再加**的变量。
        env: BTreeMap<String, String>,
        cwd: Option<PathBuf>,
    },
    /// Streamable HTTP（MCP 2025-03-26 起的标准传输）。
    Http {
        url: String,
        headers: BTreeMap<String, String>,
    },
    /// 老的 HTTP+SSE 传输（2024-11-05）。读得出来，这里的客户端不支持连它。
    Sse {
        url: String,
        headers: BTreeMap<String, String>,
    },
}

/// 这个 server 跑在哪儿，给人和模型看的分类。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Kind {
    /// 本机起的进程：它能做什么，取决于它自己，起它的产品管不着。
    LocalProcess,
    /// 本机上一个已经在跑的 HTTP 服务（`127.0.0.1`、`localhost`）。
    LocalUrl,
    /// 别处的 HTTP 服务。
    RemoteUrl,
}

impl Kind {
    pub fn as_str(self) -> &'static str {
        match self {
            Kind::LocalProcess => "local process",
            Kind::LocalUrl => "local URL",
            Kind::RemoteUrl => "remote URL",
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Server {
    pub name: String,
    pub source: Source,
    pub transport: Transport,
    /// 来源配置里把它关了（Codex 的 `enabled = false`，JSON 配置里的
    /// `disabled: true` / `enabled: false`）。怎么对待由产品定：gld 只拿它提示，
    /// 开不开看自己的名单。
    pub off_in_source: bool,
    /// Codex 的 `enabled_tools`：只放这几个。`None` = 没写。
    pub enabled_tools: Option<Vec<String>>,
    /// Codex 的 `disabled_tools`。
    pub disabled_tools: Vec<String>,
    pub startup_timeout: Option<Duration>,
    pub tool_timeout: Option<Duration>,
    /// 配置里点了名、环境里却没有的变量。有的话这个 server 起不来（Claude Code
    /// 对这种配置是整条拒绝）。
    pub missing_env: Vec<String>,
}

impl Server {
    pub fn kind(&self) -> Kind {
        match &self.transport {
            Transport::Stdio { .. } => Kind::LocalProcess,
            Transport::Http { url, .. } | Transport::Sse { url, .. } => {
                if is_loopback(&url_host(url)) {
                    Kind::LocalUrl
                } else {
                    Kind::RemoteUrl
                }
            }
        }
    }

    /// 一行"跑的是什么"，**可以给人看**：不含环境变量和请求头的值，URL 去掉
    /// 查询串和用户名密码（`?exaApiKey=…` 这种写法很常见）。stdio 只给程序名
    /// 和第一个不以 `-` 开头的参数，通常就是包名或脚本路径。
    pub fn target(&self) -> String {
        match &self.transport {
            Transport::Stdio { command, args, .. } => {
                let program = Path::new(command)
                    .file_name()
                    .map(|name| name.to_string_lossy().into_owned())
                    .unwrap_or_else(|| command.clone());
                match args.iter().find(|arg| !arg.starts_with('-')) {
                    Some(first) => format!("{program} {first}"),
                    None => program,
                }
            }
            Transport::Http { url, .. } | Transport::Sse { url, .. } => display_url(url),
        }
    }

    /// 这个工具按来源配置的 `enabled_tools` / `disabled_tools` 放不放。
    pub fn allows_tool(&self, tool: &str) -> bool {
        if self.disabled_tools.iter().any(|name| name == tool) {
            return false;
        }
        match &self.enabled_tools {
            Some(only) => only.iter().any(|name| name == tool),
            None => true,
        }
    }
}

/// 读的时候发现的问题：文件坏了、某一条写得不对。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Problem {
    pub source: Source,
    /// 整个文件的问题时是 `None`。
    pub server: Option<String>,
    pub message: String,
}

#[derive(Debug, Clone, Default)]
pub struct Installed {
    /// 按名字排（不分大小写），同名只留生效的那份。
    pub servers: Vec<Server>,
    /// 同名被先列的那份盖掉的条目（见模块说明）。
    pub shadowed: Vec<Server>,
    pub problems: Vec<Problem>,
}

impl Installed {
    pub fn find(&self, name: &str) -> Option<&Server> {
        self.servers.iter().find(|server| server.name == name)
    }
}

/// 配置在哪。
#[derive(Debug, Clone)]
pub struct Places {
    /// 项目的 `.mcp.json`；不读项目的（gld）是 `None`。
    pub project: Option<PathBuf>,
    pub claude: PathBuf,
    pub codex: PathBuf,
}

impl Places {
    /// 这个账号装的。`codex_home` 是 `$CODEX_HOME`（设了的话）。
    pub fn new(home: &Path, codex_home: Option<&Path>) -> Places {
        Places {
            project: None,
            claude: home.join(".claude.json"),
            codex: codex_home
                .map(Path::to_path_buf)
                .unwrap_or_else(|| home.join(".codex"))
                .join("config.toml"),
        }
    }

    /// 再加上项目根下的 `.mcp.json`，它排在最前面。
    pub fn with_project(mut self, root: &Path) -> Places {
        self.project = Some(root.join(".mcp.json"));
        self
    }
}

/// 读这几份配置。文件不存在就是没装，不算问题；读不了、解析不了的记进
/// [`Installed::problems`]，别的条目照读。
///
/// `env` 查环境变量：产品传 `std::env::var`，测试传一张表。
pub fn read(places: &Places, env: &dyn Fn(&str) -> Option<String>) -> Installed {
    let mut problems = Vec::new();
    let mut ordered = Vec::new();
    if let Some(project) = &places.project {
        ordered.extend(read_json(project, Source::Project, env, &mut problems));
    }
    ordered.extend(read_json(
        &places.claude,
        Source::Claude,
        env,
        &mut problems,
    ));
    ordered.extend(read_codex(&places.codex, env, &mut problems));

    let mut servers: Vec<Server> = Vec::new();
    let mut shadowed = Vec::new();
    for server in ordered {
        if servers.iter().any(|kept| kept.name == server.name) {
            shadowed.push(server);
        } else {
            servers.push(server);
        }
    }
    servers.sort_by_key(|server| (server.name.to_lowercase(), server.name.clone()));
    Installed {
        servers,
        shadowed,
        problems,
    }
}

/// `~/.claude.json` 和 `.mcp.json` 是同一个写法：顶层一个 `mcpServers`。
fn read_json(
    path: &Path,
    source: Source,
    env: &dyn Fn(&str) -> Option<String>,
    problems: &mut Vec<Problem>,
) -> Vec<Server> {
    let Some(text) = read_file(path, source, problems) else {
        return Vec::new();
    };
    let parsed: Json = match serde_json::from_str(&text) {
        Ok(parsed) => parsed,
        Err(error) => {
            problems.push(whole_file(source, format!("not valid JSON: {error}")));
            return Vec::new();
        }
    };
    let Some(entries) = parsed.get("mcpServers") else {
        return Vec::new();
    };
    let Some(entries) = entries.as_object() else {
        problems.push(whole_file(source, "mcpServers is not an object".into()));
        return Vec::new();
    };
    let mut servers = Vec::new();
    for (name, entry) in entries {
        match claude_server(name, entry, source, env) {
            Ok(server) => servers.push(server),
            Err(message) => problems.push(Problem {
                source,
                server: Some(name.clone()),
                message,
            }),
        }
    }
    servers
}

fn claude_server(
    name: &str,
    entry: &Json,
    source: Source,
    env: &dyn Fn(&str) -> Option<String>,
) -> Result<Server, String> {
    let entry = entry.as_object().ok_or("the entry is not an object")?;
    let mut missing = Vec::new();
    let mut expand = |text: &str| expand_vars(text, env, &mut missing);
    let string = |key: &str| -> Result<Option<String>, String> {
        match entry.get(key) {
            None | Some(Json::Null) => Ok(None),
            Some(Json::String(text)) => Ok(Some(text.clone())),
            Some(_) => Err(format!("{key} is not a string")),
        }
    };
    let map = |key: &str| -> Result<BTreeMap<String, String>, String> {
        match entry.get(key) {
            None | Some(Json::Null) => Ok(BTreeMap::new()),
            Some(Json::Object(object)) => object
                .iter()
                .map(|(k, v)| match v {
                    Json::String(text) => Ok((k.clone(), text.clone())),
                    _ => Err(format!("{key}.{k} is not a string")),
                })
                .collect(),
            Some(_) => Err(format!("{key} is not an object")),
        }
    };

    let kind = string("type")?;
    let command = string("command")?;
    let url = string("url")?;
    let kind = match kind.as_deref() {
        Some("stdio") => "stdio",
        Some("http" | "streamable-http" | "streamableHttp") => "http",
        Some("sse") => "sse",
        Some(other) => {
            return Err(format!(
                "type \"{other}\" is not a transport this reader knows"
            ));
        }
        None if command.is_some() => "stdio",
        None if url.is_some() => "http",
        None => return Err("it has neither command nor url".into()),
    };
    let transport = match kind {
        "stdio" => {
            let command = command.ok_or("type is stdio but there is no command")?;
            let args = match entry.get("args") {
                None | Some(Json::Null) => Vec::new(),
                Some(Json::Array(items)) => items
                    .iter()
                    .map(|item| item.as_str().map(str::to_string))
                    .collect::<Option<Vec<_>>>()
                    .ok_or("args has an entry that is not a string")?,
                Some(_) => return Err("args is not a list".into()),
            };
            Transport::Stdio {
                command: expand(&command),
                args: args.iter().map(|arg| expand(arg)).collect(),
                env: map("env")?
                    .into_iter()
                    .map(|(k, v)| (k, expand(&v)))
                    .collect(),
                cwd: None,
            }
        }
        _ => {
            let url = url.ok_or_else(|| format!("type is {kind} but there is no url"))?;
            let headers = map("headers")?
                .into_iter()
                .map(|(k, v)| (k, expand(&v)))
                .collect();
            let url = expand(&url);
            if kind == "sse" {
                Transport::Sse { url, headers }
            } else {
                Transport::Http { url, headers }
            }
        }
    };
    let off_in_source = entry.get("disabled") == Some(&Json::Bool(true))
        || entry.get("enabled") == Some(&Json::Bool(false));
    missing.sort();
    missing.dedup();
    Ok(Server {
        name: name.to_string(),
        source,
        transport,
        off_in_source,
        enabled_tools: None,
        disabled_tools: Vec::new(),
        startup_timeout: None,
        tool_timeout: None,
        missing_env: missing,
    })
}

fn read_codex(
    path: &Path,
    env: &dyn Fn(&str) -> Option<String>,
    problems: &mut Vec<Problem>,
) -> Vec<Server> {
    let Some(text) = read_file(path, Source::Codex, problems) else {
        return Vec::new();
    };
    let parsed: toml::Table = match text.parse() {
        Ok(parsed) => parsed,
        Err(error) => {
            let first = error.to_string();
            let first = first.lines().next().unwrap_or_default().to_string();
            problems.push(whole_file(
                Source::Codex,
                format!("not valid TOML: {first}"),
            ));
            return Vec::new();
        }
    };
    let Some(entries) = parsed.get("mcp_servers") else {
        return Vec::new();
    };
    let Some(entries) = entries.as_table() else {
        problems.push(whole_file(
            Source::Codex,
            "mcp_servers is not a table".into(),
        ));
        return Vec::new();
    };
    let mut servers = Vec::new();
    for (name, entry) in entries {
        match codex_server(name, entry, env) {
            Ok(server) => servers.push(server),
            Err(message) => problems.push(Problem {
                source: Source::Codex,
                server: Some(name.clone()),
                message,
            }),
        }
    }
    servers
}

fn codex_server(
    name: &str,
    entry: &toml::Value,
    env: &dyn Fn(&str) -> Option<String>,
) -> Result<Server, String> {
    let entry = entry.as_table().ok_or("the entry is not a table")?;
    let string = |key: &str| -> Result<Option<String>, String> {
        match entry.get(key) {
            None => Ok(None),
            Some(toml::Value::String(text)) => Ok(Some(text.clone())),
            Some(_) => Err(format!("{key} is not a string")),
        }
    };
    let strings = |key: &str| -> Result<Option<Vec<String>>, String> {
        match entry.get(key) {
            None => Ok(None),
            Some(toml::Value::Array(items)) => items
                .iter()
                .map(|item| item.as_str().map(str::to_string))
                .collect::<Option<Vec<_>>>()
                .map(Some)
                .ok_or_else(|| format!("{key} has an entry that is not a string")),
            Some(_) => Err(format!("{key} is not a list")),
        }
    };
    let map = |key: &str| -> Result<BTreeMap<String, String>, String> {
        match entry.get(key) {
            None => Ok(BTreeMap::new()),
            Some(toml::Value::Table(table)) => table
                .iter()
                .map(|(k, v)| match v {
                    toml::Value::String(text) => Ok((k.clone(), text.clone())),
                    _ => Err(format!("{key}.{k} is not a string")),
                })
                .collect(),
            Some(_) => Err(format!("{key} is not a table")),
        }
    };
    let seconds = |key: &str, scale: f64| -> Result<Option<Duration>, String> {
        let value = match entry.get(key) {
            None => return Ok(None),
            Some(toml::Value::Integer(n)) => *n as f64,
            Some(toml::Value::Float(n)) => *n,
            Some(_) => return Err(format!("{key} is not a number")),
        };
        if !value.is_finite() || value <= 0.0 {
            return Err(format!("{key} must be above zero"));
        }
        Ok(Some(Duration::from_secs_f64(value * scale)))
    };

    let mut missing = Vec::new();
    let transport = match (string("command")?, string("url")?) {
        (Some(command), _) => Transport::Stdio {
            command,
            args: strings("args")?.unwrap_or_default(),
            env: map("env")?,
            cwd: string("cwd")?.map(PathBuf::from),
        },
        (None, Some(url)) => {
            let mut headers = map("http_headers")?;
            // 值不在配置里、在环境里：没设就不加这个头，Codex 也是这么做的。
            for (header, var) in map("env_http_headers")? {
                if let Some(value) = env(&var).filter(|value| !value.is_empty()) {
                    headers.insert(header, value);
                }
            }
            if let Some(var) = string("bearer_token_env_var")? {
                match env(&var).filter(|value| !value.is_empty()) {
                    Some(token) => {
                        headers.insert("Authorization".into(), format!("Bearer {token}"));
                    }
                    None => missing.push(var),
                }
            }
            Transport::Http { url, headers }
        }
        (None, None) => return Err("it has neither command nor url".into()),
    };
    Ok(Server {
        name: name.to_string(),
        source: Source::Codex,
        transport,
        off_in_source: entry.get("enabled").and_then(toml::Value::as_bool) == Some(false),
        enabled_tools: strings("enabled_tools")?,
        disabled_tools: strings("disabled_tools")?.unwrap_or_default(),
        startup_timeout: match seconds("startup_timeout_sec", 1.0)? {
            Some(timeout) => Some(timeout),
            None => seconds("startup_timeout_ms", 0.001)?,
        },
        tool_timeout: seconds("tool_timeout_sec", 1.0)?,
        missing_env: missing,
    })
}

fn read_file(path: &Path, source: Source, problems: &mut Vec<Problem>) -> Option<String> {
    match std::fs::read_to_string(path) {
        Ok(text) => Some(text),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => None,
        Err(error) => {
            problems.push(whole_file(source, format!("cannot read it: {error}")));
            None
        }
    }
}

fn whole_file(source: Source, message: String) -> Problem {
    Problem {
        source,
        server: None,
        message,
    }
}

/// `${VAR}` 换成变量的值，`${VAR:-默认值}` 在变量没设或为空时用默认值。
/// 没设又没默认值的记进 `missing`，换成空串。没闭合的 `${` 原样留着。
fn expand_vars(
    text: &str,
    env: &dyn Fn(&str) -> Option<String>,
    missing: &mut Vec<String>,
) -> String {
    let mut out = String::with_capacity(text.len());
    let mut rest = text;
    while let Some(start) = rest.find("${") {
        out.push_str(&rest[..start]);
        let after = &rest[start + 2..];
        let Some(end) = after.find('}') else {
            out.push_str(&rest[start..]);
            return out;
        };
        let inner = &after[..end];
        let (name, default) = match inner.split_once(":-") {
            Some((name, default)) => (name, Some(default)),
            None => (inner, None),
        };
        match (env(name).filter(|value| !value.is_empty()), default) {
            (Some(value), _) => out.push_str(&value),
            (None, Some(default)) => out.push_str(default),
            (None, None) => {
                // 设成空串也算设了：`${VAR}` 在 shell 里就是空。
                match env(name) {
                    Some(empty) => out.push_str(&empty),
                    None => missing.push(name.to_string()),
                }
            }
        }
        rest = &after[end + 1..];
    }
    out.push_str(rest);
    out
}

/// `scheme://user:pass@host:port/path?query#frag` 里的 host（小写，IPv6 去掉方括号）。
fn url_host(url: &str) -> String {
    let after_scheme = url.split_once("://").map_or(url, |(_, rest)| rest);
    let authority = after_scheme
        .split(['/', '?', '#'])
        .next()
        .unwrap_or_default();
    let host_port = authority
        .rsplit_once('@')
        .map_or(authority, |(_, host)| host);
    let host = if let Some(bracketed) = host_port.strip_prefix('[') {
        bracketed.split(']').next().unwrap_or_default()
    } else {
        host_port.split(':').next().unwrap_or_default()
    };
    host.to_ascii_lowercase()
}

/// 这个地址是不是本机。
pub fn is_loopback_url(url: &str) -> bool {
    is_loopback(&url_host(url))
}

fn is_loopback(host: &str) -> bool {
    host == "localhost" || host.ends_with(".localhost") || host == "::1" || host.starts_with("127.")
}

/// URL 去掉用户名密码、查询串和片段。
pub fn display_url(url: &str) -> String {
    let (scheme, rest) = match url.split_once("://") {
        Some((scheme, rest)) => (format!("{scheme}://"), rest),
        None => (String::new(), url),
    };
    let end = rest.find(['?', '#']).unwrap_or(rest.len());
    let rest = &rest[..end];
    let (authority, path) = match rest.find('/') {
        Some(slash) => (&rest[..slash], &rest[slash..]),
        None => (rest, ""),
    };
    let authority = authority
        .rsplit_once('@')
        .map_or(authority, |(_, host)| host);
    format!("{scheme}{authority}{path}")
}

#[cfg(test)]
mod tests {

    /// 测试目录：跑完就删。和别的 crate 一样不引 tempfile。
    struct Temp(PathBuf);

    impl Temp {
        fn path(&self) -> &Path {
            &self.0
        }
    }

    impl Drop for Temp {
        fn drop(&mut self) {
            let _ = std::fs::remove_dir_all(&self.0);
        }
    }

    mod tempfile {
        use std::sync::atomic::{AtomicUsize, Ordering};

        static NEXT: AtomicUsize = AtomicUsize::new(0);

        pub(super) fn tempdir() -> std::io::Result<super::Temp> {
            let dir = std::env::temp_dir().join(format!(
                "toexec-mcp-installed-{}-{}",
                std::process::id(),
                NEXT.fetch_add(1, Ordering::SeqCst)
            ));
            let _ = std::fs::remove_dir_all(&dir);
            std::fs::create_dir_all(&dir)?;
            Ok(super::Temp(dir))
        }
    }
    use super::*;

    fn env_of(pairs: &[(&str, &str)]) -> impl Fn(&str) -> Option<String> {
        let map: BTreeMap<String, String> = pairs
            .iter()
            .map(|(k, v)| (k.to_string(), v.to_string()))
            .collect();
        move |name| map.get(name).cloned()
    }

    fn places(dir: &Path) -> Places {
        Places::new(dir, None)
    }

    fn write(dir: &Path, rel: &str, text: &str) {
        let path = dir.join(rel);
        std::fs::create_dir_all(path.parent().unwrap()).unwrap();
        std::fs::write(path, text).unwrap();
    }

    #[test]
    fn nothing_installed_is_not_a_problem() {
        let home = tempfile::tempdir().unwrap();
        let found = read(&places(home.path()), &env_of(&[]));
        assert!(found.servers.is_empty());
        assert!(found.problems.is_empty(), "{:?}", found.problems);
    }

    #[test]
    fn both_files_are_read_and_the_claude_one_wins_a_name() {
        let home = tempfile::tempdir().unwrap();
        write(
            home.path(),
            ".claude.json",
            r#"{
              "projects": { "/x": { "mcpServers": { "project-only": { "command": "p" } } } },
              "mcpServers": {
                "time": { "type": "stdio", "command": "uvx", "args": ["mcp-server-time", "--tz=x"] },
                "exa": { "type": "http", "url": "https://mcp.exa.ai/mcp?exaApiKey=${EXA}", "headers": { "x-api-key": "${EXA}" } },
                "old": { "type": "sse", "url": "http://127.0.0.1:9/sse" }
              }
            }"#,
        );
        write(
            home.path(),
            ".codex/config.toml",
            r#"
            model = "x"
            [mcp_servers.time]
            command = "other"
            [mcp_servers.Deep]
            url = "https://mcp.deepwiki.com/mcp"
            bearer_token_env_var = "DW"
            env_http_headers = { "X-Team" = "TEAM", "X-Unset" = "NOPE" }
            [mcp_servers.fs]
            command = "npx"
            args = ["-y", "@modelcontextprotocol/server-filesystem", "~/Projects"]
            cwd = "/tmp"
            enabled = false
            enabled_tools = ["read_file", "list_directory"]
            disabled_tools = ["list_directory"]
            startup_timeout_sec = 20
            tool_timeout_sec = 2.5
            "#,
        );
        let found = read(
            &places(home.path()),
            &env_of(&[("EXA", "k1"), ("DW", "t0k"), ("TEAM", "core")]),
        );
        assert!(found.problems.is_empty(), "{:?}", found.problems);
        let names: Vec<&str> = found.servers.iter().map(|s| s.name.as_str()).collect();
        assert_eq!(names, ["Deep", "exa", "fs", "old", "time"]);

        let time = found.find("time").unwrap();
        assert_eq!(
            time.source,
            Source::Claude,
            "同名时 ~/.claude.json 那份生效"
        );
        assert_eq!(found.shadowed.len(), 1);
        assert_eq!(found.shadowed[0].source, Source::Codex);
        assert_eq!(time.target(), "uvx mcp-server-time");
        assert_eq!(time.kind(), Kind::LocalProcess);

        let exa = found.find("exa").unwrap();
        match &exa.transport {
            Transport::Http { url, headers } => {
                assert_eq!(url, "https://mcp.exa.ai/mcp?exaApiKey=k1");
                assert_eq!(headers["x-api-key"], "k1");
            }
            other => panic!("{other:?}"),
        }
        assert_eq!(
            exa.target(),
            "https://mcp.exa.ai/mcp",
            "查询串里的密钥不能出现"
        );
        assert_eq!(exa.kind(), Kind::RemoteUrl);

        let old = found.find("old").unwrap();
        assert!(matches!(old.transport, Transport::Sse { .. }));
        assert_eq!(old.kind(), Kind::LocalUrl);

        let deep = found.find("Deep").unwrap();
        match &deep.transport {
            Transport::Http { headers, .. } => {
                assert_eq!(headers["Authorization"], "Bearer t0k");
                assert_eq!(headers["X-Team"], "core");
                assert!(!headers.contains_key("X-Unset"), "环境里没有就不加");
            }
            other => panic!("{other:?}"),
        }

        let fs = found.find("fs").unwrap();
        assert!(fs.off_in_source);
        assert_eq!(fs.startup_timeout, Some(Duration::from_secs(20)));
        assert_eq!(fs.tool_timeout, Some(Duration::from_millis(2500)));
        assert!(fs.allows_tool("read_file"));
        assert!(!fs.allows_tool("list_directory"), "disabled_tools 优先");
        assert!(!fs.allows_tool("write_file"), "不在 enabled_tools 里");
        match &fs.transport {
            Transport::Stdio { args, cwd, .. } => {
                assert_eq!(args[2], "~/Projects", "~ 不展开，交给 server 自己");
                assert_eq!(cwd.as_deref(), Some(Path::new("/tmp")));
            }
            other => panic!("{other:?}"),
        }
        assert_eq!(fs.target(), "npx @modelcontextprotocol/server-filesystem");
    }

    #[test]
    fn a_variable_nobody_set_is_named_not_guessed() {
        let home = tempfile::tempdir().unwrap();
        write(
            home.path(),
            ".claude.json",
            r#"{ "mcpServers": {
                "gh": { "command": "npx", "args": ["${PKG:-server-github}"], "env": { "TOKEN": "${GH_TOKEN}" } },
                "empty": { "command": "x", "env": { "A": "${SET_EMPTY}", "B": "${SET_EMPTY:-fallback}" } }
            } }"#,
        );
        write(
            home.path(),
            ".codex/config.toml",
            "[mcp_servers.remote]\nurl = \"https://x.example/mcp\"\nbearer_token_env_var = \"NO_TOKEN\"\n",
        );
        let found = read(&places(home.path()), &env_of(&[("SET_EMPTY", "")]));
        let gh = found.find("gh").unwrap();
        assert_eq!(gh.missing_env, ["GH_TOKEN"]);
        match &gh.transport {
            Transport::Stdio { args, .. } => assert_eq!(args, &["server-github"]),
            other => panic!("{other:?}"),
        }
        let empty = found.find("empty").unwrap();
        assert!(empty.missing_env.is_empty(), "设成空串也算设了");
        match &empty.transport {
            Transport::Stdio { env, .. } => {
                assert_eq!(env["A"], "");
                assert_eq!(env["B"], "fallback", ":- 在空串时也用默认值");
            }
            other => panic!("{other:?}"),
        }
        assert_eq!(found.find("remote").unwrap().missing_env, ["NO_TOKEN"]);
    }

    #[test]
    fn a_broken_entry_is_reported_and_the_rest_still_read() {
        let home = tempfile::tempdir().unwrap();
        write(
            home.path(),
            ".claude.json",
            r#"{ "mcpServers": {
                "good": { "command": "a" },
                "no-way": { "args": ["x"] },
                "weird": { "type": "websocket", "url": "ws://x" },
                "bad-args": { "command": "a", "args": [1] }
            } }"#,
        );
        write(home.path(), ".codex/config.toml", "this is = = not toml");
        let found = read(&places(home.path()), &env_of(&[]));
        assert_eq!(found.servers.len(), 1);
        let mut messages: Vec<String> = found
            .problems
            .iter()
            .map(|p| format!("{:?}/{:?}: {}", p.source, p.server, p.message))
            .collect();
        messages.sort();
        assert_eq!(messages.len(), 4, "{messages:#?}");
        assert!(messages[0].contains("\"bad-args\""), "{messages:#?}");
        assert!(
            messages[1].contains("neither command nor url"),
            "{messages:#?}"
        );
        assert!(messages[2].contains("websocket"), "{messages:#?}");
        assert!(
            messages[3].starts_with("Codex/None: not valid TOML"),
            "{messages:#?}"
        );
    }

    /// 项目的 `.mcp.json` 排最前、同名压过装在账号上的（Claude Code 的
    /// project > user），写法和 `~/.claude.json` 一样、变量照样展开。
    #[test]
    fn a_projects_own_servers_come_first_and_win_a_name() {
        let home = tempfile::tempdir().unwrap();
        let project = tempfile::tempdir().unwrap();
        write(
            home.path(),
            ".claude.json",
            r#"{ "mcpServers": { "db": { "command": "user-db" }, "time": { "command": "uvx" } } }"#,
        );
        write(
            project.path(),
            ".mcp.json",
            r#"{ "mcpServers": { "db": { "command": "./tools/db-mcp", "env": { "URL": "${DB_URL:-sqlite://x}" } }, "broken": { "type": "ws" } } }"#,
        );
        let found = read(
            &places(home.path()).with_project(project.path()),
            &env_of(&[]),
        );
        let db = found.find("db").unwrap();
        assert_eq!(db.source, Source::Project);
        match &db.transport {
            Transport::Stdio { command, env, .. } => {
                assert_eq!(command, "./tools/db-mcp");
                assert_eq!(env["URL"], "sqlite://x");
            }
            other => panic!("{other:?}"),
        }
        assert_eq!(found.shadowed[0].source, Source::Claude);
        assert!(found.find("time").is_some());
        assert_eq!(found.problems.len(), 1);
        assert_eq!(found.problems[0].source, Source::Project);

        // 没说要读项目的，就不读。
        let found = read(&places(home.path()), &env_of(&[]));
        assert_eq!(found.find("db").unwrap().source, Source::Claude);
    }

    #[test]
    fn codex_home_moves_the_codex_file() {
        let home = tempfile::tempdir().unwrap();
        let elsewhere = tempfile::tempdir().unwrap();
        write(
            elsewhere.path(),
            "config.toml",
            "[mcp_servers.here]\ncommand = \"a\"\n",
        );
        write(
            home.path(),
            ".codex/config.toml",
            "[mcp_servers.not-here]\ncommand = \"a\"\n",
        );
        let found = read(
            &Places::new(home.path(), Some(elsewhere.path())),
            &env_of(&[]),
        );
        let names: Vec<&str> = found.servers.iter().map(|s| s.name.as_str()).collect();
        assert_eq!(names, ["here"]);
    }

    #[test]
    fn hosts_and_labels_leave_credentials_out() {
        assert_eq!(
            url_host("https://u:p@Mcp.Example.com:8443/x?k=1"),
            "mcp.example.com"
        );
        assert_eq!(url_host("http://[::1]:3000/mcp"), "::1");
        assert!(is_loopback(&url_host("http://127.0.0.1:3501/mcp")));
        assert!(is_loopback(&url_host("http://localhost/mcp")));
        assert!(!is_loopback(&url_host("https://mcp.exa.ai/mcp")));
        assert_eq!(
            display_url("https://user:secret@host.example/mcp/?key=abc#frag"),
            "https://host.example/mcp/"
        );
    }
}
