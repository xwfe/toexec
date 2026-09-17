# 使用说明

两个 crate 怎么用、每个参数什么意思、哪些事它们**不管**。引用方式（`Cargo.toml` 那两行）在 [README](../README.md#快速使用)。

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

- **临时文件叫什么、什么时候清理。** 两个产品的命名规则不同，各自的残留清理逻辑也依赖那个名字。
- **备份和回滚。** 多文件补丁中途失败怎么恢复，ccnm 和 gld 的做法差很多，不在这里。
- **并发。** 两个进程同时替换同一个目标，谁后 rename 谁赢；需要互斥的话自己加锁。

### 已知边界

- **Windows 上的替换不是原子的。** 那里 `rename` 到一个已存在的文件会失败，所以 `replace` 先删再 rename，两步之间有一个目标不存在的窗口。标准库没有跨平台的原子替换，真要做得调 `ReplaceFileW`。
- **没有 fsync 父目录。** `rename` 本身原子，但"rename 这件事"要在断电后仍然可见，还得 fsync 目标的父目录。这里没做，和两个产品原来的行为一致；补上意味着每次替换多一次 fsync，是另一个决定。
- **两个路径必须在同一个文件系统上**，否则 `rename` 报 `EXDEV`（"Invalid cross-device link" / "Cross-device link"）。把临时文件建在目标同目录就不会遇到。
