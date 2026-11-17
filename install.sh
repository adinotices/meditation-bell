#!/usr/bin/env bash
# Adds a "Meditation Bell" launcher entry and a `bell-timer` command that run
# the app from this folder. Nothing is copied; delete the two files to uninstall.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
mkdir -p "$HOME/.local/bin" "$HOME/.local/share/applications"
cat > "$HOME/.local/bin/bell-timer" <<LAUNCH
#!/usr/bin/env bash
exec /usr/bin/python3 "$here/bell_timer.py" "\$@"
LAUNCH
chmod +x "$HOME/.local/bin/bell-timer"
cat > "$HOME/.local/share/applications/bell-timer.desktop" <<DESKTOP
[Desktop Entry]
Type=Application
Name=Meditation Bell
Comment=Meditation timer with interval bells and a practice log
Exec="$HOME/.local/bin/bell-timer"
Icon=$here/logo.png
Terminal=false
Categories=Utility;
Keywords=meditation;bell;timer;sit;retreat;zen;
StartupWMClass=bell-timer
DESKTOP
echo "Installed. Run 'bell-timer' or open Meditation Bell from your launcher."
