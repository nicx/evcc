"""evcc-Binary-Verwaltung und launchd-Steuerung.

Verantwortlich für: Binary-Datei-Operationen (Quarantäne entfernen, aus Tarball
installieren, Vorgänger sichern, Rollback) und die Steuerung des launchd-LaunchAgents,
der das evcc-Binary betreibt.

Trennung der Verantwortlichkeiten: :mod:`src.updater` liefert die Release-/Versionslogik
(GitHub-API, Checksum) und ruft hier die eigentlichen Datei-Operationen auf. Diese Schicht
kennt kein GitHub.

DB-Pfad und Loglevel werden als verifizierte CLI-Flags ins Plist geschrieben
(``--database <pfad> --log <level>``), nicht über eine separate evcc.yaml — das ist
deterministisch und gegen die aktuelle evcc-Doku geprüft (siehe Plan).
"""

from __future__ import annotations

import logging
import os
import plistlib
import shutil
import subprocess
import tarfile
import urllib.request
from pathlib import Path
from typing import Optional

from . import paths

LOGGER = logging.getLogger(__name__)


# -- launchd-Plist -----------------------------------------------------------

def write_agent_plist(level: str = "info") -> Path:
    """Schreibt das LaunchAgent-Plist für den evcc-Agenten und gibt den Pfad zurück.

    ProgramArguments: ``<binary> --database <db> --log <level>``. StdOut/StdErr werden
    auf das feste evcc-Logfile umgeleitet. ``RunAtLoad`` + ``KeepAlive`` = Autostart und
    Neustart bei Absturz (genau das, was ``brew services`` intern macht).
    """
    plist_path = paths.agent_plist_path()
    plist_path.parent.mkdir(parents=True, exist_ok=True)
    log = str(paths.evcc_log_file())
    payload = {
        "Label": paths.AGENT_LABEL,
        "ProgramArguments": [
            str(paths.evcc_binary()),
            "--database", str(paths.db_file()),
            "--log", level,
        ],
        "RunAtLoad": True,
        "KeepAlive": True,
        "StandardOutPath": log,
        "StandardErrorPath": log,
    }
    with open(plist_path, "wb") as fh:
        plistlib.dump(payload, fh)
    LOGGER.info("Agent-Plist geschrieben: %s (level=%s)", plist_path, level)
    return plist_path


def _gui_domain_target() -> str:
    return f"gui/{os.getuid()}/{paths.AGENT_LABEL}"


def bootstrap() -> bool:
    """Lädt den Agenten (moderne API ``launchctl bootstrap``; Legacy ``load -w`` als Fallback)."""
    plist = str(paths.agent_plist_path())
    domain = f"gui/{os.getuid()}"
    if _launchctl("bootstrap", domain, plist):
        return True
    LOGGER.info("bootstrap fehlgeschlagen, Fallback auf 'load -w'")
    return _launchctl("load", "-w", plist)


def bootout() -> bool:
    """Entlädt den Agenten (``bootout``; Legacy ``unload -w`` als Fallback)."""
    if _launchctl("bootout", _gui_domain_target()):
        return True
    LOGGER.info("bootout fehlgeschlagen, Fallback auf 'unload -w'")
    return _launchctl("unload", "-w", str(paths.agent_plist_path()))


def kickstart() -> bool:
    """Startet den Agenten neu (``kickstart -k``)."""
    return _launchctl("kickstart", "-k", _gui_domain_target())


def is_loaded() -> bool:
    """True, wenn der Agent bei launchd registriert ist (``launchctl print``)."""
    try:
        res = subprocess.run(
            ["launchctl", "print", _gui_domain_target()],
            check=False, capture_output=True, timeout=10,
        )
        return res.returncode == 0
    except (OSError, subprocess.SubprocessError) as exc:
        LOGGER.debug("launchctl print fehlgeschlagen: %s", exc)
        return False


def _launchctl(*args: str) -> bool:
    """Best-effort ``launchctl``-Aufruf. True bei returncode 0."""
    try:
        res = subprocess.run(["launchctl", *args], check=False,
                             capture_output=True, timeout=15)
        if res.returncode != 0:
            LOGGER.debug("launchctl %s -> rc=%s stderr=%s", args, res.returncode,
                         res.stderr.decode(errors="replace").strip())
        return res.returncode == 0
    except (OSError, subprocess.SubprocessError) as exc:
        LOGGER.debug("launchctl %s fehlgeschlagen: %s", args, exc)
        return False


# -- Binary-Datei-Operationen ------------------------------------------------

def remove_quarantine(path: Path) -> None:
    """Entfernt das ``com.apple.quarantine``-Attribut (sonst blockt Gatekeeper).

    Best-effort: ``xattr -d`` schlägt fehl, wenn das Attribut gar nicht gesetzt ist —
    das ist unkritisch und wird nur geloggt.
    """
    try:
        subprocess.run(["xattr", "-d", "com.apple.quarantine", str(path)],
                       check=False, capture_output=True, timeout=10)
    except (OSError, subprocess.SubprocessError) as exc:
        LOGGER.debug("xattr -d com.apple.quarantine fehlgeschlagen: %s", exc)


def download_file(url: str, dest: Path, timeout: float = 60.0) -> Path:
    """Lädt eine Datei nach ``dest`` (atomar über ``.part``). Wirft bei Fehler.

    Erzwingt ``https://`` — verhindert, dass ein manipuliertes API-Response per ``http://``
    oder ``file://`` einen unsicheren bzw. lokalen Pfad einschleust.
    """
    if not url.lower().startswith("https://"):
        raise ValueError(f"Download nur über HTTPS erlaubt, nicht: {url!r}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    req = urllib.request.Request(url, headers={"User-Agent": "evcc"})
    with urllib.request.urlopen(req, timeout=timeout) as resp, open(tmp, "wb") as fh:
        shutil.copyfileobj(resp, fh)
    tmp.replace(dest)
    return dest


def extract_evcc_from_tar(tar_path: Path, dest: Path) -> Path:
    """Extrahiert das ``evcc``-Binary aus einem ``*_macOS_all.tar.gz`` nach ``dest``.

    Sucht im Tarball den Eintrag mit Basename ``evcc``, schreibt ihn nach ``dest``,
    setzt ``chmod +x`` und entfernt die Quarantäne.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(tar_path, "r:gz") as tar:
        member = next((m for m in tar.getmembers()
                       if m.isfile() and os.path.basename(m.name) == "evcc"), None)
        if member is None:
            raise FileNotFoundError(f"Kein 'evcc'-Binary in {tar_path.name} gefunden")
        src = tar.extractfile(member)
        if src is None:
            raise FileNotFoundError(f"'evcc'-Eintrag in {tar_path.name} nicht lesbar")
        tmp = dest.with_suffix(dest.suffix + ".part")
        with src, open(tmp, "wb") as fh:
            shutil.copyfileobj(src, fh)
        tmp.replace(dest)
    dest.chmod(0o755)
    remove_quarantine(dest)
    return dest


def _replace_binary_atomically(src: Path, dest: Path) -> None:
    """Kopiert ``src`` nach ``dest`` über ``.part`` + atomaren ``rename`` (neue Inode).

    **Nie** ein bestehendes, ausführbares Mach-O-Binary in-place überschreiben (z. B. via
    ``shutil.copy2`` direkt auf ``dest``): macOS' Laufzeit-Signaturprüfung (taskgated/AMFI)
    cached ihr Ergebnis pro Datei/Inode. Ein In-Place-Write auf eine kürzlich ausgeführte
    Datei kann diesen Cache inkonsistent machen — die Datei ist inhaltlich unverändert und
    ``codesign --verify`` sieht sie als gültig an, der nächste Start wird aber trotzdem mit
    ``SIGKILL (Code Signature Invalid)`` getötet (beobachtet 2026-09-15, siehe CLAUDE.md).
    ``rename`` erzeugt dagegen eine frische Inode → der Kernel prüft die Signatur neu, statt
    einen möglicherweise verunreinigten Cache-Eintrag wiederzuverwenden.
    """
    tmp = dest.with_suffix(dest.suffix + ".part")
    shutil.copy2(src, tmp)
    tmp.chmod(0o755)
    tmp.replace(dest)


def save_previous() -> Optional[Path]:
    """Sichert das aktuelle Binary nach ``evcc.previous`` (für Rollback). None, wenn keins da."""
    current = paths.evcc_binary()
    if not current.exists():
        return None
    prev = paths.evcc_binary_previous()
    _replace_binary_atomically(current, prev)
    return prev


def rollback() -> bool:
    """Spielt ``evcc.previous`` zurück auf das aktive Binary. False, wenn kein Vorgänger da."""
    prev = paths.evcc_binary_previous()
    if not prev.exists():
        LOGGER.warning("Kein Vorgänger-Binary für Rollback vorhanden: %s", prev)
        return False
    target = paths.evcc_binary()
    _replace_binary_atomically(prev, target)
    remove_quarantine(target)
    LOGGER.info("Rollback auf vorheriges Binary durchgeführt")
    return True
