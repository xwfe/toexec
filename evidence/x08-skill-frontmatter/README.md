# X08：skill frontmatter 的语料、差分和模糊测试

回答一个问题：**同一个 SKILL.md，`toexec-skill` 读出来的和 Claude Code 自己读出来的一样吗？** 不一样的地方，哪些是有意的、哪些是 bug。对应 [跨仓评审](../../docs/plan/2026-09-19-cross-project-refactor-review.md) 的 X08。零额度。

结论先说：修完之后，6 个公开仓库的 711 个文件和本机 975 个文件里**没有一处说不清的分歧**；两批各 4000 个生成的畸形输入里也是 0，剩下的都归到下面 9 个有名有姓的原因里。

## 怎么跑

要三样东西：Rust 工具链、Claude Code 2.1.278 的二进制、Python 3。

```bash
# 1. 公开语料：按 runs/corpus.json 里记的仓库和提交号取回来
git clone https://github.com/anthropics/skills.git /tmp/x08/anthropics_skills
git -C /tmp/x08/anthropics_skills checkout 34040c9c568585f6929bedeaad110ad08f079624
#    其余五个同理，提交号都在 runs/corpus.json 的 source 里

# 2. 在 toexec 仓库根目录跑差分（第一次会编一个 release 例程）
python3 evidence/x08-skill-frontmatter/compare.py \
  --claude /opt/homebrew/Caskroom/claude-code@latest/2.1.278/claude \
  --corpus anthropics/skills=/tmp/x08/anthropics_skills \
  --private local-claude-home=~/.claude \
  --out /tmp/x08/corpus.json

# 3. 生成的畸形输入：同一个 seed 永远生成同一批
python3 evidence/x08-skill-frontmatter/gen_cases.py --seed 1 --count 4000 --out /tmp/x08/gen1
python3 evidence/x08-skill-frontmatter/compare.py \
  --claude /opt/homebrew/Caskroom/claude-code@latest/2.1.278/claude \
  --files generated-seed1=/tmp/x08/gen1/paths.txt --out /tmp/x08/gen1.json
```

`--private` 的语料只记计数，不记路径和内容；`--detail 文件` 会把每一处分歧连同 frontmatter 原文写成 JSONL，排查用，不提交。

**报 `host_oracle.js 没有写出结果`**：说明 Claude Code 换了运行时、不再认 `BUN_OPTIONS`，见下一节。**`claude --version` 正常打印了版本号却没有结果文件**，是同一个原因。

## 对照用的是宿主自己的解析器

Claude Code 2.1.278 是一个 Bun 打包的单文件程序，内嵌 **Bun 1.4.3（8c28b31）**——GitHub 上最新公开版只到 1.4.2，装不到同一个。所以不装：`BUN_OPTIONS="--preload=host_oracle.js" claude --version` 让脚本在 Claude Code 的主程序之前跑，拿它的 `Bun.YAML` 读完所有文件后 `process.exit(0)`。Claude Code 本身一行没执行：不联网、不读登录、不花额度。

`host_oracle.js` 开头写着它复刻的三段宿主逻辑（拆分、两步解析、取字段），都是静态读 2.1.278 打包 JS 得来的。换了 Claude Code 版本要重新核对这三段，结论才算数。

两边的比较分四层，只有前一层一样才比下一层（不然同一处分歧会被数好几遍）：

| 层 | 比什么 |
| --- | --- |
| split | 拆出来的 frontmatter 原文 |
| parse | 读法：两边都是合规 YAML / 都是加引号重读 / 宿主整段丢弃而我们宽松读出 / 其他组合 |
| tree | 读出来的整棵值（映射后写的赢、数字比数值） |
| field | 产品实际会用的 7 个字段：name、description、when_to_use、argument-hint、两个开关、arguments |

## 结果（`runs/`）

| 文件 | 输入 | 读法一致 | 有意的不同 | 说不清 |
| --- | --- | --- | --- | --- |
| `corpus.json` | 6 个公开仓库 711 个 + 本机 975 个 | 1504 | 6（都是两边都读不了） | 0 |
| `generated-seed1.json` | 生成的 4000 个 | 1805 | 见文件 | 0 |
| `generated-seed2.json` | 生成的 4000 个 | 1786 | 见文件 | 0 |

语料里有 176 个文件没有 frontmatter，不在上面三列里。「两边都读不了」的 6 个是 agent 定义文件，描述里有没缩进的 `<example>` 行，YAML 本身就不合法：宿主静默丢弃，这里报错并给行号。

**修之前（0.1.0）同一批语料上的分歧**：`anthropics/claude-code` 和 `claude-plugins-official` 里的 `commands/new-sdk-app.md` 写的是 `argument-hint: [project-name]`，宿主显示 `project-name`，0.1.0 给不出文字；`agents/silent-failure-hunter.md` 的描述里有 `PR #1234`，这一行还夹着 `Daisy: "…"` 这种冒号，YAML 读不了，宿主走了加引号那一步、保留整句，0.1.0 把 `#` 后面当注释截掉了一半。本机那两处是同样两个文件（`~/.claude/plugins/marketplaces/` 里有 claude-plugins-official 的副本）。生成的输入上分歧多得多，改动和理由都记在 toexec 的提交 `f0f7548` 里。

## 剩下的分歧，以及为什么不跟

`compare.py` 的 `CAUSES` 里是同一份清单，结果文件的 `explained` 按它计数：

| 原因 | 为什么不跟 |
| --- | --- |
| 宿主两步都读不了、整段丢弃；我们宽松读出 | 宿主丢弃时 skill 照样出现，但名字、描述、开关全丢——`disable-model-invocation: true` 也跟着没了。宽松读出更接近作者本意，对开关是更保守的一侧；`Frontmatter::reading()` 标成 `Lenient`，产品可以提醒作者修 |
| 两边都读不了 | 一致，只是宿主静默、我们报行号 |
| 值里有 `---`，宿主在那里截断 | 宿主用正则找第一个 `---`（它自己在另一个模式下也提示这是隐患），截断后写在后面的开关全丢。我们要求 `---` 独占一行 |
| 锚点 `&x`、别名 `*x`、标签 `!x` | 真实语料里 0 次。顶层的会在第二步被宿主的规则加上引号读成文字（`description: *Bold* first` 这种 markdown 写法因此读得出来）；嵌套的报 Unsupported |
| 键名大小写、`-`/`_` 不同 | 宿主只认原样的键，`Disable_Model_Invocation: true` 在它那里无效；我们认，因为官方文档两种拼法都用。一字不差的键优先，所以只有在宿主那边没有这个键时才会不同 |
| 数字保留原文 | `version: 1.10` 在宿主里成了 `1.1`。保留原文是 0.1.0 就有的决定 |
| 映射、布尔当文字用 | 宿主得到 `[object Object]` / `"false"`，没有意义 |
| 行首 tab：Bun 的判断依赖前文 | `a: 'x'\n\t\nb: 2` 它放行，前面多一个 `b: 'y'` 就报错。我们按 YAML：空行和注释行前面的 tab 放行，缩进里的 tab 报错 |
| CRLF 文件里跨行的引号串 | Bun 把换行留成 `\n`，YAML 规定折成空格（LF 文件、普通标量、`>` 块它都折得对）。只差描述里多一个换行，产品本来就会把描述压成一行 |

## 模糊测试在哪

不在这个目录。crate 里有两份，`cargo test` 就会跑：

- `crates/toexec-skill/tests/fuzz.rs`：零依赖、固定种子的变异测试，查不崩、不卡、行号在范围里、同一输入读两遍一样。默认 2 万个；release 下 4 个种子各 50 万个跑过，没有崩溃或超时。同一套测试放到 0.1.0 上也没崩——它的问题不在这里。
- `crates/toexec-skill/tests/large_input.rs`：1 MiB 级的畸形输入必须线性时间读完。0.1.0 在跨行引号串和括号上是平方级：416 KB、引号不闭合要 10.05 秒，448 KB、`[` 不闭合要 8.78 秒（release）；现在分别是 10 和 13 毫秒。
