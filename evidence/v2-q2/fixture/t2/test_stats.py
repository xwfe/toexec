import unittest

from stats import mean, moving_average, spread


class TestStats(unittest.TestCase):
    def test_mean(self):
        self.assertEqual(mean([1, 2, 3]), 2)

    def test_spread(self):
        self.assertEqual(spread([4, 1, 9]), 8)

    def test_moving_average_covers_the_last_window(self):
        self.assertEqual(moving_average([1, 2, 3, 4, 5], 3), [2, 3, 4])

    def test_moving_average_window_equals_length(self):
        self.assertEqual(moving_average([2, 4], 2), [3])


if __name__ == "__main__":
    unittest.main()
