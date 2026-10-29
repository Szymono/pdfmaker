#!/usr/bin/env python3
"""Usuwa obrazy (XObject /Image i obrazy inline) z PDF-a, zostawiając tekst i grafikę wektorową.

Użycie:
  python3 usun_obrazy.py wejscie.pdf wyjscie.pdf [--pages 1-5,8]

Wymaga: pip install pikepdf
"""
import argparse
import sys
import pikepdf
from pikepdf import Name


def parse_pages(spec, n):
    if not spec:
        return set(range(n))
    out = set()
    for part in spec.split(","):
        part = part.strip()
        if "-" in part:
            a, b = part.split("-")
            out.update(range(int(a) - 1, min(int(b), n)))
        elif part:
            out.add(int(part) - 1)
    return {i for i in out if 0 <= i < n}


def clean(pdf, obj, resources, seen, stats):
    """Czyści strumień treści obj (strona lub Form XObject) z obrazów."""
    if resources is None:
        resources = pikepdf.Dictionary()
    xobjs = resources.get("/XObject", pikepdf.Dictionary())
    image_names = set()
    for name, xo in list(xobjs.items()):
        sub = xo.get("/Subtype")
        if sub == Name.Image:
            image_names.add(name)
        elif sub == Name.Form and xo.objgen not in seen:
            seen.add(xo.objgen)
            clean(pdf, xo, xo.get("/Resources", resources), seen, stats)

    new_ops = []
    for operands, op in pikepdf.parse_content_stream(obj):
        if isinstance(operands, pikepdf.PdfInlineImage) or str(op) == "INLINE IMAGE":
            stats["inline"] += 1
            continue
        if str(op) == "Do" and operands and str(operands[0]) in image_names:
            stats["xobject"] += 1
            continue
        new_ops.append((operands, op))
    data = pikepdf.unparse_content_stream(new_ops)
    if isinstance(obj, pikepdf.Page):
        obj.obj.Contents = pdf.make_stream(data)
    else:
        obj.write(data)
    for name in image_names:
        del xobjs[name]


def main():
    ap = argparse.ArgumentParser(description="Usuń obrazy z PDF-a")
    ap.add_argument("input")
    ap.add_argument("output")
    ap.add_argument("--pages", help="np. 1-5,8 (domyślnie wszystkie)")
    a = ap.parse_args()

    with pikepdf.open(a.input) as pdf:
        stats = {"xobject": 0, "inline": 0}
        seen = set()
        for i in sorted(parse_pages(a.pages, len(pdf.pages))):
            page = pdf.pages[i]
            clean(pdf, page, page.obj.get("/Resources"), seen, stats)
        pdf.remove_unreferenced_resources()
        pdf.save(a.output, compress_streams=True, object_stream_mode=pikepdf.ObjectStreamMode.generate)
    print(f"Usunięto obrazów: {stats['xobject']} (XObject) + {stats['inline']} (inline) -> {a.output}")


if __name__ == "__main__":
    sys.exit(main())
