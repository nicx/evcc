# evcc

Native macOS-Menüleisten-App zur **terminalfreien** Verwaltung einer manuell installierten
evcc-Instanz: Install, Start/Stop/Restart, Updates mit Rollback, automatische SQLite-Backups,
Log-Einsicht und E-Mail-Alarm bei Problemen.

Stack: Python · PyObjC/AppKit · py2app · launchd · SQLite · MailRelay. Die Menüleisten-UI
(Status-Item, Menü, Timer, Dialoge, Notifications) ist durchgängig nativ — ohne `rumps`.
Notifier-, Config-/Keychain-, LaunchAgent- und Icon-Bausteine sind aus dem Schwesterprojekt
`icloud-sync` portiert. Vollständige Spezifikation: [evcc-app-spec.md](evcc-app-spec.md).

## Architektur in Kürze

- Das **evcc-Binary** läuft als launchd-**LaunchAgent** (`io.evcc.agent`), nicht als
  Kind der App — läuft also bei App-Quit/-Crash weiter und startet beim Login.
- Die Menüleisten-App ist reines Steuer-/Dashboard-Frontend und kontrolliert den Agenten
  über `launchctl` (`bootstrap`/`bootout`/`kickstart`).
- DB-Pfad und Loglevel werden als **verifizierte CLI-Flags** ins Plist geschrieben
  (`--database <pfad> --log <level>`).
- Fehler-/Recovery-Mails laufen über das lokale **MailRelay** (`127.0.0.1:2525`); eine
  State-Machine verhindert Dauerfeuer (Mail nur bei Zustandswechsel).

## Dateien (App-Daten)

| Zweck | Pfad |
|---|---|
| Binary / Vorgänger | `~/Library/Application Support/evcc/bin/evcc[.previous]` |
| SQLite-DB | `~/Library/Application Support/evcc/evcc.db` |
| App-Config | `~/Library/Application Support/evcc/config.json` |
| evcc-Logfile | `~/Library/Logs/evcc/evcc.log` |
| Agent-Plist | `~/Library/LaunchAgents/io.evcc.agent.plist` |

## Entwicklung

```bash
# Einmalig: virtuelle Umgebung + Abhängigkeiten
/opt/homebrew/bin/python3.13 -m venv .venv
.venv/bin/pip install -r requirements.txt

# Menüleisten-App im Dev-Modus starten (kein .app-Bundle)
.venv/bin/python -m src.app

# Tests (mock-frei, kein Netz/Account)
.venv/bin/python tests/test_backup.py
.venv/bin/python tests/test_updater.py
.venv/bin/python tests/test_notifier_state.py
```

## Build (.app)

```bash
.venv/bin/pip install -r requirements-build.txt
bash build/build.sh          # py2app-Build + ad-hoc-Signierung + Verify
# Ergebnis: dist/evcc.app
```

**Erststart:** Da ad-hoc signiert (Privatgebrauch, keine Notarisierung) → einmal per
Rechtsklick → „Öffnen" bestätigen (Gatekeeper). Heruntergeladene evcc-Binaries werden von
der App automatisch von der Quarantäne befreit (`xattr -d com.apple.quarantine`).

## Sicherheit

- Secrets gehören in den macOS-Keychain (`src/auth/keychain.py`), nicht in `config.json`.
  Aktuell verschickt der Notifier ohne Auth über das lokale Relay, daher kein SMTP-Passwort.
- evcc-Binaries: Quarantäne entfernen, optional SHA256 verifizieren.
