#!/bin/zsh
# Install launchd jobs: daily pipeline run + daily report (7:00),
# weekly report (Sunday 17:00). launchd runs missed jobs at next wake.
# Re-run safely to update; uninstall with:
#   launchctl unload ~/Library/LaunchAgents/com.ai-digest.*.plist && rm ~/Library/LaunchAgents/com.ai-digest.*.plist
set -e
DIGEST_DIR="$(cd "$(dirname "$0")/.." && pwd)"
PYTHON="$DIGEST_DIR/.venv/bin/python"
AGENTS="$HOME/Library/LaunchAgents"
mkdir -p "$AGENTS"

cat > "$DIGEST_DIR/scripts/daily_job.sh" <<EOF
#!/bin/zsh
cd "$DIGEST_DIR"
"$PYTHON" main.py --no-email && "$PYTHON" scripts/generate_report.py daily
EOF
chmod +x "$DIGEST_DIR/scripts/daily_job.sh"

cat > "$AGENTS/com.ai-digest.daily.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.ai-digest.daily</string>
  <key>ProgramArguments</key><array>
    <string>/bin/zsh</string><string>$DIGEST_DIR/scripts/daily_job.sh</string>
  </array>
  <key>WorkingDirectory</key><string>$DIGEST_DIR</string>
  <key>StartCalendarInterval</key><dict>
    <key>Hour</key><integer>7</integer><key>Minute</key><integer>0</integer>
  </dict>
  <key>StandardOutPath</key><string>/tmp/ai-digest-daily.log</string>
  <key>StandardErrorPath</key><string>/tmp/ai-digest-daily.log</string>
</dict></plist>
EOF

cat > "$AGENTS/com.ai-digest.weekly.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.ai-digest.weekly</string>
  <key>ProgramArguments</key><array>
    <string>$PYTHON</string><string>$DIGEST_DIR/scripts/generate_report.py</string><string>weekly</string>
  </array>
  <key>WorkingDirectory</key><string>$DIGEST_DIR</string>
  <key>StartCalendarInterval</key><dict>
    <key>Weekday</key><integer>0</integer>
    <key>Hour</key><integer>17</integer><key>Minute</key><integer>0</integer>
  </dict>
  <key>StandardOutPath</key><string>/tmp/ai-digest-weekly.log</string>
  <key>StandardErrorPath</key><string>/tmp/ai-digest-weekly.log</string>
</dict></plist>
EOF

launchctl unload "$AGENTS/com.ai-digest.daily.plist" 2>/dev/null || true
launchctl unload "$AGENTS/com.ai-digest.weekly.plist" 2>/dev/null || true
launchctl load "$AGENTS/com.ai-digest.daily.plist"
launchctl load "$AGENTS/com.ai-digest.weekly.plist"
echo "Installed:"
launchctl list | grep ai-digest
echo "Daily pipeline+report: 7:00 every day · Weekly report: Sunday 17:00"
echo "Logs: /tmp/ai-digest-daily.log /tmp/ai-digest-weekly.log"
