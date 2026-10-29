#!/usr/bin/env bash
# Extract page(s) from a PDF without rendering anything.
# Usage: extract_page.sh input.pdf N out.pdf      (single page)
#        extract_page.sh input.pdf 10-20 out.pdf  (range, qpdf syntax)
set -euo pipefail
[ $# -eq 3 ] || { echo "Usage: $0 input.pdf N|A-B out.pdf" >&2; exit 2; }
in="$1"; pages="$2"; out="$3"
if command -v qpdf >/dev/null; then
  qpdf --warning-exit-0 --empty --pages "$in" "$pages" -- "$out"
else
  # fallback: pdfseparate + pdfunite (single page or range)
  a="${pages%-*}"; b="${pages#*-}"
  tmp=$(mktemp -d); pdfseparate -f "$a" -l "$b" "$in" "$tmp/p-%05d.pdf"
  if [ "$a" = "$b" ]; then mv "$tmp"/p-*.pdf "$out"; else pdfunite "$tmp"/p-*.pdf "$out"; fi
  rm -rf "$tmp"
fi
echo "$out"
