# CLAUDE.md — Projektkontext evcc

Native macOS-**Menüleisten-App** zur terminalfreien Verwaltung einer manuell installierten
**evcc**-Instanz. Diese Datei fasst Architektur, Entscheidungen und Stolperfallen zusammen,
damit eine frische Claude-Session (auch auf einem anderen Mac) sofort produktiv ist.

- **Repo:** https://github.com/nicx/evcc (public)
- **Spec (Original-Handoff):** [evcc-app-spec.md](evcc-app-spec.md)
- **Detaillierter Umsetzungsplan:** [docs/PLAN.md](docs/PLAN.md)
- **Stack:** Python 3.13 · **PyObjC/AppKit** · py2app · launchd · SQLite · lokales MailRelay
- **Herkunft:** Bausteine (notify, keychain, settings, autostart, menubar_icon, py2app-Skelett)
  sind aus dem Schwesterprojekt `icloud-sync` **portiert**, nicht neu gebaut.
- **UI-Toolkit:** die Menüleisten-UI ist vollständig PyObjC; `rumps` (und der
  `pync`-Notification-Fallback) sind seit der Umstellung **entfernt** — siehe
  „Richtung: PyObjC" am Ende.

## Architektur

- Das **evcc-Binary** läuft als launchd-**LaunchAgent** `io.evcc.agent` (nicht als Kind
  der App) → läuft bei App-Quit/-Crash weiter, Autostart bei Login, `KeepAlive=true`.
- Die App ist reines **Steuer-/Dashboard-Frontend** und kontrolliert den Agenten via
  `launchctl` (`bootstrap` / `bootout` / `kickstart -k`, Legacy `load/unload -w` als Fallback).
- DB-Pfad & Loglevel werden als **CLI-Flags ins Plist** geschrieben
  (`--database <pfad> --log <level>`), nicht über eine separate evcc.yaml.
- **2-Tier-Timer** in `app.py` (`timers.RepeatingTimer` = `NSTimer`): Health-Poll-Timer
  (Default 30 s) + 1-s-UI-Refresh-Timer; zusätzlich periodischer Update-Check-Timer
  (Default 24 h) und interner Backup-Scheduler.
- Langlaufendes läuft im Daemon-Thread (`_spawn`), serialisiert über `threading.Lock`.
- **Threading-Regel:** AppKit nur auf dem Main-Thread. Hintergrund-Threads setzen keine UI
  direkt, sondern gehen über `mainthread.run_on_main`/`post_to_main`; die UI-Schichten
  (`statusitem`, `timers`, `dialogs`, `notify`) marshallen bereits selbst.

### Modulübersicht (`src/`)
| Datei | Zweck |
|---|---|
| `app.py` | `EvccApp` + `NSApplication`-Runloop: Menü-Spezifikation (Spec §6), Timer, Threading, verdrahtet alle Module |
| `statusitem.py` | **NSStatusItem/NSMenu**: `MenuEntry`/`SEPARATOR` als reine Datenschicht (unit-testbar) + AppKit-Übersetzung; ein langlebiges `_MenuTarget` verteilt Klicks per `tag` |
| `timers.py` | **NSTimer**-Wrapper (`RepeatingTimer`) mit start/stop/änderbarem Intervall; Callback läuft auf dem Main-Thread |
| `mainthread.py` | Main-Thread-Marshalling (`run_on_main` synchron, `post_to_main` asynchron) — **eine** Quelle der Threading-Regel für alle UI-Module |
| `settings_window.py` | natives Settings-**Fenster** (PyObjC/`NSWindow`+`NSGridView`) + reine `build_settings`-Validierung |
| `dialogs.py` | native **NSAlert**-Dialoge (`alert`/`ask_yes_no`/`ask_text`/`show_text`) |
| `lifecycle.py` | Binary-Download/-Install, Tar-Extraktion, Quarantäne entfernen, Rollback, launchctl |
| `health.py` | HTTP-Poll `:7070` → `running|stopped|unreachable`, Fehler-Schwellwert |
| `backup.py` | `sqlite3.Connection.backup()` (WAL-sicher), Retention, `BackupScheduler` |
| `updater.py` | GitHub-Release-API, Versionsvergleich, Asset-Wahl, SHA256 |
| `logs.py` | Tail, Console.app öffnen, größenbasierte Rotation |
| `notify.py` | macOS-Notification via **UNUserNotificationCenter** + `send_mail` (klartext-SMTP an lokales Relay) |
| `notifier_state.py` | **State-Machine-Debounce**: Mail nur bei Zustandswechsel; `notify_event` für Update-Infos |
| `auth/keychain.py` | `keyring`-Wrapper (Service `evcc`) — aktuell ohne Pflicht-Consumer |
| `config/settings.py` | verschachtelte Settings-Dataclasses + JSON (`config.json`), tolerant geladen |
| `paths.py` | zentrale Pfade + Label-Konstanten |
| `menubar_icon.py` | rendert 2 Template-PNGs (filled=läuft / outline=gestoppt) |
| `autostart.py` | Login-Autostart der **GUI-App** (Label `de.nicx.evcc`, getrennt vom evcc-Agenten) |

### Laufzeit-Pfade
- Binary/Vorgänger: `~/Library/Application Support/evcc/bin/evcc[.previous]`
- DB: `~/Library/Application Support/evcc/evcc.db`
- App-Config: `~/Library/Application Support/evcc/config.json`
- evcc-Logfile: `~/Library/Logs/evcc/evcc.log`
- Agent-Plist: `~/Library/LaunchAgents/io.evcc.agent.plist`

## Wichtige Entscheidungen
- **Mail-Transport über lokales MailRelay** (`127.0.0.1:2525`, klartext-SMTP, kein Auth/TLS).
  Bewusste **Abweichung von Spec §3.6** (SMTP+SSL+Keychain) zugunsten Ökosystem-Konsistenz.
- **CLI-Flags statt evcc.yaml** für DB/Loglevel (deterministisch, keine zweite Konfigquelle).
- **Checksum fail-closed**: bei aktiver Prüfung ohne `sha256:`-Digest wird der Download abgelehnt.
- **Download nur über `https://`** (lifecycle.download_file erzwingt das Schema).

## Verifizierte evcc-Fakten (gegen Doku/echtes Release geprüft — nicht raten!)
- DB-Pfad: `--database <pfad>` · Loglevel: `-l/--log <level>` (`fatal|error|warn|info|debug|trace`).
- evcc.yaml-Äquivalent: `database: {type: sqlite, dsn: <pfad>}` bzw. global `log:` + `levels:`.
- Version auslesen: **`evcc -v`** funktioniert (liefert z. B. `0.309.0`).
- **Release-Asset heißt `evcc_<version>_macOS-all.tar.gz` mit BINDESTRICH** (`macOS-all`),
  nicht `macOS_all` wie in der Spec. Regex akzeptiert beide: `^evcc_.*_macOS[-_]all\.tar\.gz$`.

## Mail-Verhalten (wann kommt eine Mail?)
Nur wenn Fehler-Mail aktiviert + Empfänger gesetzt:
- **evcc_unreachable**: Agent geladen, antwortet `failure_threshold`× (Default 3) nicht → 1 Problem-Mail, später 1 Recovery.
- **optimizer_infeasible**: `/api/state` → `evopt.res.status` 3 verschiedene Läufe in Folge `Infeasible` (Lauf-Zeitstempel `evopt.updated`, Optimizer ~alle 15 min) → 1 Problem-Mail, `Feasible` → Recovery. Übliche Ursache: Ausreißerzeile in `evcc.db` `meters` (Zählerfehler) verfälscht das Verbrauchsprofil, siehe evcc-Issue #34039.
- **backup_failed** / **update_failed**: je 1 Problem + 1 Recovery.
- **Update verfügbar**: 1 Mail pro Version (dedupliziert über `updates.last_notified_version`).
- **Manueller Stop = KEINE Mail** (Agent ausgeladen → Zustand `stopped`).
- **Bewusst NICHT umgesetzt** (vom User abgelehnt): dedizierte Crash-Mail, `db_migration`-Trigger
  (Bedingung existiert, ist aber nicht verdrahtet). Kein App-Self-Update (v1 out of scope).

## Status-Icon-Konvention (analog matter-server/homeassistant)
gefülltes SF-Symbol `bolt.car.fill` = läuft · Outline `bolt.car` = gestoppt ·
Outline + rotes Badge `🔴` = nicht erreichbar. Alles Template-Images (auto-getönt hell/dunkel).

## Entwicklung & Build
```bash
# venv (einmalig)
/opt/homebrew/bin/python3.13 -m venv .venv
.venv/bin/pip install -r requirements.txt
# Dev-Run (Menüleisten-App ohne Bundle)
.venv/bin/python -m src.app
# Tests (mock-frei, kein Netz) — derzeit 67 grün
for t in tests/test_*.py; do .venv/bin/python "$t"; done
# Build der .app (py2app + Signierung + verify)
.venv/bin/pip install -r requirements-build.txt
CODESIGN_IDENTITY="nicx Selfsign" bash build/build.sh   # → dist/evcc.app
```
Erststart der gebauten App: Rechtsklick → „Öffnen" (nicht notarisiert → Gatekeeper).

### Signierung: immer mit stabiler Identität bauen
`build/build.sh` signiert per Default **ad-hoc** (`-`), damit der Build auch ohne Zertifikat
läuft. Ad-hoc heißt aber: die Code-Identität wechselt bei **jedem** Rebuild → macOS erkennt die
App nicht wieder und vergisst erteilte Berechtigungen (**Mitteilungen**, Gatekeeper).

Daher lokal immer `CODESIGN_IDENTITY="nicx Selfsign"` setzen — ein selbstsigniertes
Code-Signing-Zertifikat (angelegt 2026-08-17, gültig bis 2036, Trust-Policy „Code Signing" in
der User-Domain). Es ist **projektübergreifend** gedacht: dieselbe Identität für alle
nicx-Menüleisten-Apps. Prüfen mit `security find-identity -v -p codesigning`; die Signatur
eines Bundles zeigt `codesign -dv --verbose=2 dist/evcc.app` (erwartet:
`Authority=nicx Selfsign`, `flags=0x0(none)` — **nicht** `0x2(adhoc)`).

Eine Team-ID hat die Identität nicht (kein bezahlter Developer-Account), daher bleibt sie auf
**Gatekeeper + TCC/Mitteilungen** beschränkt; für Verteilung an Dritte bräuchte es Developer ID
+ Notarisierung.
Build/dist/venv/Logs/DB sind via `.gitignore` ausgeschlossen.

## Konventionen
- Code-Kommentare/Docstrings auf Deutsch (wie Bestand).
- Best-effort-Fehlerbehandlung in I/O/Subprozessen (loggen statt werfen), damit UI/Betrieb
  nie kippt — siehe vorhandene `try/except`-Muster.
- Bei Änderungen: Tests grün halten + `bash build/build.sh` + Boot-Smoke
  (`open dist/evcc.app`, Prozess prüfen, beenden).

## Gotchas
- **py2app + `charset_normalizer`:** dessen mypyc-kompilierte `*__mypyc*.so` liegt im
  site-packages-Root und wird von py2app sonst nicht mitgenommen — `build/setup.py` kopiert
  sie explizit auf den Bundle-Python-Pfad. Bei „ModuleNotFoundError" im gebauten Bundle
  hier zuerst schauen.
- **Rollback nur bei kompatibler DB:** ein Binary-Downgrade nach einem Schema-Upgrade kann
  scheitern → im Zweifel zusätzlich das DB-Backup zurückspielen (im UI so kommuniziert).
- **Binary-Ersetzung immer per `rename`, nie in-place** (`lifecycle._replace_binary_atomically`,
  genutzt von `save_previous`/`rollback`; `download_file`/`extract_evcc_from_tar` machten es
  schon richtig). Beobachtet am 2026-09-15: ein automatischer Update-Check scheiterte, weil
  GitHub das Release-Tag `0.315.1` schon führte, die Asset-Dateien aber noch nicht hochgeladen
  waren (`select_asset` → `None`) — danach lief planmäßig `rollback()`, das damals noch
  `shutil.copy2()` **direkt auf das aktive Binary** schrieb. Der Agent crashte danach bei
  jedem Start mit `SIGKILL (Code Signature Invalid)` (Crash-Report:
  `termination.indicator: "Taskgated Invalid Signature"`), obwohl `codesign --verify` die
  Datei als gültig meldete und ihr Hash exakt dem funktionierenden `evcc.previous` entsprach.
  Ursache: macOS' Laufzeit-Signaturprüfung (taskgated/AMFI) cached ihr Ergebnis pro Datei/
  Inode; ein In-Place-Write auf ein kürzlich ausgeführtes Mach-O-Binary macht diesen Cache
  inkonsistent. Fix: die Datei bei jeder Ersetzung neu anlegen (`.part` + `Path.replace`,
  wie der Download-Pfad es schon tat) → neue Inode → der Kernel prüft die Signatur frisch.
  **Symptom, falls es doch wieder auftritt:** `evcc -v` (oder der Agent) stirbt sofort mit
  Exit 137; `~/Library/Logs/DiagnosticReports/evcc-*.ips` zeigt `"namespace":"CODESIGNING"`.
  Sofort-Fix ohne Codeänderung: Binary durch eine **neue** Datei ersetzen (nicht
  überschreiben) — z. B. `cp evcc.previous evcc.new && mv evcc.new evcc`.
- **GUI-Autostart nur im gebauten Bundle** (`sys.frozen`), nicht im Dev-Modus
  (`python -m src.app`) — `autostart` löst sonst keine sinnvollen ProgramArguments auf.
- **Menüleisten-Icons müssen quadratisch sein:** `statusitem` setzt das Icon per
  `setSize_` auf 20×20 pt (ohne das interpretiert `NSImage` die **Pixelmaße** der PNG als
  Punkte und sprengt die Menüleiste). Ein nicht-quadratisches Rep würde dabei gestaucht —
  `menubar_icon` rendert daher ins Quadrat mit erhaltenem Seitenverhältnis.
- **Notifications brauchen das echte Bundle:** `UNUserNotificationCenter` liefert nur aus,
  wenn der Prozess über den **App-Stub** `evcc.app/Contents/MacOS/evcc` startet. Im
  Dev-Modus (`python -m src.app`) *und* beim direkten Aufruf des eingebetteten
  `Contents/MacOS/python` lehnt macOS mit „Notifications are not allowed for this
  application" ab. Das ist **kein Bug** — nur im gebauten Bundle testen (verifiziert:
  `granted=True`, Zustellung ohne Fehler).
- **`NSMenu` deaktiviert Einträge selbst:** ohne `setAutoenablesItems_(False)` überschreibt
  AppKit den Aktiv-Zustand anhand der Responder-Chain und aktiviert die bewusst
  deaktivierten Info-Zeilen (Status/Version) wieder.
- **`NSMenuItem.target` ist eine schwache Referenz:** ein pro Eintrag erzeugtes Ziel-Objekt
  würde deallokiert und der Klick liefe ins Leere. Daher **ein** langlebiges `_MenuTarget`
  je `StatusItem`, Zuordnung über `tag`.
- **UI-Timer feuern bewusst nicht im Event-Tracking-Modus** (nur `NSDefaultRunLoopMode`):
  sonst würde ein Menü-Neuaufbau dem Nutzer das **geöffnete** Menü wegziehen.

## Richtung: PyObjC (Umstellung abgeschlossen)

Strategische Festlegung war: **bei Python bleiben und die UI vollständig auf PyObjC
vereinheitlichen, rumps ablösen.** Eine Sprache, ein Repo, Tests bleiben, kein IPC.
**Seit 2026-08-17 ist das erledigt** — `rumps` und `pync` sind aus Code, `requirements.txt`
und Bundle verschwunden.

Umgesetzt in sechs einzeln ausgelieferten, je grün getesteten Schritten:

1. **Settings-Fenster** → `settings_window.py` (`NSWindow`/`NSGridView`).
2. **Dialoge** → `dialogs.py` (`NSAlert` statt `rumps.alert`/`rumps.Window`).
3. **Statusleiste** → `statusitem.py` (`NSStatusItem`/`NSMenu` statt `rumps.App`/`MenuItem`).
4. **Timer** → `timers.py` (`NSTimer` statt `rumps.Timer`).
5. **Notifications** → `notify.py` (`UNUserNotificationCenter` statt `rumps.notification`/pync).
6. **Runloop + Dependency** → eigene `NSApplication`-Runloop in `app.main()`; rumps/pync raus.

**Leitplanke bleibt:** neue UI ausschließlich PyObjC, und die UI-Schicht hält weiterhin keine
evcc-Logik. Die Trennung „reine Datenschicht + dünne AppKit-Hülle" (`MenuEntry`,
`build_settings`) ist das Muster für alles Weitere — sie ist der Grund, warum die UI
überhaupt unit-testbar ist.
