# 使用说明

三个 crate 怎么用、每个参数什么意思、哪些事它们**不管**。引用方式（`Cargo.toml` 那几行）在 [README](../README.md#快速使用)。

文中的例子都实际编译、运行过。

## toexec-text：读一行，但不把整行读进内存

### 怎么用

```rust
use std::fs::File;
use std::io::BufReader;
use std::path::Path;

use toexec_text::{LineLimits, Terminator, next_line};

const MAX_SCAN: u64 = 64 * 1024 * 1024;

/// 逐行打印一个文件：每行最多留 4096 字节，累计走过 64 MiB 就放弃。
fn print_lines(path: &Path) -> std::io::Result<()> {
    let mut reader = BufReader::new(File::open(path)?);
    let limits = LineLimits { keep: 4096, scan_limit: Some(MAX_SCAN) };
    let mut raw = Vec::new(); // 每行复用同一块内存
    let mut scanned = 0u64;   // 跨行累计，由调用方持有

    loop {
        raw.clear();
        let Some(ending) = next_line(&mut reader, &mut raw, limits, &mut scanned)? else {
            break; // 文件读完
        };
        if scanned > MAX_SCAN {
            eprintln!("文件太大，放弃");
            break;
        }
        let tail = match ending {
            Terminator::Crlf => "\\r\\n",
            Terminator::Lf => "\\n",
            Terminator::None => "(没有换行符)",
        };
        println!("{} {tail}", String::from_utf8_lossy(&raw));
    }
    Ok(())
}
```

对一个内容是 `第一行\r\n第二行\n没换行的尾巴` 的文件，输出：

```text
第一行 \r\n
第二行 \n
没换行的尾巴 (没有换行符)
```

### 参数和返回值

| 名字 | 含义 |
| --- | --- |
| `LineLimits::keep` | 这一行最多**保留**多少字节。超出的部分照样读过去、计数，但不进内存 |
| `LineLimits::scan_limit` | 累计走过多少字节就放弃当前行；`None` 是不限 |
| `raw` | 这一行的内容写到这里。**调用方自己 `clear()`**，这样一块内存可以一直复用 |
| `scanned` | 走过的总字节数，**跨行累计**，所以也由调用方持有 |
| 返回 `Ok(None)` | 文件读完了 |
| 返回 `Ok(Some(Terminator))` | 读到一行，以及它是怎么结束的：`Crlf`、`Lf`，或 `None` |

### 容易踩的几处

- **换行符不在 `raw` 里。** 一行被截断之后，真正的 `\r\n` 可能在保留下来那一截之后好几兆，所以结束方式单独用 `Terminator` 报出来。要原样还原文件，得自己按 `Terminator` 把换行符补回去。
- **`Terminator::None` 有两种来历**：文件最后一行本来就没换行符，或者这一行还没走到头就撞上了 `scan_limit`。区分办法就是例子里那句 `if scanned > MAX_SCAN`——撞了上限就该停手并报错；不停的话，下一次调用会把同一行剩下的部分当成新的一行继续返回。
- **截断不会切在半个字符上。** `keep` 的切口如果落在一个多字节 UTF-8 字符中间，那半个字符会被去掉，所以被截断的行不会被误判成"非法 UTF-8"。没被截断的行原样交回——里面真有非法 UTF-8 时怎么办（报错还是有损替换）由你决定，这个库不替你选。
- **它不知道这一行有没有被截断。** 需要的话自己比：`keep` 给得比你要展示的上限多几个字节，`raw.len()` 超过展示上限就是截断了（ccnm 的 `read_file` 就是这么做的，`keep = 展示上限 + 8`）。

## toexec-fs：原子文件替换

### 怎么用

```rust
use std::fs;
use std::path::Path;

use toexec_fs::{Step, replace, write_durable};

/// 把 `target` 的内容换成 `bytes`。任何时刻目标要么是旧内容，要么是新内容。
fn overwrite(target: &Path, bytes: &[u8]) -> Result<(), String> {
    // 临时文件建在目标同一个目录里：rename 不能跨文件系统。名字由你自己定。
    let temp = target.with_extension("tmp-1234");
    // 把原文件的权限带过去，不然脚本会丢掉可执行位。
    let mode = fs::metadata(target).ok().map(|m| m.permissions());

    write_durable(&temp, bytes, mode.as_ref()).map_err(|e| match e.step {
        // 建不出、写不进：多半是路径或权限，调用方改得了
        Step::Create | Step::Write => format!("参数问题：{e}"),
        // 刷盘、设权限失败：机器的事
        Step::Sync | Step::Permissions => format!("内部错误：{e}"),
    })?;
    replace(&temp, target).map_err(|e| {
        let _ = fs::remove_file(&temp); // 清理临时文件也是调用方的事
        format!("替换失败：{e}")
    })
}
```

目标所在目录不存在时，上面的函数返回：

```text
参数问题：cannot create the file: No such file or directory (os error 2)
```

### 两步各做什么

| 函数 | 做什么 | 为什么 |
| --- | --- | --- |
| `write_durable(path, bytes, mode)` | 建文件 → 写内容 → `fsync` → 设权限，**内容落盘之后才返回** | 少了 `fsync`，后面的 rename 可能先持久化、内容还没有。断电后留下一个名字对、长度错的文件——比写失败更难发现，因为它看起来是成功的 |
| `replace(temp, target)` | 一次 `rename` 把临时文件顶替成目标 | 只有一次系统调用，中间没有"目标不存在"的瞬间，目标的名字上也不会出现写了一半的内容 |

`write_durable` 失败时返回 `WriteError { step, source }`：`step` 说是哪一步（`Create` / `Write` / `Sync` / `Permissions`），`source` 是原始的 `io::Error`。不关心是哪一步的话直接用 `?`，它能转成 `io::Error`。

为什么要分步报：0.1.0 把四步失败合成一个 `io::Error`，接进 ccnm 时才发现那会把"刷盘失败"从内部错误悄悄变成参数错误——而这两种错误码在 ccnm 的 MCP 协议里含义不同。所以 0.2.0 改成报出事实，怎么归类由调用方定。

### 这个 crate 不管的事

- **临时文件叫什么、什么时候清理。** 两个产品的命名规则不同，各自的残留清理逻辑也依赖那个名字。上面例子里的 `tmp-1234` 只是占个位：目标目录别人也能写的时候，固定名字会被人提前占住（换成指向别处的符号链接，或者一个他自己开着的文件），你的内容就写到别处去了。那种场景要用带随机后缀、以"不存在才创建"（`create_new`）建出来的临时文件——gld 用的是 UUID。
- **备份和回滚。** 多文件补丁中途失败怎么恢复，ccnm 和 gld 的做法差很多，不在这里。
- **并发。** 两个进程同时替换同一个目标，谁后 rename 谁赢；需要互斥的话自己加锁。

### 已知边界

- **失败了，旧目标一定还在——这条不退让。** `replace` 只做一次 `rename`，没有任何"先把目标删掉再试"的兜底。所以拿到 `Err` 的时候磁盘上还是旧内容，临时文件也还在（清它是你的事）。
  > 0.2.0 及以前不是这样：Windows 分支先 `remove_file(target)` 再 rename，只要后一步失败，旧文件就没了。那段代码的依据（"Windows 上 rename 到已存在的文件会失败"）说的是 C 的 `rename()` 和不带 flag 的 `MoveFileW`，对 Rust 的 std 不成立——std 的文档写的是"目标已存在就顶替掉"，实现是 `MoveFileExW(.., MOVEFILE_REPLACE_EXISTING)`，撞上 `ERROR_ACCESS_DENIED`（比如目标是只读文件）还会用 `SetFileInformationByHandle` + `FileRenameInfoEx` 再试一次。
- **Windows 上替换不了带只读属性的目标**，报 `Access is denied. (os error 5)`。std 那次重试只带了 `REPLACE_IF_EXISTS | POSIX_SEMANTICS`，没带 `FILE_RENAME_FLAG_IGNORE_READONLY_ATTRIBUTE`。要覆盖这种文件，你得自己先摘掉只读属性再替换。Unix 没这问题——那里 rename 看的是父目录的权限，目标 0o444 也照样换得掉。这两种行为各有一条测试钉着（`a_read_only_target_is_replaced_on_unix_and_refused_on_windows`）。
- **别把它当成"任何文件系统上都原子安全"。** 成立的是：没有先删那一步，也就没有目标不存在的窗口；失败不动旧文件。不成立的是跨所有 Windows 文件系统的原子替换保证——`FileRenameInfoEx` 要 Windows 10 1607 以上并且文件系统支持，退回 `MoveFileExW` 时目标不能是目录。目标正被别人以不许删除的方式打开（Windows 上没带 `FILE_SHARE_DELETE`）时替换会失败，这符合上面那条，但你得自己处理这个错误。
- **没有 fsync 父目录。** `rename` 本身原子，但"rename 这件事"要在断电后仍然可见，还得 fsync 目标的父目录。这里没做，和两个产品原来的行为一致；补上意味着每次替换多一次 fsync，是另一个决定。
- **两个路径必须在同一个文件系统上**，否则 `rename` 报 `EXDEV`（"Invalid cross-device link" / "Cross-device link"）。把临时文件建在目标同目录就不会遇到。

## toexec-skill：把一份 SKILL.md 读明白

skill 是一个目录，里面一份 `SKILL.md`：开头两条 `---` 之间是 YAML（叫 frontmatter，写着名字、描述、参数），后面是给模型看的正文。`.claude/commands/*.md` 是同一种格式。

### 怎么用

```rust
use toexec_skill::{args, frontmatter, inject};

let raw = std::fs::read_to_string(".claude/skills/deploy/SKILL.md")?;

// 1. 拆开，读 frontmatter。没有 frontmatter 的文件整个是正文，这是合法的。
let (front, body) = frontmatter::split(&raw);
let meta = match front {
    Some(text) => frontmatter::parse(text)?,   // 读不了会说是第几行、哪一类问题
    None => Default::default(),
};
let description = meta.text("description");            // 多行写法也是一整段
let hint = meta.string("argument-hint");               // `[issue-number]` 这种列表写法也有文字
let hidden = meta.flag("disable-model-invocation") == Some(true);
let user_invocable =                                   // 两个开关的默认值不对称，见下文
    meta.get("user-invocable").is_none() || meta.flag("user-invocable") == Some(true);
let names = meta.words("arguments");                   // 列表或 "a b" 字符串都行
if meta.reading() == frontmatter::Reading::Lenient {
    // Claude Code 读不了这份 frontmatter，会把它整个当空的；值得告诉作者。
}

// 2. 用户敲的是 `/deploy staging "two words"`：把参数填进正文。
let context = args::Context { skill_dir: Some(".claude/skills/deploy"), ..Default::default() };
let filled = args::substitute(body, r#"staging "two words""#, &names, context);

// 3. 正文里有没有要求「加载时先执行」的命令。
for found in inject::find(body) {
    println!("line {}: {}", found.line, found.command);
}
```

### 三块各自的规矩

| 模块 | 规矩 |
| --- | --- |
| `frontmatter` | **读法照 Claude Code 2.1.278**：先严格按 YAML 读；读不了，照宿主的规则给顶层带特殊字符的值加引号、行首 tab 换空格再读；还读不了（宿主此时把整段当空的），用宽松读法读出来，`reading()` 标成 `Lenient`。读得了：单行值、引号（可跨行）、`>` / `|` 块标量、`- ` 列表、缩进嵌套、`[a, b]` 和 `{k: v}`（可跨行、嵌套）。仍然报错的：引号没闭合、转义写错、缩进里的 tab、锚点/别名/标签出现在嵌套的位置、嵌套超过 32 层 |
| `args` | 对的是 Claude Code 2.1.273 的实际行为，不只是文档：`$N` 和 `$ARGUMENTS[N]` 没给到就**原样留着**；声明过的 `$name` 没给到是空串；`\$0` 转义；一个都没换成而又给了参数，就在末尾补 `ARGUMENTS: …` |
| `inject` | `` !`cmd` `` 和 ```` ```! ```` 代码块。普通代码块里的不算——文档里举例写一个，不是在要求执行它 |

### 容易踩的几处

- **`$1` 没给参数时留着不换，是故意的。** skill 正文里经常有 `awk '{print $1}'` 这种 shell 片段；换成空串，脚本就坏了，而且坏得无声无息。
- **`inject::find` 只找不跑。** 跑不跑、以谁的身份跑，是产品的安全决定，不是解析细节。ccnm 就不自动执行：一次"读 skill"的调用不该触发项目指定的命令。
- **一个键写了两遍，后写的赢**，和宿主一样；0.1.0 是先写的赢，`disable-model-invocation` 写两遍时两边读出相反的值。一字不差的键优先于只差大小写或 `-`/`_` 的写法。`duplicates()` 列出这些重名键和行号，拿去提醒作者。
- **`user-invocable` 写了就只有 true 才算 true。** 宿主把空值、`[]`、认不出的字都当 false，skill 从 `/` 菜单里消失；`disable-model-invocation` 反过来，只有 true 才生效。所以两个开关要分开写：`get(k).is_none() || flag(k) == Some(true)` 和 `flag(k) == Some(true)`。`flag` 认 `yes`/`no`/`on`/`off`/`1`/`0`。
- **显示用的字段用 `string`，不用 `text`。** 官方例子 `argument-hint: [issue-number]` 在 YAML 里是列表，`text` 给不出文字，`string` 给 `issue-number`（宿主的显示）。`[filename] [format]` 不是合法的 YAML，宿主加引号重读，两个方法都给原文。
- **`words` 只按空白切**（文档原话 "space-separated"），逗号是名字的一部分，纯数字的名字丢掉——和宿主一样。
- **frontmatter 读不了时，整个文件的 frontmatter 都拿不到**，不会给你读了一半的结果。`ParseError` 里有行号（frontmatter 里的第几行）和分类，报错怎么措辞由你定。
- **`hooks:` 这种三层嵌套也读得进来**，哪怕你根本不用它：读不过去的话，带 hooks 的 skill 连名字都拿不到。

### 这个 crate 不管的事

去哪些目录找 skill、最多收多少个、目录怎么排版、经什么通道交给模型。两个产品在这几件事上各不相同。

### 拿什么验的

拿 Claude Code 自己的解析器做差分（借它内嵌的 Bun 运行时，零额度）：6 个公开仓库（按提交号钉住，能复验）加一台开发机上共 1686 个文件，以及两批各 4000 个按片段拼出的畸形输入，0.2.0 和宿主之间说不清的分歧是 0，剩下的归到 9 个有意不跟的原因。做法、结果和每个原因为什么不跟见 [`evidence/x08-skill-frontmatter/`](../evidence/x08-skill-frontmatter/README.md)。

另外两份测试在 crate 里，`cargo test` 就跑：固定种子的变异模糊测试（不崩、不卡），和 1 MiB 级畸形输入必须线性时间读完——0.1.0 在跨行引号串上是平方级，416 KB 要 10 秒。

## toexec-agents：`~/.agents/mcp.json` 说某个工具、某个 skill 给不给

文件格式、四档的含义和为什么这样定见 [RFC-0001](rfc/0001-agents-exposure-policy.md)。这里只讲代码怎么接。

### 怎么用

```rust
use toexec_agents::{path_in, Policy, SkillLevel};

// 1. 读。文件不存在得到空规则；写坏了是 Err，别退回"不收窄"。
let policy = Policy::load(&path_in(&home))?;
let rules = policy.for_server("gld");          // 条目名固定是产品名

// 2. 工具：先按产品自己的规则得出上限，再问这里。列表和调用两处都要问。
let exposed: Vec<&str> = own_tools.iter().copied().filter(|t| rules.tool_allowed(t)).collect();

// 3. skill：产品条目里写的优先，其次顶层，都没写是 On。和 frontmatter 取更严的那个。
let from_frontmatter = if disable_model_invocation { SkillLevel::UserInvocableOnly } else { SkillLevel::On };
let level = rules.skill_level(&name).min(from_frontmatter);
if level.listed() { /* 进目录；level.described() 为假时只放名字 */ }

// 4. doctor：写错的工具名要报出来——写错在 disabledTools 里等于想关的还开着。
let typos = rules.unknown_tools(&ALL_TOOL_NAMES);
```

### 容易踩的几处

- **坏文件不能当成空文件。** `load` 只在"文件不存在"时给空规则；JSON 写坏、类型不对、档位拼错都返回错误，位置写成 `mcpServers.gld.disabledTools[1]` 或行列号。产品应当拒绝服务并把这句话给用户看，而不是忽略它——忽略等于把用户想关的工具又打开了。
- **被关的工具调用时也要拒。** 客户端可能缓存着旧的工具表，或者模型照记忆直接按名字调。
- **`min` 是"取更严的"。** 档位从严到宽排（`Off < UserInvocableOnly < NameOnly < On`），所以写 `"on"` 放不开 frontmatter 说只给用户的 skill。
- **别的键一律不看**：`mcpServers.context7.command`、顶层的其他键都被忽略，这一版不启动也不代理别的 server。但 `mcpServers` 下的每个条目都必须是对象。

### 这个 crate 不管的事

去哪个 HOME 找文件、多久重读一次、报错怎么措辞、被关掉的工具调用时回什么。
