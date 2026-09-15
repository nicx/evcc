"""Tests für sicherheitsrelevante Helfer in lifecycle (ohne Netz/launchd).

Standalone ausführen::

    .venv/bin/python tests/test_lifecycle.py
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import lifecycle  # noqa: E402


class DownloadGuardTest(unittest.TestCase):
    def test_rejects_non_https(self):
        dest = Path(tempfile.mkdtemp(prefix="evcc_dl_")) / "x"
        for bad in ("http://example.com/evcc.tar.gz", "file:///etc/passwd", "ftp://h/x"):
            with self.assertRaises(ValueError):
                lifecycle.download_file(bad, dest)
        self.assertFalse(dest.exists())


class RollbackAtomicityTest(unittest.TestCase):
    """save_previous/rollback müssen das Ziel per rename ersetzen, nie in-place beschreiben.

    Hintergrund (2026-09-15): ein in-place-Write (``shutil.copy2`` direkt auf eine
    bestehende Datei) auf ein kürzlich ausgeführtes Mach-O-Binary kann macOS' gecachte
    Signaturprüfung (taskgated/AMFI) verunreinigen — die Datei ist inhaltlich unverändert
    und `codesign --verify` sieht sie als gültig an, der nächste Start wird aber trotzdem
    mit `SIGKILL (Code Signature Invalid)` getötet. `rename` erzeugt eine neue Inode, was
    den Kernel zwingt, die Signatur frisch zu prüfen. Diese Tests verifizieren nur die
    Dateisystem-Eigenschaft (neue Inode, korrekte Bytes/Rechte) — die AMFI-Kausalkette
    selbst ist nur auf echtem macOS reproduzierbar, nicht mock-testbar.
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="evcc_rollback_"))
        self.patcher_bin = mock.patch("src.lifecycle.paths.evcc_binary",
                                       return_value=self.tmp / "evcc")
        self.patcher_prev = mock.patch("src.lifecycle.paths.evcc_binary_previous",
                                       return_value=self.tmp / "evcc.previous")
        self.patcher_bin.start()
        self.patcher_prev.start()
        self.addCleanup(self.patcher_bin.stop)
        self.addCleanup(self.patcher_prev.stop)
        # remove_quarantine ruft /usr/bin/xattr per Subprocess auf; im Test irrelevant.
        self.patcher_xattr = mock.patch("src.lifecycle.remove_quarantine")
        self.patcher_xattr.start()
        self.addCleanup(self.patcher_xattr.stop)

    def test_save_previous_replaces_target_via_new_inode(self):
        current = self.tmp / "evcc"
        current.write_bytes(b"binary-v1")
        current.chmod(0o755)
        prev = self.tmp / "evcc.previous"
        prev.write_bytes(b"stale-old-content")
        prev_inode_before = prev.stat().st_ino

        result = lifecycle.save_previous()

        self.assertEqual(result, prev)
        self.assertEqual(prev.read_bytes(), b"binary-v1")
        self.assertNotEqual(prev.stat().st_ino, prev_inode_before,
                            "save_previous muss über rename ersetzen, nicht in-place schreiben")
        self.assertEqual(oct(prev.stat().st_mode)[-3:], "755")

    def test_save_previous_no_current_binary(self):
        self.assertIsNone(lifecycle.save_previous())

    def test_rollback_replaces_target_via_new_inode(self):
        target = self.tmp / "evcc"
        target.write_bytes(b"broken-current")
        target.chmod(0o755)
        target_inode_before = target.stat().st_ino
        prev = self.tmp / "evcc.previous"
        prev.write_bytes(b"known-good-binary")
        prev.chmod(0o755)

        ok = lifecycle.rollback()

        self.assertTrue(ok)
        self.assertEqual(target.read_bytes(), b"known-good-binary")
        self.assertNotEqual(target.stat().st_ino, target_inode_before,
                            "rollback muss über rename ersetzen, nicht in-place schreiben")
        self.assertEqual(oct(target.stat().st_mode)[-3:], "755")

    def test_rollback_without_previous_binary(self):
        self.assertFalse(lifecycle.rollback())

    def test_no_leftover_part_file(self):
        current = self.tmp / "evcc"
        current.write_bytes(b"binary-v1")
        lifecycle.save_previous()
        self.assertFalse((self.tmp / "evcc.previous.part").exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
