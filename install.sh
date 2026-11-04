#!/bin/sh
# Install dialog-leveler. Usage: ./install.sh [target-dir]   (default: ~/.local/bin)
set -e
dest="${1:-$HOME/.local/bin}"
here="$(cd "$(dirname "$0")" && pwd)"

missing=""
command -v ffmpeg  >/dev/null 2>&1 || missing="$missing ffmpeg"
command -v ffprobe >/dev/null 2>&1 || missing="$missing ffprobe"
python3 -c "import numpy" 2>/dev/null || missing="$missing python3-numpy"
if [ -n "$missing" ]; then
    echo "Missing:$missing"
    echo "Install with: sudo apt install ffmpeg python3-numpy python3-matplotlib"
    exit 1
fi
python3 -c "import matplotlib" 2>/dev/null || echo "Note: python3-matplotlib not installed; --plot will be unavailable."

mkdir -p "$dest"
install -m 755 "$here/dialog_leveler.py" "$dest/dialog-leveler"
echo "Installed $dest/dialog-leveler"
case ":$PATH:" in
    *":$dest:"*) ;;
    *) echo "Note: $dest is not on your PATH. Add it, or log out and back in." ;;
esac
