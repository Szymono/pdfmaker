#!/usr/bin/env bash
# Render one page of a PDF to PNG.
# Usage: preview.sh file.pdf [page=1] [dpi=110] [out.png]
set -euo pipefail
[ $# -ge 1 ] || { echo "Usage: $0 file.pdf [page] [dpi] [out.png]" >&2; exit 2; }
pdf="$1"; page="${2:-1}"; dpi="${3:-110}"
out="${4:-${pdf%.pdf}_p${page}.png}"
pdftoppm -f "$page" -l "$page" -r "$dpi" -png -singlefile "$pdf" "${out%.png}"
echo "$out"
