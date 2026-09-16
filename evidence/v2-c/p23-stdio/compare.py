#!/usr/bin/env python3
"""Compare the summaries of several rounds: same conclusions, or say where not.

usage: compare.py <out json> [runs dir, default runs]
"""
import json
import sys
from pathlib import Path

SCENARIOS = ["basic", "refused", "drop", "unreachable"]
# What must agree across rounds. Timings, pids, session ids and pane text do not.
KEYS = ["model_steps", "plan_done", "approval_prompts", "trust_prompts", "runtime_connections",
        "exec_serve_exit_codes", "agent_relay_spawns", "methods", "work_files", "outside_files",
        "local_cwd_files", "auth_symlink_intact", "profile_files", "replies_by_code"]


def norm(text):
    """The run directory differs per round and appears in argv; the ancestor
    `.git` walk repeats a varying number of times (P21 saw 3-4), so -32004 is
    compared as present/absent rather than counted."""
    import re
    return re.sub(r"runs/r\d+/", "runs/<round>/", text)


def view(summary):
    v = {k: summary.get(k) for k in KEYS}
    codes = dict(summary.get("replies_by_code") or {})
    if "-32004" in codes:
        codes["-32004"] = "present"
    v["replies_by_code"] = codes
    v["process_starts"] = [(norm(s["argv"][-1]), s["sandbox_null"]) for s in summary.get("process_starts", [])]
    v["refusals"] = [(r["method"], r["code"], r["sandbox_null"]) for r in summary.get("refusals", [])]
    v["listening_sockets"] = [s["listening"] for s in summary.get("listen_samples", [])]
    v["guard_released"] = all(text.startswith("released") for text in summary.get("guard_markers_after", {}).values())
    v["transport_disconnected_reported"] = any("transport disconnected" in (o or "") for o in summary.get("model_outputs", []))
    v["rejected_reported"] = any("-32600" in (o or "") for o in summary.get("model_outputs", []))
    v["no_tools_reported"] = any("is not a function" in (o or "") for o in summary.get("model_outputs", []))
    v["transport_spawned"] = summary.get("transport_spawns")
    v["ssh_complaint"] = summary.get("transport_stderr_names_alias")
    return v


def main():
    out = Path(sys.argv[1])
    runs = Path(sys.argv[2] if len(sys.argv) > 2 else "runs")
    rounds = sorted(p for p in runs.iterdir() if p.is_dir() and p.name.startswith("r"))
    report, ok = {}, True
    for scenario in SCENARIOS:
        views = {}
        for r in rounds:
            p = r / scenario / "summary.json"
            if p.exists():
                views[r.name] = view(json.loads(p.read_text()))
        first = next(iter(views.values()), None)
        same = all(v == first for v in views.values())
        ok &= same and len(views) == len(rounds)
        report[scenario] = {"rounds": len(views), "consistent": same, "view": first,
                            "differences": {} if same else {k: v for k, v in views.items() if v != first}}
    report["all_consistent"] = ok
    out.write_text(json.dumps(report, indent=2, ensure_ascii=False))
    for s in SCENARIOS:
        print(s, "rounds", report[s]["rounds"], "consistent", report[s]["consistent"])
    print("ALL CONSISTENT" if ok else "DIFFERENCES, see", out)


if __name__ == "__main__":
    main()
