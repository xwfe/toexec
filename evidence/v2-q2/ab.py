#!/usr/bin/env python3
"""V2-Q2 alwaysLoad 模型对照：跑一格、判分、出报告。

一格 = 一个任务 × 一组 × 一次。两组只差一件事：外部 MCP 服务器配置里加不加
`"alwaysLoad": true`。ccnm 的代码、工具、提示词都不动。

用法（在 Agent 机器上跑，那台的 Claude CLI 得是登录状态）：

    ./ab.py smoke  <ccnm 二进制> <输出目录>
    ./ab.py run    <ccnm 二进制> <输出目录> [--tasks t1,t2,t3] [--arms a,b] [--reps 3]
    ./ab.py report <输出目录>

输出目录不要放在仓库里。每一格自己一份夹具副本、自己的 HOME 和 state 目录，
互不干扰。

两个坑写在这里，别去代码里找：

1. MCP 子进程必须用 `/usr/bin/env -i` 起。Claude Code 会往子进程注入
   CLAUDE_CODE_MESSAGING_TOKEN，ccnm 见到认证形状的环境变量就按策略拒绝
   （CCNM_E_POLICY），表现是握手直接失败。
2. `claude` 的工作目录是一个空目录，且从头到尾不告诉模型夹具在哪。ccnm 不
   吐绝对路径，所以模型只能靠 ccnm 的工具干活——否则它会用自己的 Read/Bash
   直接读文件，对照就白做了。
"""

import argparse
import base64
import json
import os
import re
import shutil
import statistics
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURES = os.path.join(HERE, "fixture")
GRADERS = os.path.join(HERE, "grade")

# MCP 子进程的 PATH：ccnm 的 search_text 要 rg，任务里要 python3。
# exec_command 让子命令继承这个环境（只剥 ANTHROPIC_*/CLAUDE_* 前缀），
# 所以给不够 PATH 的表现是「python3 is not installed on the Runtime Node」。
CHILD_PATH = "/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"

MODEL = "sonnet"
MAX_TURNS = 40
# 一格的墙钟上限。超了算这一格失败，不重跑。
CELL_TIMEOUT_S = 900

T1_ANSWER = "436254879"

PROMPTS = {
    "t1": (
        "远端工作区里有一处记录了 calibration_offset 的值。"
        "找到它，最后一行只回答那个整数，不要加单位、逗号或别的字。"
    ),
    "t2": (
        "远端工作区是一个 Python 项目，跑 `python3 -m unittest discover -s . -q` "
        "会有用例失败。找出原因并修好，只允许改 stats.py，不许改测试。"
        "改完再跑一次测试确认全过。"
    ),
    "t3": (
        "在远端工作区的 text.py 里加一个函数 top_words(text, n)：把 text 按空白切成词"
        "（区分大小写），返回出现次数最多的前 n 个，形如 [(词, 次数), ...]，"
        "按次数从多到少排；次数相同的按词本身的字母序排。n 比不同词的个数大时，有几个给几个。"
        "再在 test_text.py 里补上对应测试，最后跑 `python3 -m unittest discover -s . -q` 确认全过。"
    ),
}

SMOKE_PROMPT = (
    "调一次 workspace_info，然后一行回答这个工作区叫什么名字，不要别的字。"
)


def wire_payload(workspace, session):
    """外部入口的 wire：protocol 5、coding 模式。"""
    blob = json.dumps(
        {
            "protocol": 5,
            "workspace": workspace,
            "session": session,
            "mode": "coding",
        }
    ).encode()
    return base64.urlsafe_b64encode(blob).decode().rstrip("=")


def write_config(cell, root):
    path = os.path.join(cell, "config.toml")
    with open(path, "w") as f:
        f.write(
            "this = \"runtime\"\n"
            "[nodes.runtime]\n"
            "[nodes.agent]\n"
            "ssh = \"agent-node.invalid\"\n"
            "[workspaces.demo]\n"
            "root = \"%s\"\n"
            "agent = { node = \"agent\", instance = \"claude-main\" }\n"
            "external_mcp = \"coding\"\n"
            # 夹具是临时目录里的一次性项目，不是用户的工作区。这两个开关让
            # 非交互的 -p 会话能真的跑起测试来。
            "allow_unconfined_exec = true\n"
            "allow_unattended_exec = true\n" % root
        )
    return path


def write_mcp(cell, ccnm, config, session, always_load):
    server = {
        "type": "stdio",
        "command": "/usr/bin/env",
        "args": [
            "-i",
            "PATH=" + CHILD_PATH,
            "HOME=" + os.path.join(cell, "home"),
            "XDG_STATE_HOME=" + os.path.join(cell, "state"),
            "CCNM_CONFIG=" + config,
            ccnm,
            "internal",
            "mcp-serve",
            "--payload",
            wire_payload("demo", session),
        ],
    }
    # B 组唯一的差别就是这一行。
    if always_load:
        server["alwaysLoad"] = True
    path = os.path.join(cell, "mcp.json")
    with open(path, "w") as f:
        json.dump({"mcpServers": {"ccnm": server}}, f, indent=1)
    return path


def run_claude(cell, prompt, mcp):
    out = os.path.join(cell, "stream.jsonl")
    err = os.path.join(cell, "stderr.txt")
    cmd = [
        "/usr/bin/env",
        "-i",
        "HOME=" + os.environ["HOME"],
        "PATH=" + os.environ.get("PATH", CHILD_PATH),
        "USER=" + os.environ.get("USER", ""),
        "LANG=en_US.UTF-8",
        os.path.expanduser("~/.local/bin/claude"),
        "-p",
        prompt,
        "--model",
        MODEL,
        "--max-turns",
        str(MAX_TURNS),
        "--output-format",
        "stream-json",
        "--verbose",
        "--no-session-persistence",
        "--permission-mode",
        "bypassPermissions",
        "--strict-mcp-config",
        "--mcp-config",
        mcp,
    ]
    started = time.time()
    with open(out, "w") as o, open(err, "w") as e:
        try:
            code = subprocess.call(
                cmd, cwd=os.path.join(cell, "run"), stdout=o, stderr=e,
                timeout=CELL_TIMEOUT_S,
            )
        except subprocess.TimeoutExpired:
            code = None
    return code, time.time() - started, out


def parse_stream(path):
    """从 stream-json 里数回合和工具调用，取最后那条 result 的账单。"""
    m = {
        "turns": None,
        "tool_search_calls": 0,
        "ccnm_tool_calls": 0,
        "builtin_tool_calls": 0,
        "tools": {},
        "input_tokens": None,
        "output_tokens": None,
        "cache_creation_input_tokens": None,
        "cache_read_input_tokens": None,
        "total_cost_usd": None,
        "duration_ms": None,
        "result_subtype": None,
        "result_text": "",
        "is_error": None,
    }
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            if ev.get("type") == "assistant":
                for block in ev.get("message", {}).get("content", []) or []:
                    if block.get("type") != "tool_use":
                        continue
                    name = block.get("name", "?")
                    m["tools"][name] = m["tools"].get(name, 0) + 1
                    if "toolsearch" in name.lower():
                        m["tool_search_calls"] += 1
                    elif name.startswith("mcp__ccnm__"):
                        m["ccnm_tool_calls"] += 1
                    else:
                        m["builtin_tool_calls"] += 1
            elif ev.get("type") == "result":
                u = ev.get("usage", {}) or {}
                m["turns"] = ev.get("num_turns")
                m["duration_ms"] = ev.get("duration_ms")
                m["total_cost_usd"] = ev.get("total_cost_usd")
                m["result_subtype"] = ev.get("subtype")
                m["is_error"] = ev.get("is_error")
                m["result_text"] = ev.get("result") or ""
                for k in (
                    "input_tokens",
                    "output_tokens",
                    "cache_creation_input_tokens",
                    "cache_read_input_tokens",
                ):
                    m[k] = u.get(k)
    total = 0
    for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"):
        if m[k]:
            total += m[k]
    m["total_input_tokens"] = total
    return m


def changed_files(pristine, proj):
    """夹具副本相对原始夹具改了哪些文件。判分只看副作用，这是主要依据。

    排除 __pycache__：跑一次 `python3 -m unittest` 就会生成它，任务本身也要求
    跑测试，所以它是产物不是改动。第一轮就是没排它，18 格里 12 格被误判成
    「改了不该改的文件」。
    """
    p = subprocess.Popen(
        ["diff", "-rq", "-x", "__pycache__", "-x", "*.pyc",
         pristine, proj], stdout=subprocess.PIPE, stderr=subprocess.STDOUT
    )
    text = p.communicate()[0].decode("utf-8", "replace")
    names = set()
    for line in text.splitlines():
        mm = re.match(r"Files .*? and (.*?) differ", line)
        if mm:
            names.add(os.path.relpath(mm.group(1), proj))
            continue
        mm = re.match(r"Only in (.*?): (.*)$", line)
        if mm:
            where = os.path.join(mm.group(1), mm.group(2))
            if where.startswith(proj):
                names.add(os.path.relpath(where, proj))
            else:
                names.add("-" + os.path.relpath(where, pristine))
    return sorted(names)


def run_tests(proj):
    p = subprocess.Popen(
        ["/usr/bin/python3", "-m", "unittest", "discover", "-s", ".", "-q"],
        cwd=proj,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    text = p.communicate()[0].decode("utf-8", "replace")
    return p.returncode, text


def grade(task, cell, metrics):
    proj = os.path.join(cell, "proj")
    pristine = os.path.join(cell, "pristine")
    changed = changed_files(pristine, proj)
    g = {"changed": changed, "ok": False, "why": ""}

    if task == "t1":
        # 只读任务：夹具不该被改，答案要跟夹具里的数一致。
        answer = re.findall(r"\d[\d,]*", metrics["result_text"])
        answer = [a.replace(",", "") for a in answer]
        g["answer"] = answer[-1] if answer else None
        if changed:
            g["why"] = "只读任务却改了文件：%s" % ", ".join(changed)
        elif g["answer"] != T1_ANSWER:
            g["why"] = "答案 %r，夹具里是 %s" % (g["answer"], T1_ANSWER)
        else:
            g["ok"] = True
    elif task == "t2":
        code, text = run_tests(proj)
        g["tests_exit"] = code
        g["tests_tail"] = text.strip().splitlines()[-3:]
        if changed != ["stats.py"]:
            g["why"] = "只允许改 stats.py，实际改了：%s" % (changed or "什么都没改")
        elif code != 0:
            g["why"] = "事后重跑测试没过"
        else:
            g["ok"] = True
    elif task == "t3":
        code, text = run_tests(proj)
        g["tests_exit"] = code
        g["tests_tail"] = text.strip().splitlines()[-3:]
        shutil.copy(os.path.join(GRADERS, "t3_grade.py"), proj)
        p = subprocess.Popen(
            ["/usr/bin/python3", "t3_grade.py"],
            cwd=proj,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        gtext = p.communicate()[0].decode("utf-8", "replace")
        os.remove(os.path.join(proj, "t3_grade.py"))
        g["grader_exit"] = p.returncode
        g["grader_tail"] = gtext.strip().splitlines()[-4:]
        allowed = {"text.py", "test_text.py"}
        extra = [c for c in changed if c not in allowed]
        if extra:
            g["why"] = "改了不该改的：%s" % ", ".join(extra)
        elif "text.py" not in changed or "test_text.py" not in changed:
            g["why"] = "两个文件都得动，实际动了：%s" % (changed or "无")
        elif code != 0:
            g["why"] = "模型自己的测试没过"
        elif p.returncode != 0:
            g["why"] = "独立判分不过"
        else:
            g["ok"] = True
    return g


def one_cell(ccnm, outdir, task, arm, rep, prompt=None, fixture=None):
    name = "%s-%s-%d" % (task, arm, rep)
    cell = os.path.join(outdir, "runs", name)
    if os.path.exists(cell):
        shutil.rmtree(cell)
    for sub in ("proj", "pristine", "home", "state", "run"):
        os.makedirs(os.path.join(cell, sub))
    src = os.path.join(FIXTURES, fixture or task)
    for dst in ("proj", "pristine"):
        shutil.rmtree(os.path.join(cell, dst))
        shutil.copytree(src, os.path.join(cell, dst))

    config = write_config(cell, os.path.join(cell, "proj"))
    session = "q2-%s" % name
    mcp = write_mcp(cell, ccnm, config, session, always_load=(arm == "b"))

    code, wall, stream = run_claude(cell, prompt or PROMPTS[task], mcp)
    metrics = parse_stream(stream)
    metrics["exit_code"] = code
    metrics["wall_s"] = round(wall, 1)
    g = grade(task, cell, metrics) if task in PROMPTS else {"ok": code == 0, "changed": []}

    cellinfo = {"task": task, "arm": arm, "rep": rep, "metrics": metrics, "grade": g}
    with open(os.path.join(cell, "cell.json"), "w") as f:
        json.dump(cellinfo, f, ensure_ascii=False, indent=1)
    return cellinfo


def line(c):
    m, g = c["metrics"], c["grade"]
    return "%-9s %s  turns=%-3s ts=%-2s ccnm=%-3s builtin=%-3s in=%-7s out=%-6s %5ss  %s%s" % (
        "%s-%s-%s" % (c["task"], c["arm"], c["rep"]),
        "通过" if g.get("ok") else "失败",
        m.get("turns"),
        m.get("tool_search_calls"),
        m.get("ccnm_tool_calls"),
        m.get("builtin_tool_calls"),
        m.get("total_input_tokens"),
        m.get("output_tokens"),
        m.get("wall_s"),
        m.get("result_subtype") or "",
        "" if g.get("ok") else "  <- " + (g.get("why") or ""),
    )


def cmd_smoke(args):
    for arm in ("a", "b"):
        c = one_cell(args.ccnm, args.outdir, "smoke", arm, 0,
                     prompt=SMOKE_PROMPT, fixture="t1")
        c["grade"]["ok"] = "demo" in c["metrics"]["result_text"]
        print(line(c))
        print("   result: %r" % c["metrics"]["result_text"][:200])
        if not c["grade"]["ok"]:
            print("   冒烟没过，按实验单停下，不追加次数。")
            return 1
    return 0


def cmd_run(args):
    tasks = args.tasks.split(",")
    arms = args.arms.split(",")
    done = []
    for rep in range(1, args.reps + 1):
        for task in tasks:
            for arm in arms:
                c = one_cell(args.ccnm, args.outdir, task, arm, rep)
                done.append(c)
                print(line(c))
                sys.stdout.flush()
    return 0


def cmd_regrade(args):
    """只重判分，不再发模型请求。判分脚本改了就用它，夹具副本还在原地。"""
    runs = os.path.join(args.outdir, "runs")
    for name in sorted(os.listdir(runs)):
        p = os.path.join(runs, name, "cell.json")
        if not os.path.exists(p):
            continue
        with open(p) as f:
            c = json.load(f)
        if c["task"] in PROMPTS:
            c["grade"] = grade(c["task"], os.path.join(runs, name), c["metrics"])
        with open(p, "w") as f:
            json.dump(c, f, ensure_ascii=False, indent=1)
        print(line(c))
    return 0


def median(xs):
    xs = [x for x in xs if x is not None]
    return statistics.median(xs) if xs else None


def cmd_report(args):
    runs = os.path.join(args.outdir, "runs")
    cells = []
    for name in sorted(os.listdir(runs)):
        p = os.path.join(runs, name, "cell.json")
        if os.path.exists(p):
            with open(p) as f:
                cells.append(json.load(f))
    for c in cells:
        print(line(c))
    print()
    print("%-5s %-3s %-4s %-6s %-6s %-9s %-9s %s" % (
        "任务", "组", "成功", "TS调用", "ccnm调用", "墙钟中位", "输入中位", "输出中位"))
    summary = {}
    for task in sorted(set(c["task"] for c in cells if c["task"] != "smoke")):
        for arm in ("a", "b"):
            sel = [c for c in cells if c["task"] == task and c["arm"] == arm]
            if not sel:
                continue
            s = {
                "n": len(sel),
                "ok": sum(1 for c in sel if c["grade"].get("ok")),
                "ts": sum(c["metrics"].get("tool_search_calls") or 0 for c in sel),
                "ccnm": sum(c["metrics"].get("ccnm_tool_calls") or 0 for c in sel),
                "wall": median([c["metrics"].get("wall_s") for c in sel]),
                "tin": median([c["metrics"].get("total_input_tokens") for c in sel]),
                "tout": median([c["metrics"].get("output_tokens") for c in sel]),
            }
            summary[(task, arm)] = s
            print("%-5s %-3s %d/%-2d %-6d %-8d %-9s %-9s %s" % (
                task, arm, s["ok"], s["n"], s["ts"], s["ccnm"], s["wall"], s["tin"], s["tout"]))

    print()
    print("采纳 B 的判据（实验单冻结）：")
    verdict = True
    for task in sorted(set(t for t, _ in summary)):
        a, b = summary.get((task, "a")), summary.get((task, "b"))
        if not a or not b:
            continue
        checks = []
        checks.append(("B 成功次数不少于 A", b["ok"] >= a["ok"], "%d vs %d" % (b["ok"], a["ok"])))
        checks.append(("B 的 ToolSearch 调用为 0", b["ts"] == 0, str(b["ts"])))
        if a["wall"] and b["wall"]:
            checks.append(("B 墙钟不比 A 差 20% 以上", b["wall"] <= a["wall"] * 1.2,
                           "%.1fs vs %.1fs" % (b["wall"], a["wall"])))
        if a["tin"] and b["tin"]:
            checks.append(("B 总输入不比 A 多 10% 以上", b["tin"] <= a["tin"] * 1.1,
                           "%d vs %d" % (b["tin"], a["tin"])))
        for label, ok, detail in checks:
            print("  %-4s %-26s %s  (%s)" % (task, label, "是" if ok else "否", detail))
            verdict = verdict and ok
    print()
    print("结论：%s" % ("采纳 B（服务器配置加 alwaysLoad）" if verdict else "不采纳 B"))
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd")
    for name in ("smoke", "run"):
        p = sub.add_parser(name)
        p.add_argument("ccnm")
        p.add_argument("outdir")
        if name == "run":
            p.add_argument("--tasks", default="t1,t2,t3")
            p.add_argument("--arms", default="a,b")
            p.add_argument("--reps", type=int, default=3)
    for name in ("report", "regrade"):
        p = sub.add_parser(name)
        p.add_argument("outdir")
    args = ap.parse_args()
    if not args.cmd:
        ap.print_help()
        return 2
    if args.cmd in ("smoke", "run"):
        args.ccnm = os.path.abspath(os.path.expanduser(args.ccnm))
    args.outdir = os.path.abspath(os.path.expanduser(args.outdir))
    return {
        "smoke": cmd_smoke, "run": cmd_run,
        "report": cmd_report, "regrade": cmd_regrade,
    }[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
