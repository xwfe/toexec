import unittest

from text import longest_word, word_count


class TestText(unittest.TestCase):
    def test_word_count(self):
        self.assertEqual(word_count("a bb ccc"), 3)

    def test_longest_word(self):
        self.assertEqual(longest_word("a bb ccc"), "ccc")

    def test_longest_word_empty(self):
        self.assertEqual(longest_word("   "), "")


if __name__ == "__main__":
    unittest.main()
