#!/usr/bin/env bash
# Run the gateway as a macOS LaunchAgent: starts at login, restarts if it dies.
#   gateway/service/macos.sh print       # show the plist it would install (default)
#   gateway/service/macos.sh install     # write ~/Library/LaunchAgents/<label>.plist and load it
#   gateway/service/macos.sh uninstall   # unload and remove it
#   gateway/service/macos.sh status
# Logs: <template>/.loop/gateway.log. After editing tiers.toml, profiles or .env: uninstall + install
# (or `launchctl kickstart -k gui/$(id -u)/<label>`).
set -euo pipefail
GW="$(cd "$(dirname "$0")/.." && pwd)"; ROOT="$(dirname "$GW")"
LABEL="${DEVTEAM_SERVICE_LABEL:-com.devteam.gateway}"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
plist() {
  cat <<XML
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key><array><string>/bin/bash</string><string>$GW/start.sh</string></array>
  <key>WorkingDirectory</key><string>$GW</string>
  <key>EnvironmentVariables</key><dict><key>PATH</key><string>$(dirname "$(command -v litellm || echo /usr/local/bin/litellm)"):/usr/bin:/bin:/usr/sbin:/sbin</string></dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>10</integer>
  <key>StandardOutPath</key><string>$ROOT/.loop/gateway.log</string>
  <key>StandardErrorPath</key><string>$ROOT/.loop/gateway.log</string>
</dict>
</plist>
XML
}
case "${1:-print}" in
  print) plist ;;
  install)
    [ "$(uname)" = Darwin ] || { echo "error: macOS only" >&2; exit 1; }
    mkdir -p "$(dirname "$PLIST")" "$ROOT/.loop"; plist > "$PLIST"
    launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
    launchctl bootstrap "gui/$(id -u)" "$PLIST"
    echo "installed $PLIST; the gateway now starts at login" ;;
  uninstall)
    launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
    rm -f "$PLIST"; echo "removed $LABEL" ;;
  status) launchctl print "gui/$(id -u)/$LABEL" 2>/dev/null | grep -E "state|pid" || echo "not loaded" ;;
  *) echo "usage: $0 print|install|uninstall|status" >&2; exit 2 ;;
esac
