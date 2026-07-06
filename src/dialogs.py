"""Native NSAlert-Dialoge (rumps-frei) — Baustein 1 der rumps→PyObjC-Umstellung.

Ersetzt die verstreuten ``rumps.alert``/``rumps.Window``-Aufrufe durch dünne
AppKit-Helfer. Wie :mod:`src.settings_window` bewusst **rumps-frei**, damit das
Modul in einer späteren reinen AppKit-Shell unverändert weiterlebt.

Threading: NSAlert/NSTextField/NSTextView dürfen nur auf dem Main-Thread laufen.
Da einige Aufrufer (z. B. der Backup-Pfad in :mod:`src.app`) aus Daemon-Threads
kommen, marshallt jeder Helfer via :func:`_run_on_main` **synchron** auf den
Main-Thread und reicht dessen Rückgabe (bzw. Ausnahme) an den Aufrufer zurück.

AppKit wird **lazy innerhalb der Funktionen** importiert, damit das Modul auch
ohne GUI importierbar bleibt (parallel zu ``settings_window``).
"""

from __future__ import annotations

import logging
import threading
from typing import Callable, Optional, TypeVar

LOGGER = logging.getLogger(__name__)

T = TypeVar("T")


def _run_on_main(func: Callable[[], T]) -> T:
    """Führt ``func`` synchron auf dem Main-Thread aus und reicht Rückgabe/Fehler durch.

    Auf dem Main-Thread direkt; sonst über die Main-Operation-Queue dispatchen und
    per :class:`threading.Event` auf das Ergebnis warten. Voraussetzung ist ein
    laufender Main-Runloop (rumps/NSApplication liefert den).
    """
    from Foundation import NSOperationQueue, NSThread

    if NSThread.isMainThread():
        return func()

    box: dict = {}
    done = threading.Event()

    def _wrapper() -> None:
        try:
            box["value"] = func()
        except Exception as exc:  # noqa: BLE001 - auf den Aufrufer-Thread weiterreichen
            box["error"] = exc
        finally:
            done.set()

    NSOperationQueue.mainQueue().addOperationWithBlock_(_wrapper)
    done.wait()
    if "error" in box:
        raise box["error"]
    return box.get("value")


def _activate() -> None:
    """App nach vorne holen, damit der Dialog Fokus bekommt (wie der NSOpenPanel-Pfad)."""
    from AppKit import NSApp

    try:
        NSApp.activateIgnoringOtherApps_(True)
    except Exception:  # noqa: BLE001 - Fokus ist best effort, nie blockieren
        LOGGER.debug("NSApp.activateIgnoringOtherApps_ fehlgeschlagen", exc_info=True)


def _make_alert(title: str, message: str):
    from AppKit import NSAlert

    alert = NSAlert.alloc().init()
    alert.setMessageText_(title)
    if message:
        alert.setInformativeText_(message)
    return alert


def alert(title: str, message: str = "") -> None:
    """Einfacher informativer Hinweis mit OK-Button (Ersatz für ``rumps.alert``)."""

    def _show() -> None:
        a = _make_alert(title, message)
        a.addButtonWithTitle_("OK")
        _activate()
        a.runModal()

    _run_on_main(_show)


def ask_yes_no(title: str, message: str = "") -> bool:
    """Ja/Nein-Rückfrage. Rückgabe ``True`` bei „Ja"."""
    from AppKit import NSAlertFirstButtonReturn

    def _show() -> bool:
        a = _make_alert(title, message)
        a.addButtonWithTitle_("Ja")
        a.addButtonWithTitle_("Nein")
        _activate()
        return a.runModal() == NSAlertFirstButtonReturn

    return bool(_run_on_main(_show))


def ask_text(title: str, message: str = "", default: str = "") -> Optional[str]:
    """Einzeilige Texteingabe. Rückgabe getrimmt oder ``None`` bei Abbruch."""
    from AppKit import NSAlertFirstButtonReturn, NSTextField
    from Foundation import NSMakeRect

    def _show() -> Optional[str]:
        a = _make_alert(title, message)
        a.addButtonWithTitle_("OK")
        a.addButtonWithTitle_("Abbrechen")
        field = NSTextField.alloc().initWithFrame_(NSMakeRect(0, 0, 320, 24))
        field.setStringValue_(default or "")
        a.setAccessoryView_(field)
        try:
            a.window().setInitialFirstResponder_(field)
        except Exception:  # noqa: BLE001 - Fokus ist best effort
            LOGGER.debug("setInitialFirstResponder_ fehlgeschlagen", exc_info=True)
        _activate()
        if a.runModal() != NSAlertFirstButtonReturn:
            return None
        return str(field.stringValue()).strip()

    return _run_on_main(_show)


def show_text(title: str, body: str) -> None:
    """Längeren, monospaced Text (z. B. Log-Tail) in einer scrollbaren Ansicht zeigen."""
    from AppKit import NSFont, NSScrollView, NSTextView
    from Foundation import NSMakeRect

    def _show() -> None:
        a = _make_alert(title, "")
        a.addButtonWithTitle_("OK")
        rect = NSMakeRect(0, 0, 560, 320)
        scroll = NSScrollView.alloc().initWithFrame_(rect)
        scroll.setHasVerticalScroller_(True)
        scroll.setBorderType_(1)  # NSBezelBorder
        text = NSTextView.alloc().initWithFrame_(rect)
        text.setEditable_(False)
        text.setFont_(NSFont.monospacedSystemFontOfSize_weight_(11, 0))
        text.setString_(body or "")
        scroll.setDocumentView_(text)
        a.setAccessoryView_(scroll)
        _activate()
        a.runModal()

    _run_on_main(_show)
