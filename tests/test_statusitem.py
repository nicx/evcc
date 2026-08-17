"""Tests für die Menü-Spezifikation (:mod:`src.statusitem`).

Geprüft wird die **reine Datenschicht** (MenuEntry/SEPARATOR) — sie entscheidet, welche
Einträge klickbar sind. Die AppKit-Schicht (NSStatusItem/NSMenu) braucht eine laufende
GUI und wird per Smoke-Test abgedeckt.

Standalone ausführen::

    .venv/bin/python tests/test_statusitem.py
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.statusitem import SEPARATOR, MenuEntry  # noqa: E402


class MenuEntryEnabledTest(unittest.TestCase):
    """Aktiv-Zustand: automatisch aus Callback/Untermenü, oder explizit erzwungen."""

    def test_info_line_without_callback_is_disabled(self):
        # Statuszeile/Version sind reine Info und dürfen nicht klickbar sein.
        self.assertFalse(MenuEntry("● evcc — läuft").is_enabled())

    def test_entry_with_callback_is_enabled(self):
        self.assertTrue(MenuEntry("Start", lambda: None).is_enabled())

    def test_submenu_without_callback_is_enabled(self):
        parent = MenuEntry("Logs", children=[MenuEntry("Tail", lambda: None)])
        self.assertTrue(parent.is_enabled())

    def test_explicit_enabled_overrides_auto(self):
        self.assertFalse(MenuEntry("X", lambda: None, enabled=False).is_enabled())
        self.assertTrue(MenuEntry("X", enabled=True).is_enabled())

    def test_empty_children_is_disabled(self):
        self.assertFalse(MenuEntry("Leer", children=[]).is_enabled())


class MenuEntryShapeTest(unittest.TestCase):
    def test_defaults(self):
        entry = MenuEntry("X")
        self.assertIsNone(entry.callback)
        self.assertFalse(entry.checked)
        self.assertIsNone(entry.children)
        self.assertIsNone(entry.enabled)

    def test_checked_flag_roundtrip(self):
        self.assertTrue(MenuEntry("Autostart", lambda: None, checked=True).checked)

    def test_separator_is_distinct_sentinel(self):
        # Die AppKit-Schicht erkennt Trenner per Identität ("entry is SEPARATOR").
        self.assertIsNot(SEPARATOR, MenuEntry("-"))
        self.assertIs(SEPARATOR, SEPARATOR)
        self.assertNotIsInstance(SEPARATOR, MenuEntry)


if __name__ == "__main__":
    unittest.main()
