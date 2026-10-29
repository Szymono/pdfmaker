#!/usr/bin/env bash
# Instalacja pdfmaker na Fedorze 44 (x86_64, CPU, bez GPU).
# Uruchom z katalogu skryptu:  ./install_fedora.sh
# Bezpieczne do ponownego uruchomienia (pomija to, co już jest).
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE"
LLAMA_TAG="b11738"

echo "==> 1/5 Pakiety systemowe (dnf, wymaga sudo)"
sudo dnf install -y \
  texlive-collection-xetex texlive-collection-luatex texlive-collection-latexextra \
  texlive-collection-langpolish texlive-collection-fontsrecommended texlive-collection-mathscience \
  texlive-lm texlive-lm-math texlive-tex-gyre texlive-pgf \
  pandoc-cli tesseract tesseract-langpack-pol qpdf poppler-utils \
  python3-img2pdf python3-pikepdf \
  uv curl tar zip unzip procps-ng libgomp openssl-libs \
  dejavu-sans-mono-fonts dejavu-sans-fonts dejavu-serif-fonts

echo "==> 2/5 Środowisko Pythona 3.12 (uv pobierze 3.12, Fedora 44 ma nowszy domyślny)"
if [ ! -x venv/bin/python ]; then
  # Fedora ustawia w uv pobieranie Pythona na 'manual' - instalujemy 3.12 jawnie
  uv python install 3.12 || sudo dnf install -y python3.12
  uv venv --python 3.12 venv
fi
uv pip install --python venv/bin/python torch torchvision --index-url https://download.pytorch.org/whl/cpu
uv pip install --python venv/bin/python "marker-pdf==2.0.0" pikepdf
# --layout faithful / --compare: analiza skanu i porównanie stron
uv pip install --python venv/bin/python opencv-python-headless scikit-image

echo "==> 3/5 llama-server (llama.cpp ${LLAMA_TAG}, CPU)"
if [ ! -x "llama/llama-${LLAMA_TAG}/llama-server" ]; then
  mkdir -p llama
  curl -L --fail -o /tmp/llama.tar.gz \
    "https://github.com/ggml-org/llama.cpp/releases/download/${LLAMA_TAG}/llama-${LLAMA_TAG}-bin-ubuntu-x64.tar.gz"
  tar -xzf /tmp/llama.tar.gz -C llama
  rm -f /tmp/llama.tar.gz
  # archiwum może się rozpakować do innego podkatalogu - ujednolicamy ścieżkę
  if [ ! -x "llama/llama-${LLAMA_TAG}/llama-server" ]; then
    found="$(find llama -name llama-server -type f | head -n1)"
    [ -n "$found" ] || { echo "Nie znaleziono llama-server w archiwum"; exit 1; }
    mkdir -p "llama/llama-${LLAMA_TAG}"
    cp -a "$(dirname "$found")"/. "llama/llama-${LLAMA_TAG}/"
  fi
fi
"llama/llama-${LLAMA_TAG}/llama-server" --version >/dev/null 2>&1 \
  || echo "UWAGA: llama-server nie startuje - sprawdź: ldd llama/llama-${LLAMA_TAG}/llama-server"

echo "==> 4/5 Uprawnienia skryptów"
chmod +x convert.sh extract_page.sh preview.sh usun_obrazy.py 2>/dev/null || true

echo "==> 5/5 Test (pobierze modele ~1.6 GB przy pierwszym uruchomieniu)"
python3 - <<'PY'
import shutil
for t in ["xelatex", "pandoc", "tesseract", "qpdf", "pdftoppm"]:
    print(f"  {t:10s}", "OK" if shutil.which(t) else "BRAK")
PY
tesseract --list-langs 2>/dev/null | grep -q '^pol$' && echo "  tesseract pol OK" || echo "  tesseract pol BRAK"
venv/bin/python -c "import marker, torch; print('  marker OK, torch', torch.__version__)"
venv/bin/python -c "import cv2; print('  opencv OK (--layout faithful)')" || echo "  opencv BRAK (--layout faithful nie zadziała)"

echo
echo "Gotowe. Przykład:  ./convert.sh ksiazka.pdf --pages 1-2 --out proba --newpage"
