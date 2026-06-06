#!/bin/bash
# Installa il Timone Server locale (porta 5020) come LaunchAgent.
# Doppio clic per eseguire, oppure: bash install_server.command

set -e

if command -v python3 >/dev/null 2>&1; then
    PYTHON=$(command -v python3)
else
    PYTHON=$(command -v python)
fi

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
LOCAL_DIR="$HOME/Library/Application Support/TimoneServer"
mkdir -p "$LOCAL_DIR"
cp "$SCRIPT_DIR/timone_server.py" "$LOCAL_DIR/timone_server.py"
SERVER="$LOCAL_DIR/timone_server.py"
PLIST="$HOME/Library/LaunchAgents/it.timone.server.plist"

cat > "$PLIST" << PLISTEOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>it.timone.server</string>
    <key>ProgramArguments</key>
    <array>
        <string>$PYTHON</string>
        <string>$SERVER</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>$HOME/timone_server.log</string>
    <key>StandardErrorPath</key>
    <string>$HOME/timone_server.log</string>
</dict>
</plist>
PLISTEOF

launchctl load "$PLIST"
echo "Timone server installato e avviato su porta 5020."
echo "Si avvierà automaticamente ad ogni login."
