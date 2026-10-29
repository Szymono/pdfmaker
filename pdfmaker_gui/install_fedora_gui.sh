#!/usr/bin/env bash
# Instalacja pdfmaker GUI na Fedorze 44.  Uruchom z folderu GUI:  ./install_fedora_gui.sh
# Skrypt pdfmaker (convert.sh) instaluje się osobno: ../pdfmaker/install_fedora.sh
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE"

echo "==> 1/3 Pakiety systemowe (Tkinter, Tk, pdfinfo, xdg-open)"
sudo dnf install -y python3-tkinter tk poppler-utils xdg-utils

echo "==> 2/3 Środowisko .venv z tkinterdnd2 (przeciąganie plików)"
if [ ! -x .venv/bin/python ]; then
  # systemowy python3 + python3-tkinter; --system-site-packages daje dostęp do tkinter
  python3 -m venv --system-site-packages .venv
fi
.venv/bin/python -m pip install --upgrade --quiet tkinterdnd2
.venv/bin/python -c "import tkinter, tkinterdnd2; print('  tkinter', tkinter.TkVersion, '+ tkinterdnd2 OK')"

echo "==> 3/3 Skrót w menu aplikacji (~/.local/share/applications/pdfmaker.desktop)"
chmod +x gui.sh pdfmaker_gui.py
mkdir -p "$HOME/.local/share/applications"
sed "s|@GUI_DIR@|$HERE|g" pdfmaker.desktop > "$HOME/.local/share/applications/pdfmaker.desktop"
chmod +x "$HOME/.local/share/applications/pdfmaker.desktop"
command -v update-desktop-database >/dev/null && update-desktop-database "$HOME/.local/share/applications" || true

if [ -f "$HERE/../pdfmaker/convert.sh" ]; then
  echo "  Znaleziono skrypt: $(cd "$HERE/../pdfmaker" && pwd)/convert.sh"
else
  echo "  UWAGA: nie ma ../pdfmaker/convert.sh – wskaż folder skryptu w oknie (przycisk 'Wskaż folder skryptu…')."
fi
echo
echo "Gotowe. Uruchom:  ./gui.sh   albo z menu aplikacji: pdfmaker"
