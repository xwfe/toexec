#!/bin/sh
# 把 17 个实验按同一顺序跑若干轮：batch.sh [轮次名...]，默认 r1 r2 r3，结果写 runs/<轮次>/<实验>/。
# 跑完用 compare.py 逐项比。每个实验之间先删掉 Agent 侧标记文件，出现了就记一笔。
cd "$(dirname "$0")"
[ $# -eq 0 ] && set -- r1 r2 r3
for r in "$@"; do
  mkdir -p runs/$r
  for e in exec-remote-cwd exec-local-cwd exec-shared-path tui-remote-cwd tui-no-cwd tui-shared-path tui-surface tui-escalate-accept tui-escalate-decline git-none git-parent-forwarded git-parent-intercepted git-root policy-basic policy-surface policy-escalate-accept project-config-trusted; do
    rm -f agent-side-marker
    python3 probe.py $e runs/$r/$e </dev/null > runs/$r/$e.out 2>&1
    echo "$r $e $?" >> runs/batch.log
    [ -e agent-side-marker ] && echo "$r $e AGENT-MARKER-PRESENT" >> runs/batch.log
  done
done
echo DONE >> runs/batch.log
