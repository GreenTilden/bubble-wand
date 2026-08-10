#!/usr/bin/env bash
# clawatch-bridge installer — stand up your OWN wrist co-pilot bridge.
#
# Run this on the machine that runs your Claude Code tmux sessions. It installs
# the bridge as a systemd --user service. Your Anthropic key is NOT entered here:
# after install, open the printed /setup URL from a device on your LAN and paste
# your key there — it is written locally and never leaves this box.
#
# Overrides (env vars):
#   CLAWATCH_PORT (8793)  CLAWATCH_HOST (0.0.0.0)  CLAWATCH_TMUX_SCOPE (dev:1)
#     — 'dev:1' is one window; a bare 'dev' is every window in the session
#   CLAWATCH_PREFIX (~/clawatch-bridge)  CLAWATCH_SERVICE (clawatch-bridge)
#   CLAWATCH_NO_SERVICE (set to skip systemd and just print the run command)
set -euo pipefail

PREFIX="${CLAWATCH_PREFIX:-$HOME/clawatch-bridge}"
PORT="${CLAWATCH_PORT:-8793}"
HOST="${CLAWATCH_HOST:-0.0.0.0}"
# One window (dev:1) or a whole session (dev). CLAWATCH_TMUX_WINDOW is the
# former name and is still honoured so an existing install script keeps working.
SCOPE="${CLAWATCH_TMUX_SCOPE:-${CLAWATCH_TMUX_WINDOW:-dev:1}}"
SERVICE="${CLAWATCH_SERVICE:-clawatch-bridge}"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="$PREFIX/clawatch.env"
# One-time /setup code. Deterministic so we can print a ready-to-open URL; it is
# only meaningful until you provision (after that /setup is inert regardless).
SETUP_CODE="${CLAWATCH_SETUP_TOKEN:-$(python3 -c 'import secrets;print(secrets.token_urlsafe(16))')}"

say(){ printf '\033[36m» %s\033[0m\n' "$*"; }
die(){ printf '\033[31m✗ %s\033[0m\n' "$*" >&2; exit 1; }

# --- prereqs -----------------------------------------------------------------
command -v python3 >/dev/null || die "python3 not found (need 3.11+)"
PYV=$(python3 -c 'import sys;print("%d.%d"%sys.version_info[:2])')
python3 -c 'import sys;sys.exit(0 if sys.version_info[:2]>=(3,11) else 1)' \
  || die "need Python 3.11+ (found $PYV)"
command -v tmux >/dev/null || die "tmux not found (the bridge drives your tmux Claude Code panes)"

# --- copy source into place (when installing from a clone elsewhere) ---------
say "Installing into $PREFIX"
mkdir -p "$PREFIX"
if [ "$SRC_DIR" != "$PREFIX" ]; then
  cp -r "$SRC_DIR/clawatch_bridge" "$SRC_DIR/pyproject.toml" "$PREFIX/"
  [ -d "$SRC_DIR/systemd" ] && cp -r "$SRC_DIR/systemd" "$PREFIX/" || true
fi

# --- venv + deps -------------------------------------------------------------
say "Creating venv + installing dependencies (fastapi, uvicorn, anthropic)"
python3 -m venv "$PREFIX/.venv"
"$PREFIX/.venv/bin/pip" -q install --upgrade pip
"$PREFIX/.venv/bin/pip" -q install "$PREFIX"

# --- env file (no secrets; the key arrives via /setup) -----------------------
if [ ! -f "$ENV_FILE" ]; then
  say "Writing $ENV_FILE (no secrets — you add your key via /setup)"
  ( umask 077; cat > "$ENV_FILE" <<EOF
# clawatch-bridge configuration. Secrets (ANTHROPIC_API_KEY, CLAWATCH_TOKEN)
# are written here by the /setup page — do not paste them by hand.
CLAWATCH_HOST=$HOST
CLAWATCH_PORT=$PORT
CLAWATCH_TMUX_SCOPE=$SCOPE
CLAWATCH_ENV_FILE=$ENV_FILE
EOF
  )
  chmod 600 "$ENV_FILE"
else
  say "Keeping existing $ENV_FILE"
fi

# --- service -----------------------------------------------------------------
if [ -n "${CLAWATCH_NO_SERVICE:-}" ]; then
  say "CLAWATCH_NO_SERVICE set — skipping systemd. Run the bridge with:"
  echo "    CLAWATCH_ENV_FILE=$ENV_FILE CLAWATCH_SETUP_TOKEN=$SETUP_CODE $PREFIX/.venv/bin/uvicorn clawatch_bridge.main:app --host $HOST --port $PORT"
else
  say "Installing systemd --user service: $SERVICE"
  mkdir -p "$HOME/.config/systemd/user"
  cat > "$HOME/.config/systemd/user/$SERVICE.service" <<EOF
[Unit]
Description=clawatch-bridge (tmux HTTP bridge for the wrist co-pilot)
After=default.target

[Service]
Type=simple
WorkingDirectory=$PREFIX
EnvironmentFile=$ENV_FILE
Environment=CLAWATCH_SETUP_TOKEN=$SETUP_CODE
ExecStart=$PREFIX/.venv/bin/uvicorn clawatch_bridge.main:app --host \${CLAWATCH_HOST} --port \${CLAWATCH_PORT}
Restart=on-failure
RestartSec=3

[Install]
WantedBy=default.target
EOF
  systemctl --user daemon-reload
  systemctl --user enable --now "$SERVICE"
  loginctl enable-linger "$USER" >/dev/null 2>&1 || true
  sleep 1
fi

# --- print the /setup URL with the one-time code -----------------------------
CODE="$SETUP_CODE"
LANIP=$(hostname -I 2>/dev/null | tr ' ' '\n' | grep -E '^(192\.168|10\.|172\.(1[6-9]|2[0-9]|3[01]))\.' | head -1 || true)
echo
printf '\033[32m✓ clawatch-bridge is running.\033[0m\n'
echo "  Finish setup from a phone/laptop on the SAME network as this box:"
echo "      http://${LANIP:-<this-box-LAN-IP>}:$PORT/setup${CODE:+?t=$CODE}"
echo "  Paste your own Anthropic API key there — it is stored on this box only."
echo
echo "  Then in the watch app's Settings, enter that URL (minus /setup) and the"
echo "  token shown after provisioning, and pair."
