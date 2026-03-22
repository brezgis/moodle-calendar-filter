#!/usr/bin/env bash
# setup.sh — Quick setup for moodle-calendar-filter
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
CONFIG="$SCRIPT_DIR/config.json"
SCRIPT="$SCRIPT_DIR/filter-moodle-calendar.py"

echo "🎓 Moodle Calendar Filter — Setup"
echo "=================================="
echo

# Check Python
if ! command -v python3 &>/dev/null; then
    echo "❌ Python 3 is required but not found. Install it first."
    exit 1
fi

PYTHON_VERSION=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
echo "✓ Python $PYTHON_VERSION found"

# Config
if [ ! -f "$CONFIG" ]; then
    echo
    echo "Creating config.json..."
    cp "$SCRIPT_DIR/config.example.json" "$CONFIG"
    echo "✓ Created config.json from template"
    echo
    echo "📝 Next: edit config.json with your Moodle calendar URL."
    echo "   To find it: Moodle → Calendar → Export → Get calendar URL"
    echo
else
    echo "✓ config.json already exists"
fi

# Offer cron setup
echo
read -rp "Set up hourly cron job? [y/N] " cron_answer
if [[ "$cron_answer" =~ ^[Yy] ]]; then
    CRON_LINE="0 * * * * cd $SCRIPT_DIR && python3 $SCRIPT >> /tmp/moodle-filter.log 2>&1"
    
    # Check if already installed
    if crontab -l 2>/dev/null | grep -qF "filter-moodle-calendar"; then
        echo "⚠ Cron job already exists — skipping"
    else
        (crontab -l 2>/dev/null; echo "$CRON_LINE") | crontab -
        echo "✓ Cron job installed (runs every hour)"
        echo "  Logs: /tmp/moodle-filter.log"
    fi
fi

echo
echo "🎉 Setup complete!"
echo
echo "Run it now:  python3 filter-moodle-calendar.py"
echo "With a URL:  python3 filter-moodle-calendar.py --url 'https://...'"
echo "Tasks only:  python3 filter-moodle-calendar.py --tasks-only"
