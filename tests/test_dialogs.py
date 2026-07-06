"""Tests für den Main-Thread-Marshaller der NSAlert-Dialoge (:mod:`src.dialogs`).

Die AppKit-Dialoge selbst brauchen eine GUI und werden per Smoke-Test geprüft;
hier ist nur die reine Marshalling-Logik von :func:`_run_on_main` gekapselt und
ohne laufenden Runloop testbar (der Test läuft ohnehin auf dem Main-Thread →
Direktpfad).

Standalone ausführen::

    .venv/bin/python tests/test_dialogs.py
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import dialogs  # noqa: E402


class RunOnMainMainThreadTest(unittest.TestCase):
    """Auf dem Main-Thread führt _run_on_main direkt aus (kein Dispatch)."""

    def test_returns_value(self):
        self.assertEqual(dialogs._run_on_main(lambda: 42), 42)

    def test_returns_none(self):
        self.assertIsNone(dialogs._run_on_main(lambda: None))

    def test_propagates_exception(self):
        def _boom():
            raise ValueError("kaputt")

        with self.assertRaises(ValueError):
            dialogs._run_on_main(_boom)

    def test_calls_exactly_once(self):
        calls = []
        dialogs._run_on_main(lambda: calls.append(1))
        self.assertEqual(calls, [1])


if __name__ == "__main__":
    unittest.main()
