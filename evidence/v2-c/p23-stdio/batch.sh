#!/bin/sh
# Run the four scenarios for several rounds: batch.sh [round...], default r1 r2 r3,
# results in runs/<round>/<scenario>/. Then compare with compare.py.
cd "$(dirname "$0")"
[ $# -eq 0 ] && set -- r1 r2 r3
for r in "$@"; do
  mkdir -p runs/$r
  for e in basic refused drop unreachable; do
    rm -rf runs/$r/$e
    python3 harness.py $e runs/$r/$e </dev/null > runs/$r/$e.out 2>&1
    echo "$r $e $?" >> runs/batch.log
  done
done
echo DONE >> runs/batch.log
