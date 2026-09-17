# MCP Host 怎么对待工具结果里的图片和文件（原始记录）

对应 [v3 方案](../../../docs/plan/implementation-plan-v3-native-parity.md) 第 4.2 节第 4–6 项，以及 ccnm 的 P39.1。结论对设计的影响写在 ccnm 的研究记录里，这里只放脚本和结果。**全程零模型额度**，只在本机（macOS arm64）跑。

## 要回答的问题

ccnm 想加一个 `view_image` 工具，把 Runtime 上的图片交给模型；PDF 和 notebook 里也可能有图。MCP 工具结果里能放 `image` 块，也能放 `resource` 块（`blob` 是 base64 的任意文件）。两个 Host 分别把它们变成了什么、有没有大小上限、会不会缩放，得看实际行为。

## 怎么测的

| 文件 | 做什么 |
| --- | --- |
| `media_server.py` | 探针 MCP server。工具 `media_probe` 按参数返回一种内容块：`image`（2×2 PNG）、`resource_png`（同一张 PNG 放进 resource blob）、`resource_pdf`（最小 PDF 放进 resource blob），都带文本标记 |
| `run_codex.py` | Codex 连探针，**不开 Code Mode**。本机假模型第一轮回一个 `function_call` 去调 `media_probe`，第二轮把 Codex 发来的请求存盘，看工具结果变成了什么。三种内容块各跑一次 |
| `run_codex_code_mode.py` | 同上，但加上 ccnm 受管 Codex 会话用的 `--enable code_mode_only` 和 `excluded_tool_namespaces`。假模型第一轮给 `exec` 写一段 JS：调工具，再 `image(r.content[1])` |
| `runs/*.json` | 两个脚本的汇总（base64 截短）。原始请求不入库 |

Codex 的隔离和 [skills-surface](../skills-surface/README.md) 一样：`sandbox-exec` 禁非本机出站，`HOME` / `CODEX_HOME` 是临时空目录，模型接口是本机假服务。

Claude Code 没登录就发不出模型请求，看不到工具结果最终的样子，所以只有静态证据（下面）。

```bash
python3 run_codex.py <仓库外的新目录>
python3 run_codex_code_mode.py <仓库外的新目录>
```

## 结果：Codex 0.154.0

| 内容块 | 不开 Code Mode（`runs/codex-0.154.0-direct.json`） | Code Mode（`runs/codex-0.154.0-code-mode.json`） |
| --- | --- | --- |
| `image` | 变成 `{"type":"input_image","image_url":"data:image/png;base64,…","detail":"high"}`，**模型看得到图** | 工具结果是 JS 对象；脚本调 `image(r.content[1])` 后变成 `input_image`，**模型看得到图**。不调就只是一个对象 |
| `resource`（blob 是 PNG） | 整个块被 JSON 序列化成一段 `input_text`，base64 原样塞进上下文 | 没测 |
| `resource`（blob 是 PDF） | 同上 | 没测 |

补充观察：

- 不开 Code Mode 时，MCP 工具放在 `{"type":"namespace","name":"mcp__media"}` 里；调用要写 `"namespace":"mcp__media","name":"media_probe"`，只写 `"name":"media_probe"` 会得到 `unsupported call: media_probe`。
- Code Mode 的 `exec` 说明里写着：`image()` 可以直接接收 MCP 的 `ImageContent` 块；图片块可以用 `_meta: {"codex/imageDetail": "original"}` 要求原始清晰度。
- Code Mode 不指定模型时请求里的模型是 `gpt-6-astra`（CLI 默认），工具列表在 `input` 的 `additional_tools` 项里。

## 结果：Claude Code 2.1.273（静态证据）

`strings` 二进制后读打包 JS。

**MCP 工具结果逐块转换**（原文，去掉无关分支）：

```js
case"image":{if(go(e.mimeType)){let{block:h}=await Hg({data:String(e.data),mediaType:e.mimeType,limits:r});return[h]}
  return await kn(Buffer.from(String(e.data),"base64"),e.mimeType,n,`[Image from ${n}] `,s)}
case"resource":{… else if("blob"in h)if(go(h.mimeType)){…Hg(…)…}
  else return await kn(Buffer.from(h.blob,"base64"),h.mimeType,n,g,s); …}
// kn：把二进制写到本机磁盘上的一个文件，给模型的是"文件存在哪"这句话
```

- 支持的图片类型进 `Hg`：用 sharp 读尺寸，超过 `maxWidth` / `maxHeight`（**2000 × 2000**）或字节预算就缩放、转 JPEG 压缩；再超过 `cge`（**512000 字节**）再压一次。上限常量：`{maxWidth:2000,maxHeight:2000,maxBase64Size:5242880,targetRawSize:3932160}`。
- **不支持的图片类型，和任何非图片的 resource blob（比如 PDF），被写到跑 Claude Code 的那台机器的磁盘上**，模型拿到的只是本机路径。对 ccnm 受管会话（本机 Read 关着）等于没用，而且是把 Runtime 上的文件内容落到了 Agent 机器上。
- 大小估算：每张图按 1600 token 算；整个结果超过 `MAX_MCP_OUTPUT_TOKENS`（默认 25000）时，文本截断、图片逐张压缩到剩余预算。

## 对设计的直接影响

1. 图片用 `image` 块，不用 `resource` 块：两个 Host 都只把 `image` 块当图片。
2. ccnm 不必自己缩放：Claude Code 会缩放压缩；Codex 原样转发（服务端怎么计费、是否缩放不在本机可见范围）。ccnm 只需要一个字节上限，Claude Code 的 `maxBase64Size` 是 5 MiB（原始字节约 3.75 MiB）。
3. 只发 PNG / JPEG / GIF / WebP：Claude Code 对别的类型会把文件落到 Agent 机器的磁盘上。
4. PDF 不能靠 `resource` blob 交出去（Codex 塞 base64 文本、Claude Code 落盘），只能在 Runtime 上转成文本或页面图片再发。
5. Codex Code Mode 下模型要自己调 `image()`，工具说明里值得提一句。

## 范围

只测了这两个版本。Claude Code 没有模型请求可看，图片最终进没进上下文是从代码推断的。Codex 服务端对 `detail: high` 的计费和缩放不可见。
