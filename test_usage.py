import unittest
from collector import codex_windows, window


class QuotaTests(unittest.TestCase):
    def test_weekly_primary_is_not_a_session(self):
        short, weekly = codex_windows({'primary': {'windowDurationMins': 10080, 'usedPercent': 10}})
        self.assertIsNone(short)
        self.assertEqual(weekly['used'], 10)

    def test_regular_two_windows(self):
        short, weekly = codex_windows({'primary': {'windowDurationMins': 300, 'usedPercent': 100, 'resetsAt': 123},
                                      'secondary': {'windowDurationMins': 10080, 'usedPercent': 75}})
        self.assertEqual(short['reset'], 123)
        self.assertEqual(short['used'], 100)
        self.assertEqual(weekly['used'], 75)

    def test_unknown_is_not_zero(self):
        self.assertIsNone(window(None))
        self.assertIsNone(window({'resetsAt': None})['used'])
        self.assertEqual(window({'utilization': 0}, 'utilization')['used'], 0)


if __name__ == '__main__':
    unittest.main()
