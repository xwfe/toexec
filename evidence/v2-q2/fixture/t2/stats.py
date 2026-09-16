"""几个不依赖第三方库的统计函数。"""


def mean(xs):
    return sum(xs) / len(xs)


def moving_average(xs, window):
    """长度 n 的序列、窗口 w，应当得到 n - w + 1 个平均值。"""
    out = []
    for i in range(len(xs) - window):
        out.append(mean(xs[i : i + window]))
    return out


def spread(xs):
    return max(xs) - min(xs)
