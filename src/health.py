"""HTTP-Healthcheck der evcc-Instanz (localhost:7070).

Stateless-Probe (:func:`probe`) plus ein kleiner Zähler-Wrapper (:class:`HealthMonitor`),
der aufeinanderfolgende Fehlversuche gegen einen Schwellwert hält. Der Schwellwert speist
die Notifier-State-Machine (erst nach N Fehlversuchen in Folge gilt es als "Problem").
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request

LOGGER = logging.getLogger(__name__)

# Zustände
RUNNING = "running"          # HTTP-Antwort erhalten -> evcc lebt
UNREACHABLE = "unreachable"  # Verbindung verweigert/Timeout -> evcc antwortet nicht
STOPPED = "stopped"          # Agent gar nicht geladen (von außen gesetzt, nicht hier ermittelt)


def probe(url: str, timeout: float = 5.0) -> str:
    """Pollt ``url``. ``running`` bei beliebiger HTTP-Antwort, sonst ``unreachable``.

    Jede HTTP-Statuszeile (auch 4xx/5xx) bedeutet "Server lebt" — evcc kann je nach Pfad
    auch Redirects/Fehler liefern. Nur Verbindungsfehler/Timeout zählen als ``unreachable``.
    """
    req = urllib.request.Request(url, method="GET", headers={"User-Agent": "evcc"})
    try:
        with urllib.request.urlopen(req, timeout=timeout):
            return RUNNING
    except urllib.error.HTTPError:
        return RUNNING  # HTTP-Fehlerstatus = Server antwortet trotzdem
    except (urllib.error.URLError, OSError, ValueError) as exc:
        LOGGER.debug("Healthcheck %s nicht erreichbar: %s", url, exc)
        return UNREACHABLE


class HealthMonitor:
    """Hält den letzten Zustand und zählt Fehlversuche in Folge gegen einen Schwellwert."""

    def __init__(self, url: str, failure_threshold: int = 3) -> None:
        self.url = url
        self.failure_threshold = max(1, failure_threshold)
        self._consecutive_failures = 0
        self.last_state: str = RUNNING

    def check(self, timeout: float = 5.0) -> str:
        """Führt eine Probe aus, aktualisiert Zähler/Zustand und gibt den Zustand zurück."""
        state = probe(self.url, timeout=timeout)
        if state == RUNNING:
            self._consecutive_failures = 0
        else:
            self._consecutive_failures += 1
        self.last_state = state
        return state

    @property
    def is_problem(self) -> bool:
        """True, sobald der Fehlversuch-Schwellwert erreicht/überschritten ist."""
        return self._consecutive_failures >= self.failure_threshold

    @property
    def consecutive_failures(self) -> int:
        return self._consecutive_failures


def optimizer_run(base_url: str, timeout: float = 5.0) -> tuple[str, str] | None:
    """Liest den letzten Optimizer-Lauf aus ``/api/state``: ``(updated, status)``.

    ``None``, wenn nicht ermittelbar (evcc nicht erreichbar, Optimizer aus, noch kein Lauf,
    unerwartete Antwort). Der Lauf-Zeitstempel erlaubt es, mehrfaches Pollen desselben
    Laufs nicht als neue Runde zu zählen.
    """
    req = urllib.request.Request(base_url.rstrip("/") + "/api/state", method="GET",
                                 headers={"User-Agent": "evcc"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            evopt = json.load(resp).get("evopt") or {}
        updated, status = evopt.get("updated"), (evopt.get("res") or {}).get("status")
    except (urllib.error.URLError, OSError, ValueError, AttributeError) as exc:
        LOGGER.debug("Optimizer-Status nicht lesbar: %s", exc)
        return None
    if not updated or not status:
        return None
    return str(updated), str(status)


class OptimizerMonitor:
    """Zählt aufeinanderfolgende **verschiedene** Optimizer-Läufe mit Status ``Infeasible``.

    Hintergrund: Ein Ausreißer in ``meters`` (z. B. Zählerfehler) macht den Optimizer
    wochenlang unlösbar, ohne dass es auffällt. Erst ``failure_threshold`` Läufe in Folge
    gelten als Problem (der Optimizer läuft ca. alle 15 min), ein ``Feasible`` setzt zurück.
    """

    def __init__(self, base_url: str, failure_threshold: int = 3) -> None:
        self.base_url = base_url
        self.failure_threshold = max(1, failure_threshold)
        self._consecutive = 0
        self._last_updated: str | None = None

    def check(self, timeout: float = 5.0) -> None:
        run = optimizer_run(self.base_url, timeout=timeout)
        if run is None:
            return  # unbekannt -> Zustand unverändert lassen
        updated, status = run
        if updated == self._last_updated:
            return  # derselbe Lauf wie beim letzten Poll
        self._last_updated = updated
        self._consecutive = self._consecutive + 1 if status == "Infeasible" else 0

    @property
    def is_problem(self) -> bool:
        return self._consecutive >= self.failure_threshold

    @property
    def consecutive_failures(self) -> int:
        return self._consecutive
