// Claude Code 读 SKILL.md 的那一套，跑在 Claude Code 自己的运行时里。
//
// 怎么跑（compare.py 会替你拼）：
//
//   X08_PATHS=paths.txt X08_OUT=host.jsonl \
//   BUN_OPTIONS="--preload=$PWD/host_oracle.js" claude --version </dev/null
//
// 为什么不直接装一个 bun：Claude Code 2.1.278 内嵌的是 Bun 1.4.3（8c28b31），
// GitHub 上最新公开版只到 1.4.2。`BUN_OPTIONS=--preload` 让这个脚本在它的主
// 程序之前跑，最后 process.exit(0)，Claude Code 本身一行都没执行：不联网、
// 不读登录、不花额度。拿到的 `Bun.YAML` 就是宿主解析 skill 时用的那一个。
//
// 下面几段是照 2.1.278 打包 JS 里的实际行为重写的（静态读出来的，不是文档）：
//
// 1. 拆分：去掉 BOM，正则 /^---\s*\n([\s\S]*?)---\s*\n?/。注意收尾的 `---`
//    不要求独占一行——值里出现 `---` 就会在那里截断（宿主自己也在
//    quoteLossyValues 里提示这件事）。`...` 不算收尾。
// 2. 解析：Bun.YAML.parse；失败就把「顶层 `key: value` 且值里有特殊字符」的
//    行加上双引号、行首 tab 换成两个空格再试一次；还失败就当 frontmatter 是
//    空的，skill 照样加载（日志里记一条 "failed to parse and was ignored"）。
//    解析结果不是映射（列表、标量）也当空的。
// 3. 取字段：键名原样查，不归一化（ts() 的 normalizeKeys 在 2.1.278 里是
//    恒等函数）；重复键是 Bun.YAML 决定的：后写的赢。布尔字段认
//    1/true/yes/on 和 0/false/no/off（不分大小写、去空白），别的值当没写。

const fs = require("fs");

const FRONT = /^---\s*\n([\s\S]*?)---\s*\n?/;
const SPECIAL = /[{}[\]*&#!|>%@`]|: /;

function split(raw) {
  const text = raw.charCodeAt(0) === 0xfeff ? raw.slice(1) : raw;
  const m = text.match(FRONT);
  return m ? m[1] || "" : null;
}

// 第二次尝试之前宿主对原文做的改写。
function requote(front) {
  return front
    .split("\n")
    .map((line) => {
      const m = line.match(/^([a-zA-Z_-]+):\s+(.+)$/);
      if (!m) return line;
      const [, key, value] = m;
      const quoted =
        (value.startsWith('"') && value.endsWith('"')) ||
        (value.startsWith("'") && value.endsWith("'"));
      if (quoted) return line;
      if (value.startsWith("[") && value.endsWith("]")) {
        try {
          if (Array.isArray(Bun.YAML.parse(value))) return line;
        } catch {}
      }
      if (!SPECIAL.test(value)) return line;
      const escaped = value.replaceAll("\\", "\\\\").replaceAll('"', '\\"');
      return `${key}: "${escaped}"`;
    })
    .join("\n")
    .replace(/^\t+/gm, (tabs) => "  ".repeat(tabs.length));
}

function parse(front) {
  let first;
  try {
    return { stage: "direct", value: Bun.YAML.parse(front) };
  } catch (e) {
    first = String(e.message || e);
  }
  try {
    return { stage: "requoted", value: Bun.YAML.parse(requote(front)), first };
  } catch (e) {
    return { stage: "failed", error: String(e.message || e), first };
  }
}

const asMap = (v) => (v && typeof v === "object" && !Array.isArray(v) ? v : {});
const str = (v) => (v == null ? null : String(v));
function description(v) {
  if (v == null) return null;
  if (typeof v === "string") return v.trim() || null;
  if (typeof v === "number" || typeof v === "boolean") return String(v);
  return null;
}
// 布尔字段：返回 true / false / null（null = 宿主当没写）。
function flag(v) {
  if (typeof v === "boolean") return v;
  if (typeof v !== "string" && typeof v !== "number") return null;
  const n = String(v).toLowerCase().trim();
  if (["1", "true", "yes", "on"].includes(n)) return true;
  if (["0", "false", "no", "off"].includes(n)) return false;
  return null;
}
function words(v) {
  if (!v) return [];
  const ok = (w) => typeof w === "string" && w.trim() !== "" && !/^\d+$/.test(w);
  if (Array.isArray(v)) return v.filter(ok);
  if (typeof v === "string") return v.split(/\s+/).filter(ok);
  return [];
}

// JSON 表达不了的值换成能比的形状：NaN/Infinity 记成字符串，Date 记成 ISO。
function plain(v) {
  if (typeof v === "number" && !Number.isFinite(v)) return { num: String(v) };
  if (v instanceof Date) return { date: v.toISOString() };
  if (Array.isArray(v)) return v.map(plain);
  if (v && typeof v === "object") {
    const out = {};
    for (const [k, x] of Object.entries(v)) out[k] = plain(x);
    return out;
  }
  if (typeof v === "bigint") return { num: v.toString() };
  return v;
}

function one(path) {
  let raw;
  try {
    raw = fs.readFileSync(path, "utf8");
  } catch (e) {
    return { path, skip: String(e.message || e) };
  }
  const front = split(raw);
  const out = { path, front };
  let fm = {};
  if (front !== null) {
    const r = parse(front);
    out.stage = r.stage;
    if (r.first) out.first_error = r.first;
    if (r.stage === "failed") out.error = r.error;
    else {
      out.value = plain(r.value);
      fm = asMap(r.value);
    }
  }
  out.fields = {
    name: str(fm.name),
    description: description(fm.description),
    when_to_use: str(fm.when_to_use),
    argument_hint: str(fm["argument-hint"]),
    disable_model_invocation: flag(fm["disable-model-invocation"]),
    user_invocable: fm["user-invocable"] === undefined ? true : flag(fm["user-invocable"]) ?? false,
    arguments: words(fm.arguments),
  };
  return out;
}

const paths = fs.readFileSync(process.env.X08_PATHS, "utf8").split("\n").filter(Boolean);
const lines = [JSON.stringify({ meta: { bun: Bun.version, revision: Bun.revision, exe: process.execPath } })];
for (const p of paths) lines.push(JSON.stringify(one(p)));
fs.writeFileSync(process.env.X08_OUT, lines.join("\n") + "\n");
process.exit(0);
