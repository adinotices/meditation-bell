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
case "$here" in
    *$'\n'*) echo "The folder path contains a line break; move the app elsewhere first." >&2
              exit 1 ;;
esac
mkdir -p "$HOME/.local/bin" "$apps"

# An Exec value is read twice: first as a desktop-file string (where \\ means \),
# then as a command line (where, inside "...", \ " ` and $ need a backslash and a
# literal % is written %%). So: quote it for the command line, then double every
# backslash for the string.
desktop_quote() {
    local s=${1//\\/\\\\}
    s=${s//\"/\\\"}
    s=${s//\`/\\\`}
    s=${s//\$/\\\$}
    s=${s//%/%%}
    s="\"$s\""
    printf '%s' "${s//\\/\\\\}"
}

# don't replace someone else's `bell-timer` command
if [ -e "$bin" ] && ! grep -q "bell_timer.py" "$bin"; then
    echo "$bin already exists and isn't Meditation Bell's launcher; not replacing it." >&2
    exit 1
fi
printf '#!/usr/bin/env bash\nexec /usr/bin/python3 %q "$@"\n' "$here/bell_timer.py" > "$bin"
chmod +x "$bin"

# the desktop file is named after the app ID, so notifications and docks match it
# name used by earlier versions (removed only if it's ours)
if grep -qs "Name=Meditation Bell" "$apps/bell-timer.desktop"; then
    rm -f "$apps/bell-timer.desktop"
fi
{
    echo "[Desktop Entry]"
    echo "Type=Application"
    echo "Name=Meditation Bell"
    echo "Comment=Meditation timer with interval bells and a journal"
    echo "Exec=$(desktop_quote "$bin")"
    echo "Icon=${here//\\/\\\\}/logo.png"   # a backslash must be written as \\
    echo "Terminal=false"
    echo "Categories=Utility;"
    echo "Keywords=meditation;bell;timer;sit;retreat;zen;"
    echo "StartupWMClass=bell-timer"
} > "$apps/$app_id.desktop"
command -v update-desktop-database >/dev/null && update-desktop-database "$apps" || true
echo "Installed. Run 'bell-timer' or open Meditation Bell from your launcher."
