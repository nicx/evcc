"""Main-Thread-Marshalling für AppKit-Aufrufe (rumps-frei).

AppKit ist nicht thread-sicher: NSStatusItem/NSMenu/NSAlert/NSTimer dürfen nur auf dem
Main-Thread berührt werden. Die App erledigt aber alles Langlaufende in Daemon-Threads
(:meth:`src.app.EvccApp._spawn`), daher braucht jede UI-Schicht denselben Marshaller.

Ursprünglich lokal in :mod:`src.dialogs` entstanden; hier herausgezogen, damit
:mod:`src.statusitem`, :mod:`src.timers` und :mod:`src.notify` dieselbe Implementierung
nutzen (eine Quelle für die Threading-Regel).
"""

from __future__ import annotations

import logging
import threading
from typing import Callable, TypeVar

LOGGER = logging.getLogger(__name__)

T = TypeVar("T")


def is_main_thread() -> bool:
    """True, wenn der aufrufende Thread der AppKit-Main-Thread ist."""
    from Foundation import NSThread

    return bool(NSThread.isMainThread())


def run_on_main(func: Callable[[], T]) -> T:
    """Führt ``func`` **synchron** auf dem Main-Thread aus; reicht Rückgabe/Fehler durch.

    Auf dem Main-Thread direkt (kein Dispatch, keine Deadlock-Gefahr); sonst über die
    Main-Operation-Queue und per :class:`threading.Event` auf das Ergebnis warten.
    Voraussetzung ist ein laufender Main-Runloop (``NSApplication.run``).
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


def post_to_main(func: Callable[[], object]) -> None:
    """Stellt ``func`` **asynchron** auf den Main-Thread (feuern und vergessen).

    Für UI-Updates aus Hintergrund-Threads, bei denen niemand auf ein Ergebnis wartet —
    dort wäre das synchrone :func:`run_on_main` unnötige Blockade.
    """
    from Foundation import NSOperationQueue

    def _wrapper() -> None:
        try:
            func()
        except Exception:  # noqa: BLE001 - UI-Update darf den Thread nie killen
            LOGGER.exception("Main-Thread-Task fehlgeschlagen")

    NSOperationQueue.mainQueue().addOperationWithBlock_(_wrapper)
