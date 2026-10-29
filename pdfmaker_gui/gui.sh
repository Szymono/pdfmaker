#!/usr/bin/env bash
# Uruchamia okienkowy pdfmaker GUI.  Użycie: ./gui.sh [plik.pdf]
HERE="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
# Python z Tkinterem: najpierw własne środowisko GUI (.venv, z tkinterdnd2),
# potem systemowy python3, na końcu venv skryptu pdfmaker.
for py in "$HERE/.venv/bin/python" python3 "$HERE/../pdfmaker/venv/bin/python"; do
  if "$py" -c "import tkinter" >/dev/null 2>&1; then
    exec "$py" "$HERE/pdfmaker_gui.py" "$@"
  fi
done
msg="Brak Pythona z Tkinterem. Uruchom: $HERE/install_fedora_gui.sh  (albo: sudo dnf install python3-tkinter)"
echo "$msg" >&2
command -v notify-send >/dev/null && notify-send "pdfmaker GUI" "$msg"
exit 1
