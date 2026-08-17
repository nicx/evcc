"""Tests für das Main-Thread-Marshalling (:mod:`src.mainthread`).

Die AppKit-UI selbst braucht eine GUI und wird per Smoke-Test geprüft; hier ist nur die
reine Marshalling-Logik gekapselt. Der Test läuft selbst auf dem Main-Thread → geprüft
wird der Direktpfad (ohne laufenden Runloop wäre der Dispatch-Pfad nicht zustellbar).

Standalone ausführen::

    .venv/bin/python tests/test_mainthread.py
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import dialogs, mainthread  # noqa: E402


class RunOnMainMainThreadTest(unittest.TestCase):
    """Auf dem Main-Thread führt run_on_main direkt aus (kein Dispatch)."""

    def test_is_main_thread(self):
        self.assertTrue(mainthread.is_main_thread())

    def test_returns_value(self):
        self.assertEqual(mainthread.run_on_main(lambda: 42), 42)

    def test_returns_none(self):
        self.assertIsNone(mainthread.run_on_main(lambda: None))

    def test_propagates_exception(self):
        def _boom():
            raise ValueError("kaputt")

        with self.assertRaises(ValueError):
            mainthread.run_on_main(_boom)

    def test_calls_exactly_once(self):
        calls = []
        mainthread.run_on_main(lambda: calls.append(1))
        self.assertEqual(calls, [1])


class DialogsReuseTest(unittest.TestCase):
    """dialogs nutzt denselben Marshaller (eine Quelle für die Threading-Regel)."""

    def test_dialogs_uses_shared_marshaller(self):
        self.assertIs(dialogs._run_on_main, mainthread.run_on_main)


if __name__ == "__main__":
    unittest.main()
