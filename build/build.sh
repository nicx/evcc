#!/usr/bin/env bash
#
# Baut "evcc.app" mit py2app und signiert es ad-hoc.
# Vom Repo-Root ausführen:  bash build/build.sh
#
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

PY="${PYTHON:-.venv/bin/python}"
APP="dist/evcc.app"

# Signier-Identität. Default "-" = ad-hoc (funktioniert ohne Zertifikat, aber die
# Code-Identität wechselt bei JEDEM Rebuild → macOS vergisst erteilte Berechtigungen
# (Mitteilungen, Gatekeeper). Mit einer stabilen selbstsignierten Identität bleiben sie
# erhalten:
#     CODESIGN_IDENTITY="nicx Selfsign" bash build/build.sh
# Verfügbare Identitäten: security find-identity -v -p codesigning
CODESIGN_IDENTITY="${CODESIGN_IDENTITY:--}"

if [[ ! -x "$PY" ]]; then
  echo "Kein venv-Python unter $PY. Erst: /opt/homebrew/bin/python3.13 -m venv .venv" >&2
  exit 1
fi

echo "==> Build-Abhängigkeiten"
"$PY" -m pip install --quiet -r requirements-build.txt

echo "==> py2app-Build"
rm -rf dist build/_py2app
"$PY" build/setup.py py2app --dist-dir dist --bdist-base build/_py2app

if [[ "$CODESIGN_IDENTITY" == "-" ]]; then
  echo "==> Signierung: ad-hoc (Hinweis: CODESIGN_IDENTITY setzen für stabile Identität)"
else
  echo "==> Signierung: $CODESIGN_IDENTITY"
fi
codesign --force --deep --sign "$CODESIGN_IDENTITY" "$APP"
codesign --verify --deep --strict "$APP"

echo "==> Fertig: $APP"
echo "    Erststart: Rechtsklick -> Öffnen  (nicht notarisiert -> Gatekeeper)."
