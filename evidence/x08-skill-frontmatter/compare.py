#!/usr/bin/env python3
"""把同一批 SKILL.md 分别交给 toexec-skill 和 Claude Code 自己的解析器，逐个比对。

用法（在 toexec 仓库根目录）：

    python3 evidence/x08-skill-frontmatter/compare.py \\
        --claude /opt/homebrew/Caskroom/claude-code@latest/2.1.278/claude \\
        --corpus anthropics_skills=/path/to/clone ... \\
        --private local=~/.claude \\
        --out evidence/x08-skill-frontmatter/runs/corpus.json

- `--corpus 名字=目录`：公开语料。目录要是一个 git clone，结果里记下远端地址和
  提交号，别人按这两样重新 clone 就能复验；分歧的例子带相对路径。
- `--private 名字=目录`：本机语料，只记计数，不记路径和内容。
- `--files 名字=清单`：直接给一个路径清单（模糊测试生成的输入用这个）。

只看三类文件：`SKILL.md`、`commands/` 和 `agents/` 下的 `.md`。字段比对只对
skill 和命令做——agent 文件的字段语义不一样，但 frontmatter 是同一个解析器。

需要：Rust 工具链（跑 `--example frontmatter_json`）、Claude Code 二进制（只借它的
运行时，见 host_oracle.js 开头）。零额度。
"""

import argparse
import json
import math
import os
import re
import subprocess
import sys
import tempfile
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
EXAMPLES_PER_CATEGORY = 6
FIELDS = [
    "name",
    "description",
    "when_to_use",
    "argument_hint",
    "disable_model_invocation",
    "user_invocable",
    "arguments",
]


def kind_of(path: Path) -> str | None:
    if path.name == "SKILL.md":
        return "skill"
    if path.suffix != ".md":
        return None
    parts = path.parts
    if "commands" in parts:
        return "command"
    if "agents" in parts:
        return "agent"
    return None


def collect(root: Path) -> list[tuple[Path, str]]:
    found = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in (".git", "node_modules")]
        for name in filenames:
            p = Path(dirpath) / name
            k = kind_of(p.relative_to(root))
            if k and p.is_file() and not p.is_symlink():
                found.append((p, k))
    found.sort()
    return found


def run_ours(paths: list[str]) -> dict:
    exe = build_example()
    out = subprocess.run(
        [exe], input="\n".join(paths) + "\n", capture_output=True, text=True, check=True
    ).stdout
    rows = [json.loads(line) for line in out.splitlines() if line]
    return {r["path"]: r for r in rows}


_EXAMPLE = None


def build_example() -> str:
    global _EXAMPLE
    if _EXAMPLE is None:
        subprocess.run(
            ["cargo", "build", "-q", "--release", "-p", "toexec-skill", "--example", "frontmatter_json"],
            cwd=REPO,
            check=True,
        )
        _EXAMPLE = str(REPO / "target/release/examples/frontmatter_json")
    return _EXAMPLE


def run_host(claude: str, paths: list[str]) -> tuple[dict, dict]:
    with tempfile.TemporaryDirectory() as tmp:
        listing = Path(tmp) / "paths.txt"
        listing.write_text("\n".join(paths) + "\n")
        out = Path(tmp) / "host.jsonl"
        env = {
            **os.environ,
            "X08_PATHS": str(listing),
            "X08_OUT": str(out),
            "BUN_OPTIONS": f"--preload={HERE / 'host_oracle.js'}",
        }
        subprocess.run(
            [claude, "--version"], env=env, stdin=subprocess.DEVNULL,
            capture_output=True, check=True, timeout=600,
        )
        if not out.exists():
            sys.exit("host_oracle.js 没有写出结果：preload 没生效，Claude Code 可能换了运行时")
        rows = [json.loads(line) for line in out.read_text().splitlines() if line]
    meta = rows[0]["meta"]
    return meta, {r["path"]: r for r in rows[1:]}


# --- 值的比较 -----------------------------------------------------------------


def ours_value(v):
    """把 frontmatter_json 的编码换成宿主看到的形状：映射后写的赢，数字比数值。"""
    if isinstance(v, dict):
        if set(v) == {"map"}:
            out = {}
            for k, x in v["map"]:
                out[k] = ours_value(x)
            return out
        if set(v) == {"num"}:
            return ("num", number(v["num"]))
    if isinstance(v, list):
        return [ours_value(x) for x in v]
    return v


def host_value(v):
    if isinstance(v, bool) or v is None or isinstance(v, str):
        return v
    if isinstance(v, (int, float)):
        return ("num", float(v))
    if isinstance(v, dict):
        if set(v) == {"num"}:
            return ("num", number(v["num"]))
        return {k: host_value(x) for k, x in v.items()}
    if isinstance(v, list):
        return [host_value(x) for x in v]
    return v


def number(text: str) -> float:
    t = text.strip().lower().replace("_", "")
    if t.lstrip("+-") in (".inf", "infinity"):
        return float("-inf") if t.startswith("-") else float("inf")
    if t in (".nan", "nan"):
        return float("nan")
    try:
        if t.lstrip("+-").startswith("0x"):
            return float(int(t, 16))
        if t.lstrip("+-").startswith("0o"):
            return float(int(t.replace("0o", ""), 8))
        return float(t)
    except ValueError:
        return float("nan")


def short(v) -> str:
    s = json.dumps(v, ensure_ascii=False, default=str)
    return s if len(s) <= 80 else s[:77] + "..."


def diff(a, b, at="$"):
    """第一处不同的位置和两边的值；一样就是 None。"""
    if isinstance(a, tuple) and isinstance(b, tuple):
        same = a[1] == b[1] or (math.isnan(a[1]) and math.isnan(b[1]))
        return None if same else f"{at}: ours {a[1]} vs host {b[1]}"
    if type(a) is not type(b):
        return f"{at}: ours {short(a)} vs host {short(b)}"
    if isinstance(a, dict):
        if set(a) != set(b):
            return f"{at}: keys ours {sorted(a)} vs host {sorted(b)}"
        for k in a:
            d = diff(a[k], b[k], f"{at}.{k}")
            if d:
                return d
        return None
    if isinstance(a, list):
        if len(a) != len(b):
            return f"{at}: ours {len(a)} items vs host {len(b)}"
        for i, (x, y) in enumerate(zip(a, b)):
            d = diff(x, y, f"{at}[{i}]")
            if d:
                return d
        return None
    return None if a == b else f"{at}: ours {short(a)} vs host {short(b)}"


def norm_front(text):
    if text is None:
        return None
    lines = text.replace("\r\n", "\n").split("\n")
    while lines and not lines[0].strip():
        lines.pop(0)
    return "\n".join(lines).rstrip()


# --- 分类 ---------------------------------------------------------------------


def ours_fields(row) -> dict:
    """两个开关例程已经按宿主的规则算成了布尔值，见 Frontmatter::flag 的说明。"""
    return row.get("fields")  # None = frontmatter 读不了，产品拿不到任何字段


def host_fields(row) -> dict:
    f = dict(row["fields"])
    f["disable_model_invocation"] = f["disable_model_invocation"] is True
    return f


def classify(ours, host, kind):
    """这个文件落进哪几个分类，以及每个分类的一句说明。"""
    cats = []
    of, hf = norm_front(ours.get("front")), norm_front(host.get("front"))
    if of is None and hf is None:
        cats.append(("split", "no-frontmatter", ""))
    elif of is None or hf is None or of != hf:
        detail = f"ours {'none' if of is None else len(of)} chars vs host {'none' if hf is None else len(hf)}"
        cats.append(("split", "differs", detail))
    else:
        cats.append(("split", "same", ""))
    # 拆出来的原文都不一样，后面的值和字段自然不一样，不再重复计数。
    if cats[-1][1] == "differs":
        return cats

    ours_ok = "ok" in ours["parse"]
    stage = host.get("stage")  # None = 宿主没找到 frontmatter
    reading = ours.get("reading")
    # 两边对这份 frontmatter 的读法是否一致。前三种是一致或有意的不同（宽松读法
    # 是这里的扩展，见 toexec-skill 的 Reading::Lenient），其余都要查。
    expected = {("Yaml", "direct"): "both-yaml", ("Requoted", "requoted"): "both-requoted",
                ("Lenient", "failed"): "lenient-host-ignores"}
    if ours.get("front") is None and host.get("front") is None:
        pass
    elif stage is None:
        cats.append(("parse", "host-no-frontmatter", ""))
    elif ours_ok and (reading, stage) in expected:
        cats.append(("parse", expected[(reading, stage)], ""))
    elif ours_ok:
        detail = host.get("first_error", "") if stage != "failed" else host.get("error", "")
        cats.append(("parse", f"ours-{reading.lower()}-host-{stage}", detail))
    elif stage in ("direct", "requoted"):
        e = ours["parse"]["err"]
        cats.append(("parse", "ours-rejects-host-reads", f"{e['kind']} at line {e['line']} (host: {stage})"))
    else:
        e = ours["parse"]["err"]
        cats.append(("parse", "both-reject", f"ours {e['kind']} at line {e['line']}; host {host.get('error', '')}"))

    if ours_ok and "value" in host and ours.get("front") is not None and host.get("front") is not None:
        d = diff(ours_value(ours["parse"]["ok"]), host_value(host["value"]))
        if d is None and not isinstance(host["value"], dict):
            d = f"$: host result is not a mapping ({short(host['value'])})"
        if d:
            cats.append(("tree", "differs", d))

    # 字段只在两边都读进了 frontmatter 时比：一边读不了已经记在 parse 那一组，
    # 再按字段数一遍只是把同一处分歧重复计数。
    if kind in ("skill", "command") and ours_ok and stage != "failed":
        of_ = ours_fields(ours)
        hf_ = host_fields(host)
        if of_ is not None:
            for name in FIELDS:
                a, b = of_[name], hf_[name]
                if name in ("name", "when_to_use", "argument_hint") and isinstance(b, str):
                    b = b.strip() or None
                if a != b:
                    cats.append(("field", name, f"ours {short(a)} vs host {short(b)}"))
    return cats


# --- 已知原因 -----------------------------------------------------------------
#
# 每一条都是查过、有意不跟或跟不了的不同，理由写在 README 的「剩下的分歧」
# 一节。归不进来的记为 unexplained——那才是要查的。

CAUSES = {
    "host-ignores-we-read-leniently": "宿主两步都读不了、整段丢弃；toexec-skill 宽松读出（Reading::Lenient）",
    "both-unreadable": "两边都读不了：宿主整段丢弃，toexec-skill 报错并给行号",
    "host-cuts-at-dashes": "值里有 `---`，宿主的正则在那里截断 frontmatter；toexec-skill 要求 `---` 独占一行",
    "anchor-tag-alias": "锚点 &x、别名 *x、标签 !x：宿主（Bun.YAML）认，toexec-skill 不认",
    "normalized-key": "键名大小写或 -/_ 不同、或同一个键写了几遍：toexec-skill 归一化后认，宿主只认原样",
    "number-spelling": "数字保留原文（1.10、0x1F）；宿主 String(数字) 会改写成 1.1、31",
    "host-stringifies-map": "映射或布尔当文字用：宿主 String() 得 [object Object] / \"false\"，toexec-skill 给不出文字",
    "bun-tab-quirk": "行首有 tab：Bun 是否报错取决于前文（前面是引号串或块标量时报），toexec-skill 按 YAML 规则",
    "bun-crlf-quoted": "CRLF 文件里跨行的引号串：Bun 把换行留成 \\n，YAML 规定折成空格",
}

ANCHOR = re.compile(r"(:[ \t]+|^[ \t]*-[ \t]+|[\[,{][ \t]*)[&!*][^\s,\]}]", re.M)
FIELD_KEYS = {"name": "name", "description": "description", "when_to_use": "when_to_use",
              "argument_hint": "argument-hint", "disable_model_invocation": "disable-model-invocation",
              "user_invocable": "user-invocable", "arguments": "arguments"}


def top_keys(front):
    keys = []
    for line in front.replace("\r\n", "\n").split("\n"):
        m = re.match(r"^[ \t]*(\"[^\"]*\"|'[^']*'|[^\s#:][^:]*?):(\s|$)", line)
        if m:
            keys.append(m.group(1).strip("\"'"))
    return keys


def explain(group, cat, detail, front):
    """这处分歧属于哪个已知原因；None = 说不清。"""
    front = front or ""
    norm = lambda k: k.replace("_", "-").lower()
    if group == "split":
        return "host-cuts-at-dashes" if "---" in front else None
    if group == "parse" and cat == "lenient-host-ignores":
        return "host-ignores-we-read-leniently"
    if group == "parse" and cat == "both-reject":
        return "both-unreadable"
    if ANCHOR.search(front):
        return "anchor-tag-alias"
    if re.search(r"^[ ]*\t", front, re.M) and (group in ("parse", "tree") or "Tab" in detail):
        return "bun-tab-quirk"
    if "\r\n" in front and group in ("tree", "field") and "\\n" in detail:
        return "bun-crlf-quoted"
    if group == "field":
        if "[object Object]" in detail or re.match(r'ours null vs host "(true|false)"$', detail):
            return "host-stringifies-map"
        m = re.match(r'ours "(.*)" vs host "(.*)"$', detail)
        if m:
            a, b = number(m.group(1)), number(m.group(2))
            if a == b or (a != a and b != b):
                return "number-spelling"
        want = FIELD_KEYS[cat]
        variants = [k for k in top_keys(front) if norm(k) == norm(want)]
        if variants and (want not in variants or len(variants) > 1):
            return "normalized-key"
        if re.search(r"^[ ]*\t", front, re.M):
            return "bun-tab-quirk"
    return None


def summarise(name, source, files, ours, host, private, detail_log=None):
    counts = {"split": Counter(), "parse": Counter(), "tree": Counter(), "field": Counter()}
    explained = Counter()
    unexplained = Counter()
    examples = []
    per_cat = Counter()
    kinds = Counter(k for _, k in files)
    for path, kind in files:
        o, h = ours.get(str(path)), host.get(str(path))
        if o is None or h is None or "skip" in o or "skip" in h:
            counts["split"]["skipped"] += 1
            continue
        front = o.get("front") if o.get("front") is not None else h.get("front")
        for group, cat, detail in classify(o, h, kind):
            counts[group][cat] += 1
            agree = group in ("split", "parse") and cat in ("same", "no-frontmatter", "both-yaml", "both-requoted")
            if agree:
                continue
            cause = explain(group, cat, detail, front)
            if cause:
                explained[cause] += 1
            else:
                unexplained[f"{group}:{cat}"] += 1
            key = (group, cat, cause)
            # 说不清的都留例子；说得清的每个原因留两个，够看懂就行。
            cap = EXAMPLES_PER_CATEGORY if cause is None else 2
            if not private and per_cat[key] < cap:
                per_cat[key] += 1
                rel = str(path.relative_to(source["root"])) if "root" in source else path.parent.name
                example = {"path": rel, "kind": kind, "group": group, "category": cat,
                           "cause": cause, "detail": detail}
                if "generated" in source:  # 合成的输入，原文直接放进来
                    example["front"] = front
                examples.append(example)
            if detail_log is not None and not private:
                detail_log.append({"path": str(path), "group": group, "category": cat, "cause": cause,
                                   "detail": detail, "front": front})
    src = {k: v for k, v in source.items() if k != "root"}
    return {
        "name": name,
        "source": src,
        "files": len(files),
        "kinds": dict(sorted(kinds.items())),
        **{g: dict(sorted(c.items())) for g, c in counts.items()},
        "explained": dict(explained.most_common()),
        "unexplained": dict(unexplained.most_common()),
        **({} if private else {"examples": examples}),
    }


def git_source(root: Path) -> dict:
    def git(*args):
        r = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True)
        return r.stdout.strip() or None

    return {"root": root, "repo": git("remote", "get-url", "origin"), "commit": git("rev-parse", "HEAD")}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--claude", required=True)
    ap.add_argument("--corpus", action="append", default=[])
    ap.add_argument("--private", action="append", default=[])
    ap.add_argument("--files", action="append", default=[])
    ap.add_argument("--out", required=True)
    ap.add_argument("--detail", help="把每一处分歧连同 frontmatter 原文写成 JSONL（排查用，不提交）")
    args = ap.parse_args()

    groups = []
    for spec in args.corpus:
        name, root = spec.split("=", 1)
        root = Path(root).expanduser().resolve()
        groups.append((name, git_source(root), collect(root), False))
    for spec in args.private:
        name, root = spec.split("=", 1)
        root = Path(root).expanduser().resolve()
        groups.append((name, {"private": True}, collect(root), True))
    for spec in args.files:
        name, listing = spec.split("=", 1)
        paths = [Path(p) for p in Path(listing).read_text().splitlines() if p]
        meta = Path(listing).parent / "meta.json"
        source = {"generated": json.loads(meta.read_text()) if meta.exists() else "unknown"}
        groups.append((name, source, [(p, "skill") for p in paths], False))

    log = [] if args.detail else None
    every = sorted({str(p) for _, _, files, _ in groups for p, _ in files})
    ours = run_ours(every)
    meta, host = run_host(args.claude, every)
    head = subprocess.run(["git", "-C", str(REPO), "rev-parse", "--short", "HEAD"], capture_output=True, text=True).stdout.strip()
    dirty = subprocess.run(["git", "-C", str(REPO), "status", "--porcelain", "crates/toexec-skill"], capture_output=True, text=True).stdout.strip()
    result = {
        "host": {**meta, "claude": os.path.realpath(args.claude)},
        "ours": {"toexec": head + ("+dirty" if dirty else "")},
        "causes": CAUSES,
        "corpora": [summarise(n, s, f, ours, host, p, log) for n, s, f, p in groups],
    }
    if args.detail:
        Path(args.detail).write_text("".join(json.dumps(x, ensure_ascii=False) + "\n" for x in log))
    Path(args.out).write_text(json.dumps(result, ensure_ascii=False, indent=1, default=str) + "\n")
    for c in result["corpora"]:
        print(f"{c['name']}: {c['files']} files, parse {c['parse']}")
        print(f"  explained {c['explained']}")
        print(f"  unexplained {c['unexplained']}")


if __name__ == "__main__":
    main()
