"""The preflight's wait for a busy machine: it measures again until the machine settles, and refuses only one that
stays busy for the whole settle time; the limit itself does not move."""
import unittest
from unittest import mock

import preflight


class SettleTests(unittest.TestCase):
    def test_a_machine_busy_for_a_moment_is_measured_again_until_it_settles(self):
        samples = iter([28.4, 19.0, 12.0])
        with mock.patch.object(preflight, "busy_percent", lambda: next(samples)), \
                mock.patch.object(preflight.time, "sleep", lambda _: None):
            self.assertEqual(preflight.settled_busy_percent(), [28.4, 19.0, 12.0])

    def test_an_idle_machine_is_measured_once(self):
        with mock.patch.object(preflight, "busy_percent", lambda: 11.8):
            self.assertEqual(preflight.settled_busy_percent(), [11.8])

    def test_a_machine_that_stays_busy_ends_above_the_limit_after_the_settle_time(self):
        clock = iter(range(0, 1000, 5))
        with mock.patch.object(preflight, "busy_percent", lambda: 30.0), \
                mock.patch.object(preflight.time, "sleep", lambda _: None), \
                mock.patch.object(preflight.time, "time", lambda: next(clock)):
            samples = preflight.settled_busy_percent(settle_s=20)
        self.assertEqual(len(samples), 4)
        self.assertGreater(samples[-1], preflight.BUSY_PERCENT)
        self.assertEqual(preflight.BUSY_PERCENT, 15.0)


if __name__ == "__main__":
    unittest.main()
