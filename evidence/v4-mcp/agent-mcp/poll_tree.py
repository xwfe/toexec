"""每 0.25 秒看一次进程表，记下命令行含 ROOT 的进程和它们的子孙（每个 pid 记第一次见到的那行）。

用法：python3 poll_tree.py '<ROOT 片段>' <输出文件> <最多秒数>
只记 pid、ppid、首次见到的时间和命令行的前 200 个字符。
"""
import subprocess
import sys
import time

root, out, limit = sys.argv[1], sys.argv[2], float(sys.argv[3])
seen = {}
start = time.time()
with open(out, "w") as f:
    while time.time() - start < limit:
        rows = subprocess.run(["ps", "-axo", "pid=,ppid=,command="], capture_output=True,
                              text=True).stdout.splitlines()
        procs = {}
        for row in rows:
            parts = row.split(None, 2)
            if len(parts) == 3:
                procs[int(parts[0])] = (int(parts[1]), parts[2])
        roots = {p for p, (_, cmd) in procs.items() if root in cmd and "poll_tree" not in cmd}
        keep = set(roots)
        changed = True
        while changed:
            changed = False
            for p, (pp, _) in procs.items():
                if pp in keep and p not in keep:
                    keep.add(p)
                    changed = True
        for p in sorted(keep):
            if p not in seen:
                seen[p] = True
                pp, cmd = procs[p]
                f.write(f"{time.strftime('%H:%M:%S')} {p} {pp} {cmd[:200]}\n")
                f.flush()
        time.sleep(0.25)
