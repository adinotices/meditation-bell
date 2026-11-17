#!/usr/bin/env bash
# Adds a "Meditation Bell" launcher entry and a `bell-timer` command that run
# the app from this folder. Nothing is copied; delete the two files to uninstall:
#   ~/.local/bin/bell-timer
#   ~/.local/share/applications/io.github.adinotices.MeditationBell.desktop
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
app_id="io.github.adinotices.MeditationBell"
bin="$HOME/.local/bin/bell-timer"
apps="$HOME/.local/share/applications"
mkdir -p "$HOME/.local/bin" "$apps"

# Desktop-entry quoting: inside "...", escape \ " ` and $ with a backslash.
desktop_quote() {
    local s=${1//\\/\\\\}
    s=${s//\"/\\\"}
    s=${s//\`/\\\`}
    s=${s//\$/\\\$}
    printf '"%s"' "$s"
}

printf '#!/usr/bin/env bash\nexec /usr/bin/python3 %q "$@"\n' "$here/bell_timer.py" > "$bin"
chmod +x "$bin"

# the desktop file is named after the app ID, so notifications and docks match it
rm -f "$apps/bell-timer.desktop"   # name used by earlier versions
{
    echo "[Desktop Entry]"
    echo "Type=Application"
    echo "Name=Meditation Bell"
    echo "Comment=Meditation timer with interval bells and a journal"
    echo "Exec=$(desktop_quote "$bin")"
    echo "Icon=$here/logo.png"
    echo "Terminal=false"
    echo "Categories=Utility;"
    echo "Keywords=meditation;bell;timer;sit;retreat;zen;"
    echo "StartupWMClass=bell-timer"
} > "$apps/$app_id.desktop"
command -v update-desktop-database >/dev/null && update-desktop-database "$apps" || true
echo "Installed. Run 'bell-timer' or open Meditation Bell from your launcher."
