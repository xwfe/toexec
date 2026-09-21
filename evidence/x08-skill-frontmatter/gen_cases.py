#!/usr/bin/env python3
"""差分模糊测试的输入：按 skill 作者实际会写的片段随机拼 frontmatter。

    python3 gen_cases.py --seed 1 --count 4000 --out /tmp/x08-cases
    python3 compare.py --claude … --files fuzz=/tmp/x08-cases/paths.txt --out runs/fuzz.json

和纯随机字节比，拼片段的好处是大部分输入都「差一点就是合法的 frontmatter」，
两个解析器会走到同一段逻辑里去，分歧才有意义。纯随机字节的模糊测试在 crate
自己的测试里（tests/fuzz.rs），那边只查不崩、不卡。

同一个 seed 永远生成同一批文件。
"""

import argparse
import json
import random
from pathlib import Path

KEYS = [
    "name", "description", "when_to_use", "when-to-use", "argument-hint", "arguments",
    "disable-model-invocation", "Disable-Model-Invocation", "disable_model_invocation",
    "user-invocable", "User_Invocable", "allowed-tools", "model", "metadata", "version", "license",
]

WORDS = ["deploy", "review", "the", "user", "asks", "PR", "C#", "http://x.io/a", "v1.2", "it's",
         "say \"hi\"", "a-b", "50%", "@me", "x|y", "a>b", "[draft]", "{k}", "*star", "&amp", "!bang",
         "`tick`", "é", "中文", "tab\there"]

SCALARS = ["true", "false", "True", "FALSE", "yes", "no", "on", "off", "Yes", "y", "n", "1", "0",
           "~", "null", "Null", "1.10", "010", "0x1F", "0o17", "1e3", "-2", "+1", ".5", "1_000",
           ".inf", ".nan", "2024-01-02", "12:30", "1.2.3", ""]


def words(r, lo=1, hi=6):
    return " ".join(r.choice(WORDS) for _ in range(r.randint(lo, hi)))


def dq(r, text):
    esc = r.choice(["", "\\n", "\\t", "\\\"", "\\\\", "\\x41", "\\u00e9", "\\/", "\\ ", "\\q", "\\x4"])
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + esc + '"'


def sq(text):
    return "'" + text.replace("'", "''") + "'"


def value(r, depth, indent):
    """一个值：返回 (同一行上的部分, 后续行)。"""
    pad = " " * (indent + 2)
    roll = r.random()
    if roll < 0.18:
        return r.choice(SCALARS), []
    if roll < 0.36:
        return words(r), []
    if roll < 0.46:
        return dq(r, words(r)), []
    if roll < 0.52:
        return sq(words(r)), []
    if roll < 0.60:
        header = r.choice(["|", ">", "|-", ">-", "|+", ">+", "|2", ">1", "| # c"])
        lines = []
        for _ in range(r.randint(1, 4)):
            lines.append(r.choice(["", pad + words(r), pad + "  " + words(r), pad + "# not a comment"]))
        return header, lines
    if roll < 0.68:
        return words(r), [pad + r.choice([words(r), "# comment", "key: " + words(r), "- item", ""]) for _ in range(r.randint(1, 3))]
    if roll < 0.74:
        items = [r.choice([words(r, 1, 2), dq(r, words(r, 1, 2)), r.choice(SCALARS)]) for _ in range(r.randint(0, 4))]
        if r.random() < 0.15:
            items.append("[nested]")
        return "[" + ", ".join(items) + "]", []
    if roll < 0.78:
        return "{" + ", ".join(f"{r.choice(KEYS)}: {r.choice(SCALARS + ['x'])}" for _ in range(r.randint(0, 3))) + "}", []
    if roll < 0.82:
        # 跨行的引号串 / 流式列表，格式化工具折出来的那种
        if r.random() < 0.5:
            return '"' + words(r), [pad + words(r), pad + words(r) + '"']
        return "[", [pad + words(r, 1, 1) + ",", pad + dq(r, "x") + ",", " " * indent + "]"]
    if depth < 3 and roll < 0.92:
        if r.random() < 0.5:
            return "", [pad + "- " + value(r, depth + 1, indent + 2)[0] for _ in range(r.randint(1, 3))]
        lines = []
        for _ in range(r.randint(1, 3)):
            head, tail = value(r, depth + 1, indent + 2)
            lines.append(f"{pad}{r.choice(KEYS)}: {head}".rstrip())
            lines.extend(tail)
        return "", lines
    return r.choice(["&anchor " + words(r, 1, 1), "*anchor", "!!str 1", "!tag x", "%x", "@x", "`x`"]), []


def document(r):
    lines = []
    for _ in range(r.randint(1, 6)):
        key = r.choice(KEYS)
        if r.random() < 0.05:
            key = '"' + key + '"'
        head, tail = value(r, 0, 0)
        lines.append(f"{key}: {head}".rstrip() if head else f"{key}:")
        lines.extend(tail)
        if r.random() < 0.1:
            lines.append(r.choice(["", "# comment", "  # indented comment"]))
    # 少量整体变形：tab、行尾空白、重复一行、多一个 `---`、CRLF
    if r.random() < 0.08:
        i = r.randrange(len(lines))
        lines[i] = "\t" + lines[i]
    if r.random() < 0.08:
        i = r.randrange(len(lines))
        lines[i] = lines[i] + "   "
    if r.random() < 0.08:
        i = r.randrange(len(lines))
        lines.insert(i, lines[i])
    if r.random() < 0.04:
        i = r.randrange(len(lines))
        lines[i] = lines[i] + " --- x"
    text = "\n".join(lines) + "\n"
    if r.random() < 0.05:
        text = text.replace("\n", "\r\n")
    return text


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--count", type=int, default=4000)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    r = random.Random(args.seed)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    paths = []
    for i in range(args.count):
        d = out / f"case-{args.seed}-{i:05d}"
        d.mkdir(exist_ok=True)
        f = d / "SKILL.md"
        nl = "\r\n" if "\r\n" in (front := document(r)) else "\n"
        f.write_text(f"---{nl}{front}---{nl}# Body{nl}text{nl}", newline="")
        paths.append(str(f))
    (out / "paths.txt").write_text("\n".join(paths) + "\n")
    # compare.py 把这个记进结果，别人照着就能生成同一批。
    (out / "meta.json").write_text(json.dumps({"generator": "gen_cases.py", "seed": args.seed, "count": args.count}) + "\n")


if __name__ == "__main__":
    main()
