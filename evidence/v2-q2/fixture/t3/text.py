"""几个不依赖第三方库的文本函数。词按空白切分，区分大小写。"""


def words(text):
    return text.split()


def word_count(text):
    return len(words(text))


def longest_word(text):
    ws = words(text)
    if not ws:
        return ""
    return max(ws, key=len)
