"""Menüleisten-App (PyObjC/AppKit): Entrypoint, Menüaufbau, Scheduler, Event-Dispatch.

Diese Schicht hält **keine** evcc-Logik — sie zeigt Status, steuert den launchd-Agenten
über :mod:`src.lifecycle`, stößt Backups/Updates an und blockiert das UI nie (alles
Langlaufende läuft im Hintergrund-Thread).

Die UI besteht ausschließlich aus PyObjC-Bausteinen (:mod:`src.statusitem`,
:mod:`src.timers`, :mod:`src.dialogs`, :mod:`src.settings_window`) — ``rumps`` ist
vollständig abgelöst.

Start (Entwicklung, ohne .app-Bundle)::

    .venv/bin/python -m src.app
"""

from __future__ import annotations

import logging
import logging.handlers
import os
import plistlib
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Optional

from . import (autostart, backup, dialogs, health, lifecycle, logs, menubar_icon, notify,
               paths, statusitem, timers, updater)
from .config.settings import Settings, load_settings, save_settings
from .notifier_state import NotifierState
from .statusitem import SEPARATOR, MenuEntry

LOGGER = logging.getLogger(__name__)

# UI-Refresh-Rate (Sekunden) für Icon/Menü-Updates aus Hintergrund-Threads.
UI_TICK_SECONDS = 1.0

# Statuszeile je Zustand (Symbol + Text).
_STATE_LINE = {
    health.RUNNING: "● evcc — läuft",
    health.STOPPED: "○ evcc — gestoppt",
    health.UNREACHABLE: "● evcc — nicht erreichbar",
}


class EvccApp:
    """Menüleisten-Resident zur terminalfreien Verwaltung einer evcc-Instanz."""

    def __init__(self) -> None:
        self.settings: Settings = load_settings()
        self._op_lock = threading.Lock()  # serialisiert Start/Stop/Update/Backup
        self._state: str = health.STOPPED
        self._needs_rebuild = False
        self._has_icon = False
        self._update_available = False
        self._latest_release: Optional[dict] = None
        self._installed_version: Optional[str] = None

        self.notifier = NotifierState(lambda: self.settings.notifications)
        self._health = health.HealthMonitor(
            self.settings.health.url, self.settings.health.failure_threshold)
        self._optimizer = health.OptimizerMonitor(self.settings.health.url)
        self._backup_scheduler = backup.BackupScheduler(
            backup.schedule_to_seconds(self.settings.backup.schedule), self._scheduled_backup)

        self._status = statusitem.StatusItem()
        self._setup_menubar_icon()
        self._rebuild_menu()

        # Health-Poll-Timer (Intervall aus Settings) + schneller UI-Refresh-Timer.
        self.health_timer = timers.RepeatingTimer(
            max(5, self.settings.health.interval_seconds), self._health_tick)
        self.health_timer.start()
        self.ui_timer = timers.RepeatingTimer(UI_TICK_SECONDS, self._ui_tick)
        self.ui_timer.start()
        self._backup_scheduler.start()
        # Periodischer Update-Check (Default täglich; 0 h = aus).
        self.update_timer = timers.RepeatingTimer(3600, self._update_tick)
        self._apply_update_timer()

        # Beim Start einmal Zustand prüfen und (falls aktiviert) auf Updates schauen.
        self._spawn(self._do_health)
        if self.settings.updates.check_on_launch:
            self._spawn(self._do_check_update)

    def _apply_update_timer(self) -> None:
        """(Re-)Konfiguriert den periodischen Update-Check-Timer aus den Settings."""
        self.update_timer.stop()
        hours = self.settings.updates.check_interval_hours
        if hours and hours > 0:
            self.update_timer.interval = hours * 3600
            self.update_timer.start()
            LOGGER.info("Periodischer Update-Check: alle %d h", hours)
        else:
            LOGGER.info("Periodischer Update-Check deaktiviert (0 h).")

    def _update_tick(self) -> None:
        self._spawn(self._do_check_update)

    # -- Menüaufbau ----------------------------------------------------------

    def _menu_spec(self) -> list:
        """Beschreibt das gesamte Menü als reine Daten (Struktur gem. Spec §6)."""
        items: list = []

        # Info-Zeilen ohne Callback sind automatisch deaktiviert.
        items.append(MenuEntry(_STATE_LINE.get(self._state, "evcc")))
        if self._installed_version:
            items.append(MenuEntry(f"Version: {self._installed_version}"))
        items.append(SEPARATOR)

        items.append(MenuEntry("Web-UI öffnen", self._open_webui))
        items.append(SEPARATOR)

        items.append(MenuEntry("Start", self._start))
        items.append(MenuEntry("Stop", self._stop))
        items.append(MenuEntry("Neu starten", self._restart))
        items.append(SEPARATOR)

        items.append(MenuEntry("Auf Update prüfen…", self._check_update))
        if self._update_available:
            version = updater.release_version(self._latest_release) or "neu"
            items.append(MenuEntry(f"Update installieren… ({version})", self._install_update))
        else:
            items.append(MenuEntry("Update installieren…"))  # nur aktiv bei neuerer Version
        items.append(MenuEntry("Rollback auf vorherige Version…", self._rollback))
        items.append(SEPARATOR)

        items.append(MenuEntry("Backup jetzt", self._backup_now))
        # Log-Level liegt im Settings-Fenster; hier nur noch Log-Aktionen.
        items.append(MenuEntry("Logs", children=[
            MenuEntry("Log öffnen (Console)", self._open_log_console),
            MenuEntry("Letzte 50 Zeilen", self._show_last_lines),
        ]))
        items.append(SEPARATOR)

        items.append(MenuEntry("Einstellungen…", self._open_settings))
        items.append(MenuEntry("Beim Login starten", self._toggle_autostart,
                               checked=autostart.is_enabled()))
        items.append(SEPARATOR)
        items.append(MenuEntry("Beenden", self._quit))
        return items

    def _rebuild_menu(self) -> None:
        """Baut das gesamte Menü neu auf."""
        self._status.set_menu(self._menu_spec())
        self._update_icon()

    def _setup_menubar_icon(self) -> None:
        """Rendert die zwei Status-Icons (gefüllt=läuft, Outline=gestoppt) und setzt das Start-Icon."""
        icons = menubar_icon.ensure_menubar_icons()
        self._icon_running = icons.get("running")
        self._icon_stopped = icons.get("stopped")
        self._has_icon = bool(self._icon_running and self._icon_stopped)
        if self._has_icon:
            self._status.set_title("")
            self._status.set_icon(self._icon_stopped)  # bis der erste Healthcheck greift
        else:
            self._status.set_title("evcc")

    def _update_icon(self) -> None:
        problem = self._state == health.UNREACHABLE
        if self._has_icon:
            # Gefülltes Icon, wenn evcc läuft; sonst Outline. Bei „unreachable" zusätzlich
            # ein rotes Badge (Agent geladen, antwortet aber nicht).
            self._status.set_icon(
                self._icon_running if self._state == health.RUNNING else self._icon_stopped)
            self._status.set_title(" 🔴" if problem else "")
        else:
            self._status.set_title("evcc 🔴" if problem else "evcc")

    # -- Dialog-Helfer -------------------------------------------------------

    def _ask_text(self, message: str, title: str, default: str = "") -> Optional[str]:
        # Native NSAlert-Texteingabe (siehe :mod:`src.dialogs`).
        return dialogs.ask_text(title, message, default)

    def _ask_yes_no(self, message: str, title: str) -> bool:
        return dialogs.ask_yes_no(title, message)

    def _ask_directory(self, message: str, default: Optional[str] = None) -> Optional[str]:
        try:
            return self._native_directory_dialog(message, default)
        except Exception:  # noqa: BLE001 - im Zweifel nie blockieren
            LOGGER.exception("NSOpenPanel nicht verfügbar, Fallback auf Texteingabe")
            return self._ask_text(message, "Ordner wählen", default=default or "") or None

    @staticmethod
    def _native_directory_dialog(message: str, default_path: Optional[str]) -> Optional[str]:
        from AppKit import NSApp, NSOpenPanel
        from Foundation import NSURL

        panel = NSOpenPanel.openPanel()
        panel.setCanChooseFiles_(False)
        panel.setCanChooseDirectories_(True)
        panel.setAllowsMultipleSelection_(False)
        panel.setCanCreateDirectories_(True)
        panel.setPrompt_("Auswählen")
        panel.setMessage_(message)
        if default_path:
            panel.setDirectoryURL_(NSURL.fileURLWithPath_(default_path))
        NSApp.activateIgnoringOtherApps_(True)
        if panel.runModal() != 1:  # 1 == NSModalResponseOK
            return None
        urls = panel.URLs()
        return urls[0].path() if urls else None

    # -- Lifecycle-Aktionen --------------------------------------------------

    def _open_webui(self) -> None:
        try:
            subprocess.run(["open", self.settings.health.url], check=False, timeout=10)
        except (OSError, subprocess.SubprocessError) as exc:
            dialogs.alert("Web-UI", f"Konnte {self.settings.health.url} nicht öffnen: {exc}")

    def _start(self) -> None:
        self._spawn(self._do_start)

    def _stop(self) -> None:
        self._spawn(self._do_stop)

    def _restart(self) -> None:
        self._spawn(self._do_restart)

    def _do_start(self) -> None:
        with self._op_lock:
            if not paths.evcc_binary().exists():
                notify.notify("evcc", "Kein Binary vorhanden – lade aktuelles Release…")
                if not self._install_latest_binary():
                    return
            lifecycle.write_agent_plist(self.settings.logging.level)
            if lifecycle.bootstrap():
                notify.notify("evcc", "Agent gestartet.")
            else:
                notify.notify("evcc", "Agent-Start fehlgeschlagen (siehe Log).")
        self._spawn(self._do_health)

    def _do_stop(self) -> None:
        with self._op_lock:
            lifecycle.bootout()
            notify.notify("evcc", "Agent gestoppt.")
        self._spawn(self._do_health)

    def _do_restart(self) -> None:
        with self._op_lock:
            if not lifecycle.is_loaded():
                lifecycle.write_agent_plist(self.settings.logging.level)
                lifecycle.bootstrap()
            else:
                lifecycle.kickstart()
            notify.notify("evcc", "Agent neu gestartet.")
        self._spawn(self._do_health)

    def _install_latest_binary(self) -> bool:
        """Lädt das jüngste Release-Binary und installiert es. True bei Erfolg."""
        try:
            # Immer frisch holen: das beim Check gecachte Release kann noch ohne Assets sein
            # (GitHub lädt sie erst nach dem Veröffentlichen hoch) → sonst Dauerfehler.
            release = updater.fetch_latest_release()
            self._latest_release = release
            asset = updater.select_asset(release)
            if asset is None:
                self.notifier.problem("update_failed", "Kein macOS-Asset im Release gefunden.")
                return False
            tar = paths.bin_dir() / asset["name"]
            lifecycle.download_file(asset["browser_download_url"], tar)
            if not self._verify_asset(tar, asset):
                tar.unlink(missing_ok=True)
                return False
            lifecycle.extract_evcc_from_tar(tar, paths.evcc_binary())
            tar.unlink(missing_ok=True)
            self._installed_version = updater.installed_version(paths.evcc_binary())
            self.notifier.healthy("update_failed")
            self._needs_rebuild = True
            return True
        except Exception as exc:  # noqa: BLE001
            LOGGER.exception("Binary-Installation fehlgeschlagen")
            self.notifier.problem("update_failed", f"Installation fehlgeschlagen: {exc}")
            return False

    def _verify_asset(self, tar: Path, asset: dict) -> bool:
        """Prüft die SHA256-Checksumme des Assets (GitHub liefert sie als ``digest``).

        Bei aktivierter Prüfung **fail-closed**: fehlt ein verwertbarer ``sha256:``-Digest,
        wird der Download abgelehnt — sonst könnte ein manipuliertes API-Response die
        Integritätsprüfung durch Weglassen des Digests einfach aushebeln.
        """
        if not self.settings.updates.verify_checksum:
            return True
        digest = asset.get("digest") or ""
        if not digest.startswith("sha256:"):
            self.notifier.problem(
                "update_failed",
                "Checksum-Prüfung aktiv, aber kein SHA256-Digest am Asset – Download abgelehnt.")
            return False
        if updater.verify_sha256(tar, digest.split(":", 1)[1]):
            return True
        self.notifier.problem("update_failed", "SHA256-Checksumme stimmt nicht.")
        return False

    # -- Updates -------------------------------------------------------------

    def _check_update(self) -> None:
        self._spawn(self._do_check_update)

    def _do_check_update(self) -> None:
        try:
            release = updater.fetch_latest_release()
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("Update-Check fehlgeschlagen: %s", exc)
            notify.notify("evcc", f"Update-Check fehlgeschlagen: {exc}")
            return
        self._latest_release = release
        self._installed_version = updater.installed_version(paths.evcc_binary())
        latest = updater.release_version(release)
        newer = updater.is_newer(latest, self._installed_version)
        # Neues Release ohne macOS-Asset (Upload läuft noch) gilt noch nicht als verfügbar —
        # weder im Menü anbieten noch mailen; der nächste Check holt es nach.
        self._update_available = newer and updater.select_asset(release) is not None
        self._needs_rebuild = True
        if newer and not self._update_available:
            LOGGER.info("Release %s noch ohne macOS-Asset – wird beim nächsten Check erneut geprüft", latest)
            return
        if not self._update_available:
            notify.notify("evcc", f"evcc ist aktuell ({self._installed_version or '—'}).")
            return
        # Update verfügbar: einmal pro Version per Mail melden, danach Stille.
        if self.settings.updates.notify_email and latest != self.settings.updates.last_notified_version:
            mailed = self.notifier.notify_event(
                f"Update verfügbar: {latest}",
                "Eine neue evcc-Version steht bereit.\n\n"
                f"Verfügbar:   {latest}\n"
                f"Installiert: {self._installed_version or '—'}\n"
                f"Changelog:   {updater.release_url(release)}\n\n"
                "Installation über das Menü: 'Update installieren…'.")
            # Version nur dann als gemeldet merken, wenn die Mail wirklich rausging — sonst
            # bei späterer Mail-Aktivierung erneut versuchen.
            if mailed:
                self.settings.updates.last_notified_version = latest or ""
                save_settings(self.settings)
        else:
            notify.notify("evcc", f"Update verfügbar: {latest} (installiert: {self._installed_version or '—'})")

    def _install_update(self) -> None:
        if not self._update_available:
            return
        self._spawn(self._do_install_update)

    def _do_install_update(self) -> None:
        # Vorab prüfen: GitHub veröffentlicht das Release oft Minuten VOR den Dateien. Dann
        # weder evcc stoppen noch Fehlermail senden, sondern später erneut versuchen lassen.
        try:
            release = updater.fetch_latest_release()
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("Update nicht möglich, Release nicht abrufbar: %s", exc)
            notify.notify("evcc", f"Update nicht möglich: {exc}")
            return
        self._latest_release = release
        if updater.select_asset(release) is None:
            LOGGER.info("Release %s hat noch kein macOS-Asset – Update verschoben",
                        updater.release_version(release))
            notify.notify("evcc", "Das neue Release ist noch nicht vollständig (macOS-Datei "
                                  "fehlt). Bitte in ein paar Minuten erneut versuchen.")
            return
        with self._op_lock:
            # 1) Backup erzwingen (nur, wenn Ziel konfiguriert/erreichbar).
            if self.settings.backup.target_path:
                self._run_backup(announce=False)
            # 2) Agent stoppen, neues Binary holen, Vorgänger sichern, ersetzen.
            lifecycle.bootout()
            lifecycle.save_previous()
            if not self._install_latest_binary():
                # Installation gescheitert -> Vorgänger zurück und wieder starten.
                lifecycle.rollback()
                lifecycle.write_agent_plist(self.settings.logging.level)
                lifecycle.bootstrap()
                return
            # 3) Agent wieder starten + Healthcheck.
            lifecycle.write_agent_plist(self.settings.logging.level)
            lifecycle.bootstrap()
        time.sleep(3)
        state = health.probe(self.settings.health.url)
        if state == health.RUNNING:
            self._update_available = False
            self.notifier.healthy("update_failed")
            notify.notify("evcc", f"Update installiert: {self._installed_version or 'neu'}.")
        else:
            self.notifier.problem("update_failed", "Healthcheck nach Update rot – evtl. Rollback nötig.")
        self._spawn(self._do_health)

    def _rollback(self) -> None:
        if not paths.evcc_binary_previous().exists():
            dialogs.alert("Rollback", "Kein Vorgänger-Binary vorhanden.")
            return
        if not self._ask_yes_no(
                "Auf vorherige evcc-Version zurückrollen?\n\nHinweis: funktioniert nur bei "
                "kompatibler DB. Nach einem Schema-Upgrade ggf. zusätzlich das DB-Backup "
                "zurückspielen.", "Rollback"):
            return
        self._spawn(self._do_rollback)

    def _do_rollback(self) -> None:
        with self._op_lock:
            lifecycle.bootout()
            ok = lifecycle.rollback()
            lifecycle.write_agent_plist(self.settings.logging.level)
            lifecycle.bootstrap()
        self._installed_version = updater.installed_version(paths.evcc_binary())
        self._needs_rebuild = True
        notify.notify("evcc", "Rollback durchgeführt." if ok else "Rollback fehlgeschlagen.")
        self._spawn(self._do_health)

    # -- Backup --------------------------------------------------------------

    def _backup_now(self) -> None:
        self._spawn(lambda: self._run_backup(announce=True))

    def _scheduled_backup(self) -> None:
        if self.settings.backup.target_path:
            self._run_backup(announce=False)

    def _run_backup(self, announce: bool) -> bool:
        cfg = self.settings.backup
        if not cfg.target_path:
            if announce:
                dialogs.alert("Backup", "Kein Backup-Ziel gesetzt. Bitte in den Einstellungen "
                                        "ein Backup-Ziel wählen.")
            return False
        try:
            target = backup.hot_backup(paths.db_file(), Path(cfg.target_path), paths.evcc_yaml())
            removed = backup.apply_retention(Path(cfg.target_path), cfg.retention)
            self.notifier.healthy("backup_failed")
            if announce:
                notify.notify("evcc", f"Backup erstellt: {target.name}"
                                       + (f" ({len(removed)} alte entfernt)" if removed else ""))
            return True
        except backup.BackupError as exc:
            self.notifier.problem("backup_failed", str(exc))
            if announce:
                dialogs.alert("Backup fehlgeschlagen", str(exc))
            return False

    # -- Logs ----------------------------------------------------------------

    def _open_log_console(self) -> None:
        logs.open_in_console(paths.evcc_log_file())

    def _show_last_lines(self) -> None:
        text = logs.tail(paths.evcc_log_file(), 50) or "(Logfile noch leer oder nicht vorhanden.)"
        dialogs.show_text("evcc – letzte 50 Zeilen", text)

    def _apply_log_level(self) -> None:
        with self._op_lock:
            if lifecycle.is_loaded():
                lifecycle.write_agent_plist(self.settings.logging.level)
                lifecycle.kickstart()
        self._needs_rebuild = True
        notify.notify("evcc", f"Log-Level: {self.settings.logging.level}")

    # -- Einstellungen -------------------------------------------------------

    def _open_settings(self) -> None:
        """Öffnet das native Settings-Fenster; Fallback auf die Dialogkette bei AppKit-Problemen."""
        from . import settings_window

        old_level = self.settings.logging.level
        try:
            edited = settings_window.run_settings_window(
                self.settings, lambda msg, d: self._ask_directory(msg, d))
        except Exception:  # noqa: BLE001 - im Zweifel nie blockieren
            LOGGER.exception("Settings-Fenster nicht verfügbar, Fallback auf Dialogkette")
            return self._open_settings_legacy()
        if edited is None:
            return  # Abbrechen: nichts geändert (kein In-place-Leak wie in der alten Kette)
        self.settings = edited
        save_settings(self.settings)
        self._apply_settings()
        if self.settings.logging.level != old_level:
            self._spawn(self._apply_log_level)  # nur bei Änderung; threaded wie die Aktionen
        self._needs_rebuild = True
        notify.notify("evcc", "Einstellungen gespeichert.")

    def _open_settings_legacy(self) -> None:
        """Fallback: sequenzielle Dialoge für Intervalle, Retention und Fehler-Mail (Abbruch beendet)."""
        s = self.settings
        val = self._ask_text("Backup-Zeitplan (hourly / daily / weekly):", "Einstellungen",
                             default=s.backup.schedule)
        if val is None:
            return
        if val in backup._SCHEDULE_HOURS:
            s.backup.schedule = val

        val = self._ask_text("Backups behalten (Anzahl):", "Einstellungen",
                             default=str(s.backup.retention))
        if val is None:
            return
        try:
            s.backup.retention = max(1, int(val))
        except ValueError:
            pass

        val = self._ask_text("Healthcheck-Intervall (Sekunden):", "Einstellungen",
                             default=str(s.health.interval_seconds))
        if val is None:
            return
        try:
            s.health.interval_seconds = max(5, int(val))
        except ValueError:
            pass

        val = self._ask_text("Update-Check-Intervall (Stunden, 0 = aus):", "Einstellungen",
                             default=str(s.updates.check_interval_hours))
        if val is None:
            return
        try:
            s.updates.check_interval_hours = max(0, int(val))
        except ValueError:
            pass

        enable_mail = self._ask_yes_no("Fehler-E-Mail über lokales MailRelay aktivieren?", "Einstellungen")
        s.notifications.enabled = enable_mail
        if enable_mail:
            rcpt = self._ask_text("Empfänger-Adresse:", "Fehler-E-Mail",
                                  default=s.notifications.recipient)
            if rcpt is not None:
                s.notifications.recipient = rcpt
            host = self._ask_text("Relay-Host:", "Fehler-E-Mail", default=s.notifications.smtp_host)
            if host:
                s.notifications.smtp_host = host
            port = self._ask_text("Relay-Port:", "Fehler-E-Mail", default=str(s.notifications.smtp_port))
            if port:
                try:
                    s.notifications.smtp_port = int(port)
                except ValueError:
                    pass

        save_settings(s)
        self._apply_settings()
        notify.notify("evcc", "Einstellungen gespeichert.")

    def _apply_settings(self) -> None:
        """Übernimmt geänderte Settings in die laufenden Timer/Monitore."""
        self._health = health.HealthMonitor(
            self.settings.health.url, self.settings.health.failure_threshold)
        self.health_timer.interval = max(5, self.settings.health.interval_seconds)
        self._backup_scheduler.stop()
        self._backup_scheduler = backup.BackupScheduler(
            backup.schedule_to_seconds(self.settings.backup.schedule), self._scheduled_backup)
        self._backup_scheduler.start()
        self._apply_update_timer()
        self._needs_rebuild = True

    # -- Autostart der GUI-App ----------------------------------------------

    def _toggle_autostart(self) -> None:
        """Login-Autostart der Menüleisten-App selbst (getrennt vom evcc-Agenten)."""
        if autostart.is_enabled():
            autostart.disable()
        else:
            args = self._autostart_program_args()
            if args is None:
                dialogs.alert(
                    "Autostart nur im .app-Bundle",
                    "Der Login-Autostart funktioniert nur für die gebaute App-Bundle-Version. "
                    "Im Entwicklungsmodus (python -m src.app) ist er nicht verfügbar.")
                return
            autostart.enable(args)
        self._needs_rebuild = True  # Häkchen über den regulären Menü-Neuaufbau nachziehen

    @staticmethod
    def _autostart_program_args() -> Optional[list[str]]:
        """Programmargumente für den LaunchAgent – nur sinnvoll im gebauten Bundle.

        py2app setzt ``sys.frozen``. ``sys.executable`` zeigt im Bundle auf den eingebetteten
        Interpreter, NICHT auf den Loader-Stub (``CFBundleExecutable``). Daher den echten
        Bundle-Executable auflösen (Stub triggert ``__boot__`` → launcher.py → main()).
        """
        if not getattr(sys, "frozen", False):
            return None
        macos_dir = os.path.dirname(sys.executable)              # …/Contents/MacOS
        bundle = os.path.dirname(os.path.dirname(macos_dir))     # …/evcc.app
        exe_name = os.path.splitext(os.path.basename(bundle))[0]
        try:
            with open(os.path.join(bundle, "Contents", "Info.plist"), "rb") as fh:
                exe_name = plistlib.load(fh).get("CFBundleExecutable") or exe_name
        except (OSError, plistlib.InvalidFileException):
            pass
        return [os.path.join(macos_dir, exe_name)]

    def _quit(self) -> None:
        import AppKit

        self._backup_scheduler.stop()
        self.health_timer.stop()
        self.ui_timer.stop()
        self.update_timer.stop()
        AppKit.NSApplication.sharedApplication().terminate_(None)

    # -- Health / Timer ------------------------------------------------------

    def _health_tick(self) -> None:
        self._spawn(self._do_health)

    def _do_health(self) -> None:
        """Ermittelt den Zustand (Hintergrund) und speist Notifier-State-Machine + Icon."""
        url = self.settings.health.url
        if not lifecycle.is_loaded():
            self._state = health.STOPPED
        else:
            state = self._health.check()
            self._state = state
            if self._health.is_problem:
                self.notifier.problem(
                    "evcc_unreachable",
                    f"{self._health.consecutive_failures} fehlgeschlagene Polls an {url}.")
            else:
                self.notifier.healthy("evcc_unreachable")
            if state == health.RUNNING:
                self._check_optimizer(url)
        # App-seitige Logrotation (launchd rotiert nicht).
        if logs.rotate_if_needed(paths.evcc_log_file(), self.settings.logging.max_size_mb):
            if lifecycle.is_loaded():
                lifecycle.kickstart()  # Agent öffnet die (frische) Logdatei neu
        self._needs_rebuild = True

    def _check_optimizer(self, url: str) -> None:
        """Meldet dauerhaft ``Infeasible``-Läufe des evcc-Optimizers (siehe OptimizerMonitor)."""
        if self._optimizer.base_url != url:
            self._optimizer = health.OptimizerMonitor(url)
        self._optimizer.check()
        if self._optimizer.is_problem:
            self.notifier.problem(
                "optimizer_infeasible",
                f"{self._optimizer.consecutive_failures} Optimizer-Läufe in Folge ohne Lösung. "
                "Üblich: Ausreißerzeile in evcc.db (Tabelle meters, Spalte energy) verfälscht "
                "das Verbrauchsprofil; Zeile prüfen/löschen (siehe evcc-Issue #34039).")
        elif self._optimizer.consecutive_failures == 0:
            self.notifier.healthy("optimizer_infeasible")

    def _ui_tick(self) -> None:
        """Schneller UI-Refresh auf dem Main-Thread (Icon + ggf. Menü-Neuaufbau)."""
        self._update_icon()
        if self._needs_rebuild:
            self._needs_rebuild = False
            self._rebuild_menu()

    # -- Util ----------------------------------------------------------------

    @staticmethod
    def _spawn(fn) -> None:
        """Startet ``fn`` in einem Daemon-Thread; fängt+loggt unerwartete Fehler."""
        def _runner():
            try:
                fn()
            except Exception:  # noqa: BLE001 - Thread darf nie lautlos sterben
                LOGGER.exception("Hintergrund-Task abgebrochen: %r", getattr(fn, "__name__", fn))

        threading.Thread(target=_runner, daemon=True).start()


def _setup_logging() -> None:
    """Root-Logger auf eine rotierende Datei (App-eigenes Log) + stderr konfigurieren.

    In der ``.app`` (Menüleisten-App ohne Terminal) ist stderr verloren — die Datei ist die
    einzige verlässliche Diagnosequelle. Idempotent.
    """
    root = logging.getLogger()
    if any(getattr(h, "_evcc", False) for h in root.handlers):
        return
    root.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        fh = logging.handlers.RotatingFileHandler(
            paths.app_log_file(), maxBytes=1_000_000, backupCount=5, encoding="utf-8")
        fh.setFormatter(fmt)
        fh._evcc = True  # type: ignore[attr-defined]
        root.addHandler(fh)
    except OSError:
        pass

    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    sh._evcc = True  # type: ignore[attr-defined]
    root.addHandler(sh)

    def _log_uncaught(exc_type, exc, tb):
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc, tb)
            return
        logging.getLogger("uncaught").error("Unbehandelte Exception", exc_info=(exc_type, exc, tb))

    sys.excepthook = _log_uncaught


def main() -> None:
    """Startet die Menüleisten-App und übergibt an den AppKit-Runloop."""
    import AppKit

    _setup_logging()
    LOGGER.info("evcc startet (Log: %s)", paths.app_log_file())

    ns_app = AppKit.NSApplication.sharedApplication()
    # „Accessory": Menüleisten-Resident ohne Dock-Icon und ohne Menüleisten-Hauptmenü.
    # Entspricht LSUIElement aus der Info.plist, gilt aber auch im Dev-Modus ohne Bundle.
    ns_app.setActivationPolicy_(AppKit.NSApplicationActivationPolicyAccessory)

    app = EvccApp()  # Referenz halten: Status-Item und Timer hängen daran
    LOGGER.info("Menüleisten-Item aktiv, Runloop startet.")
    ns_app.run()
    LOGGER.info("Runloop beendet (%r).", app.__class__.__name__)


if __name__ == "__main__":
    main()
