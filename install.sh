#!/bin/bash
# Vaktin as a launchd user agent on macOS: starts at login, restarts if it dies.
#
#   ./install.sh install | uninstall | restart | status | logs
#
# A user agent, not a system daemon, on purpose: Vaktin runs `git`, `gh` and
# `balena` with YOUR credentials (~/.config/gh, ~/.balena). As root it would have
# none of them and every panel would read "unknown".
#
# Not a container, for the same reason. Containerising a stdlib HTTP server whose
# whole job is to shell out to three host CLIs against host checkouts would mean
# mounting the repos and both credential stores into it — on macOS, inside a VM —
# to gain nothing.
set -euo pipefail

LABEL="is.vaktin.agent"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
HERE="$(cd "$(dirname "$0")" && pwd)"
SCRIPT="$HERE/vaktin.py"
LOG="$HOME/Library/Logs/vaktin.log"
PORT="${PORT:-8787}"

# Apple's python3, not Homebrew's: stdlib-only means any 3.9+ works, and
# /usr/bin/python3 does not move when Homebrew upgrades a formula out from under
# a launchd agent that then silently fails to start.
PY="${PY:-/usr/bin/python3}"

# launchd gives an agent a minimal PATH. gh and balena live in Homebrew's bin on
# both Apple Silicon and Intel, so both are included and the missing one is
# simply never found.
AGENT_PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

case "${1:-}" in
install)
    [ -f "$SCRIPT" ] || { echo "vaktin.py not found beside install.sh"; exit 1; }
    mkdir -p "$HOME/Library/LaunchAgents" "$HOME/Library/Logs" "$HOME/.config/vaktin"
    [ -f "$HOME/.config/vaktin/repos" ] || {
        printf '# One absolute path per line: repositories Vaktin should watch.\n' \
            > "$HOME/.config/vaktin/repos"
        echo "note: wrote an empty $HOME/.config/vaktin/repos — add your repos to it"
    }
    cat > "$PLIST" <<PLIST_EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>$LABEL</string>
    <key>ProgramArguments</key>
    <array>
        <string>$PY</string>
        <string>$SCRIPT</string>
    </array>
    <key>WorkingDirectory</key><string>$HERE</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key><string>$AGENT_PATH</string>
        <key>HOME</key><string>$HOME</string>
        <key>PORT</key><string>$PORT</string>
    </dict>
    <key>RunAtLoad</key><true/>
    <key>KeepAlive</key><true/>
    <!-- Without this a crash-on-start loops as fast as launchd can spawn it. -->
    <key>ThrottleInterval</key><integer>10</integer>
    <key>StandardOutPath</key><string>$LOG</string>
    <key>StandardErrorPath</key><string>$LOG</string>
</dict>
</plist>
PLIST_EOF
    launchctl unload "$PLIST" 2>/dev/null || true
    launchctl load -w "$PLIST"
    sleep 2
    echo "installed $LABEL → http://localhost:$PORT"
    echo "  plist  $PLIST"
    echo "  log    $LOG"
    echo "  repos  $HOME/.config/vaktin/repos"
    ;;
uninstall)
    launchctl unload "$PLIST" 2>/dev/null || true
    rm -f "$PLIST"
    echo "removed $LABEL (config and logs left alone)"
    ;;
restart)
    launchctl unload "$PLIST" 2>/dev/null || true
    launchctl load -w "$PLIST"
    echo "restarted $LABEL"
    ;;
status)
    launchctl list | grep -F "$LABEL" || echo "$LABEL is not loaded"
    # The agent can be loaded and still not serving; ask the port, not launchd.
    code=$(curl -s -m 5 -o /dev/null -w '%{http_code}' "http://127.0.0.1:$PORT/" || true)
    echo "http://127.0.0.1:$PORT → ${code:-no answer}"
    ;;
logs)
    tail -n "${2:-40}" "$LOG" 2>/dev/null || echo "no log at $LOG yet"
    ;;
*)
    sed -n '2,8p' "$0"
    exit 1
    ;;
esac
