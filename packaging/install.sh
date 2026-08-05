#!/usr/bin/env bash
# Install /Applications/No Brakes.app. Idempotent - re-run after any change.
set -euo pipefail
cd "$(dirname "$0")/.."
REPO="$(pwd)"
DEST="/Applications/No Brakes.app"

if [[ ! -x .venv/bin/python3 ]]; then
  echo "no venv - run: python3 -m venv .venv && .venv/bin/pip install -r requirements.txt" >&2
  exit 1
fi
if [[ ! -f .env ]]; then
  echo "no .env - copy .env.example to .env and fill it in" >&2
  exit 1
fi

# The icon is committed, so a machine without the Obsidian vault can still install.
if [[ -f "$HOME/Documents/Obsidian Vault/wiki/brand/assets/handled-mark-amber.png" ]]; then
  echo "building the icon..."
  bash packaging/make-icon.sh
elif [[ -f packaging/NoBrakes.app/Contents/Resources/nobrakes.icns ]]; then
  echo "brand mark not available; using the committed icon"
else
  echo "no brand mark and no committed icon - the app will use the generic one" >&2
fi

echo "installing hooks..."
bash packaging/install-hooks.sh

echo "installing $DEST ..."
rm -rf "$DEST"
cp -R packaging/NoBrakes.app "$DEST"
chmod +x "$DEST/Contents/MacOS/nobrakes"

# Pin the repo location so the launcher works from /Applications.
printf '%s' "$REPO" > "$DEST/Contents/Resources/REPO"

# Nudge Finder/Dock to pick up the new icon.
touch "$DEST"
/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister \
  -f "$DEST" >/dev/null 2>&1 || true
killall Dock >/dev/null 2>&1 || true

echo
echo "installed: $DEST"
echo "repo:      $REPO"
echo
echo "Double-click it from /Applications, or: open -a 'No Brakes'"
