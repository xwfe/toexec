#!/usr/bin/env python3
"""把 runs/r1..r3 里同名实验的关键事实抽出来逐项比；不一致的字段打印出来。

比的是"会不会推翻结论"的事实，不比耗时、进程号、会话 id 和 .git 上溯查询的重复次数
（Codex 在启动阶段重复上溯 3–4 遍，次数随时序变化，路径集合不变）。
用法：compare.py [输出 json 路径]
"""

import json
import re
import sys
from pathlib import Path

RUNS = Path(__file__).resolve().parent / "runs"
ROUNDS = ["r1", "r2", "r3"]


def facts(run):
    summary = json.loads((run / "summary.json").read_text())
    rows = [json.loads(line) for line in open(run / "messages.jsonl")] if (run / "messages.jsonl").exists() else []
    responses = {r["msg"]["id"]: r["msg"] for r in rows
                 if r["dir"] in ("server->client", "bridge->client") and "id" in r["msg"]}
    requests = set()
    refused = set()
    for r in rows:
        msg = r["msg"]
        if r["dir"] != "client->server" or "method" not in msg:
            continue
        params = msg.get("params") or {}
        # 本机工作目录带着轮次目录名，换成占位才能跨轮比
        target = re.sub(r"/runs/(r\d/)?", "/runs/rN/", params.get("path") or params.get("cwd") or "")
        sb = params.get("sandbox")
        entries = sorted(json.dumps(e["path"], sort_keys=True) + ":" + e["access"]
                         for e in ((sb or {}).get("permissions", {}).get("file_system", {}) or {}).get("entries", []))
        resp = responses.get(msg.get("id"))
        outcome = "-" if resp is None else (f"error {resp['error']['code']}" if "error" in resp else "ok")
        key = f"{msg['method']} {target} sandbox={'null' if sb is None else 'present'} {outcome}"
        if msg["method"] in ("process/start", "fs/writeFile") and sb is not None:
            key += " entries=" + "|".join(entries)
        requests.add(key)
        if r.get("intercepted"):
            refused.add(key)
    files_after = sorted(set(re.findall(r"^[-d]\S+\s+\d+\s+\S+\s+\S+\s+\d+\s+\S+\s+\d+\s+\S+\s+(\S+)$",
                                        summary.get("container_after", ""), re.M)) - {".", ".."})
    # 进程输出里 stdout 和 stderr 交错的顺序每次不同（sh 的报错前缀、报错行尾都可能被 stdout 的
    # rc=N 插开），去掉前缀、把 rc=N 单独成行、按行排序后再比
    outputs = sorted("\n".join(sorted(l for l in re.sub(r"(rc=\d+)", r"\n\1\n", re.sub(r"\d{3,}", "N", v)).replace("/bin/sh: 1: ", "").splitlines() if l))
                     for v in (summary.get("process_output") or {}).values())
    # 早期几轮还没有标记检查；那几个实验两边都没放标记文件，等价于两者都不在
    markers = summary.get("markers_in_model_request") or {"LOCAL-AGENTS-MARKER": False, "REMOTE-AGENTS-MARKER": False}
    return {
        "codex_rc": summary.get("codex_rc"),
        "plan_done": summary.get("plan_done"),
        "approval_prompts": summary.get("approval_prompts"),
        "bridge_connections": summary.get("bridge_connections"),
        "methods": sorted((summary.get("methods") or {}).keys()),
        "requests": sorted(requests),
        "refused": sorted(refused),
        "files_after": files_after,
        "markers": markers,
        "process_output": outputs,
    }


def main():
    report = {}
    names = sorted(p.name for p in (RUNS / "r1").iterdir() if p.is_dir())
    for name in names:
        per_round = {}
        for r in ROUNDS:
            run = RUNS / r / name
            if (run / "summary.json").exists():
                per_round[r] = facts(run)
        fields = per_round.get("r1", {}).keys()
        diffs = {f: {r: per_round[r][f] for r in per_round} for f in fields
                 if len({json.dumps(per_round[r][f], sort_keys=True) for r in per_round}) > 1}
        report[name] = {"rounds": sorted(per_round), "consistent": not diffs, "differences": diffs,
                        "facts": per_round.get("r1")}
        print(f"{name}: rounds={sorted(per_round)} consistent={not diffs}")
        for f, v in diffs.items():
            print(f"  DIFF {f}: " + json.dumps(v, ensure_ascii=False)[:600])
    if len(sys.argv) > 1:
        Path(sys.argv[1]).write_text(json.dumps(report, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
