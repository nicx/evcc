"""Tests für den Optimizer-Monitor (Infeasible-Läufe in Folge).

Standalone ausführen::

    .venv/bin/python tests/test_health.py
"""

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import health  # noqa: E402


def _run(updated, status):
    return (updated, status)


class OptimizerMonitorTest(unittest.TestCase):
    def _monitor(self, runs, threshold=3):
        m = health.OptimizerMonitor("http://x", failure_threshold=threshold)
        with mock.patch("src.health.optimizer_run", side_effect=runs):
            for _ in runs:
                m.check()
        return m

    def test_problem_after_threshold_distinct_infeasible_runs(self):
        m = self._monitor([_run("t1", "Infeasible"), _run("t2", "Infeasible"),
                           _run("t3", "Infeasible")])
        self.assertTrue(m.is_problem)
        self.assertEqual(m.consecutive_failures, 3)

    def test_same_run_polled_repeatedly_counts_once(self):
        m = self._monitor([_run("t1", "Infeasible")] * 5)
        self.assertEqual(m.consecutive_failures, 1)
        self.assertFalse(m.is_problem)

    def test_feasible_run_resets(self):
        m = self._monitor([_run("t1", "Infeasible"), _run("t2", "Infeasible"),
                           _run("t3", "Feasible")])
        self.assertEqual(m.consecutive_failures, 0)
        self.assertFalse(m.is_problem)

    def test_unknown_state_leaves_counter_unchanged(self):
        m = self._monitor([_run("t1", "Infeasible"), None, _run("t2", "Infeasible")])
        self.assertEqual(m.consecutive_failures, 2)

    def test_optimizer_run_parses_state(self):
        payload = b'{"evopt": {"updated": "2026-10-04T14:42:07+02:00", "res": {"status": "Feasible"}}}'
        resp = mock.MagicMock()
        resp.__enter__.return_value = mock.MagicMock(read=mock.Mock(side_effect=[payload, b""]))
        with mock.patch("src.health.urllib.request.urlopen", return_value=resp), \
             mock.patch("src.health.json.load", return_value={
                 "evopt": {"updated": "T", "res": {"status": "Feasible"}}}):
            self.assertEqual(health.optimizer_run("http://x"), ("T", "Feasible"))

    def test_optimizer_run_none_without_evopt(self):
        resp = mock.MagicMock()
        with mock.patch("src.health.urllib.request.urlopen", return_value=resp), \
             mock.patch("src.health.json.load", return_value={}):
            self.assertIsNone(health.optimizer_run("http://x"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
