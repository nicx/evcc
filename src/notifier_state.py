"""State-Machine-Debounce für Benachrichtigungen (Spec §3.6/§4).

Verhindert Dauerfeuer: pro Bedingung wird nur bei einem **Zustandswechsel** eine Mail
verschickt — eine Problem-Mail beim Übergang gesund→Problem, eine Recovery-Mail beim
Übergang Problem→gesund. Während einer anhaltenden Störung passiert nichts weiter.

Diese Logik existiert in keinem der Vorläuferprojekte (iCloud-Sync verschickt stateless)
und ist daher neu. Der Transport selbst ist :func:`src.notify.send_mail` (lokales MailRelay).
"""

from __future__ import annotations

import logging
import time
from typing import Callable, Optional

from . import notify
from .config.settings import NotificationSettings

LOGGER = logging.getLogger(__name__)

# Bedingung -> (Problem-Betreff, Recovery-Betreff). Deckt die Trigger aus Spec §4 ab.
CONDITIONS: dict[str, tuple[str, str]] = {
    "evcc_unreachable": ("evcc nicht erreichbar", "evcc wieder erreichbar"),
    "backup_failed": ("evcc-Backup fehlgeschlagen", "evcc-Backup wieder erfolgreich"),
    "update_failed": ("evcc-Update fehlgeschlagen", "evcc-Update-Problem behoben"),
    "optimizer_infeasible": ("evcc-Optimizer unlösbar (Infeasible)",
                             "evcc-Optimizer wieder lösbar"),
    "db_migration": ("evcc-DB-Migration auffällig", "evcc-DB-Migration behoben"),
}

_MAX_SEND_RETRIES = 3
_RETRY_BACKOFF_SECONDS = 2.0


class NotifierState:
    """Hält pro Bedingung den Zustand (gesund/Problem) und mailt nur bei Wechsel.

    :param settings_provider: Callable, das die aktuellen :class:`NotificationSettings`
        liefert (so wirken Einstellungsänderungen sofort, ohne Neuinstanz).
    :param mailer: Mail-Transport (Default :func:`src.notify.send_mail`) — für Tests injizierbar.
    :param desktop_notify: macOS-Notification-Callback (Default :func:`src.notify.notify`).
    :param sleep: Verzögerungsfunktion für den Sende-Retry (für Tests injizierbar).
    """

    def __init__(
        self,
        settings_provider: Callable[[], NotificationSettings],
        mailer: Callable[..., bool] = notify.send_mail,
        desktop_notify: Optional[Callable[[str, str], None]] = notify.notify,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._settings_provider = settings_provider
        self._mailer = mailer
        self._desktop_notify = desktop_notify
        self._sleep = sleep
        # Bedingung -> True (aktuell Problem) / False/absent (gesund).
        self._problem: dict[str, bool] = {}

    def is_problem(self, condition: str) -> bool:
        return self._problem.get(condition, False)

    def notify_event(self, subject: str, body: str = "") -> bool:
        """Einmalige Info-Benachrichtigung **ohne** Zustandslogik (z. B. "Update verfügbar").

        Anders als :meth:`problem`/:meth:`healthy` wird hier kein Zustand gehalten — der
        Aufrufer ist für Entprellung/Deduplizierung zuständig (z. B. eine Mail pro Version).
        Verschickt Desktop-Notification + Mail (mit Retry); gibt True zurück, wenn die Mail
        zugestellt wurde (sonst False, z. B. Mail deaktiviert).
        """
        return self._emit(subject, body or subject)

    def report(self, condition: str, healthy: bool, detail: str = "") -> None:
        """Meldet den aktuellen Zustand einer Bedingung; mailt nur bei echtem Wechsel.

        :param healthy: True = alles gut, False = Problem.
        """
        if condition not in CONDITIONS:
            LOGGER.debug("Unbekannte Notifier-Bedingung ignoriert: %s", condition)
            return
        if healthy:
            self._on_healthy(condition)
        else:
            self._on_problem(condition, detail)

    # Komfort-Wrapper
    def problem(self, condition: str, detail: str = "") -> None:
        self.report(condition, healthy=False, detail=detail)

    def healthy(self, condition: str) -> None:
        self.report(condition, healthy=True)

    def _on_problem(self, condition: str, detail: str) -> None:
        if self._problem.get(condition):
            return  # bereits im Problemzustand -> kein erneutes Mailen (Debounce)
        self._problem[condition] = True
        subject = CONDITIONS[condition][0]
        body = subject if not detail else f"{subject}\n\n{detail}"
        LOGGER.warning("Notifier: Problemzustand '%s' (%s)", condition, detail or "ohne Detail")
        self._emit(subject, body)

    def _on_healthy(self, condition: str) -> None:
        if not self._problem.get(condition):
            return  # war nicht im Problemzustand -> keine Recovery-Mail
        self._problem[condition] = False
        subject = CONDITIONS[condition][1]
        LOGGER.info("Notifier: Recovery '%s'", condition)
        self._emit(subject, subject)

    def _emit(self, subject: str, body: str) -> bool:
        """Verschickt Desktop-Notification + Mail (mit begrenztem Retry). True, wenn Mail zugestellt."""
        cfg = self._settings_provider()
        if self._desktop_notify is not None:
            self._desktop_notify("evcc", subject)
        if not cfg.enabled or not cfg.recipient:
            LOGGER.debug("Mail nicht aktiv/kein Empfänger -> nur Desktop-Notification")
            return False
        sender = cfg.sender or cfg.recipient
        for attempt in range(1, _MAX_SEND_RETRIES + 1):
            ok = self._mailer(cfg.smtp_host, int(cfg.smtp_port), sender, cfg.recipient,
                              f"evcc: {subject}", body)
            if ok:
                return True
            LOGGER.warning("Mail-Versuch %d/%d fehlgeschlagen", attempt, _MAX_SEND_RETRIES)
            if attempt < _MAX_SEND_RETRIES:
                self._sleep(_RETRY_BACKOFF_SECONDS)
        LOGGER.error("Mail endgültig nicht zustellbar: %s", subject)
        return False
