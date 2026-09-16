"""T3 的独立判分：不看模型自己写的测试，只看 top_words 的行为。

放进夹具副本里跑（`python3 t3_grade.py`），退出码 0 才算通过。
"""

import sys

sys.path.insert(0, ".")

from text import top_words  # noqa: E402

CASES = [
    ("a b b c c c", 2, [("c", 3), ("b", 2)]),
    # 次数相同按字母序，所以 a 在 b 前面
    ("b a", 2, [("a", 1), ("b", 1)]),
    # n 大于不同词的个数时，有几个给几个
    ("x x y", 5, [("x", 2), ("y", 1)]),
    # 区分大小写：A 和 a 是两个词
    ("A a a", 1, [("a", 2)]),
    ("", 3, []),
]

bad = 0
for text, n, want in CASES:
    try:
        got = top_words(text, n)
    except Exception as e:  # noqa: BLE001
        print("top_words(%r, %r) 抛了 %s: %s" % (text, n, type(e).__name__, e))
        bad += 1
        continue
    if list(got) != want:
        print("top_words(%r, %r) = %r，应当是 %r" % (text, n, got, want))
        bad += 1

print("t3_grade: %d/%d" % (len(CASES) - bad, len(CASES)))
sys.exit(1 if bad else 0)
