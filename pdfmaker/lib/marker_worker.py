"""Run Marker (Surya OCR + LaTeX math) on a list of single-page PDFs, loading models once.
Usage: python marker_worker.py jobs.json
jobs.json: {"extract_images": true,
            "jobs": [{"pdf": ".../src_0001.pdf", "md": ".../page_0001.md",
                      "img_dir": ".../pages/img", "img_prefix": "p0001"}, ...]}
(a bare list of jobs is also accepted -> images disabled, old behaviour)
Optional per job: "json": ".../page_0001.json" -> additionally writes the page layout (faithful mode):
  {"page_bbox": [...], "blocks": [{"id","type","bbox","html","md","lines":[[x0,y0,x1,y1],...],
   "children":[...]}]}  (coordinates in PDF points of the page, origin top-left)
Writes one JSON line per page to stdout: {"md":..., "ok":bool, "sec":float, "err":str, "images":[...]}

With extract_images, every figure Marker detects is cropped from the (192 dpi) page scan and saved
as img_dir/<prefix>_<Figure|Picture>_<n>.<png|jpg>; the .md references are rewritten to img/<file>
(relative to the pages/ directory where the .md lives).
"""
import os, sys, json, time, traceback, signal, re
signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))  # run atexit cleanup (stops llama-server)
os.environ.setdefault("GRPC_VERBOSITY", "ERROR")
os.environ.setdefault("GLOG_minloglevel", "2")

HIGHRES_DPI = 192  # Marker's default highres_image_dpi (used to crop the images)

def is_color(img, tol=12):
    from PIL import ImageChops, ImageStat
    rgb = img.convert("RGB")
    r, g, b = rgb.split()
    diff = ImageChops.difference(r, g)
    diff = ImageChops.lighter(diff, ImageChops.difference(g, b))
    return ImageStat.Stat(diff).mean[0] > tol / 4 or diff.getextrema()[1] > 6 * tol

def save_images(text, images, img_dir, prefix):
    """Save Marker's PIL images with page-unique names, rewrite refs in markdown text."""
    os.makedirs(img_dir, exist_ok=True)
    saved, mapping = [], {}
    for name, img in images.items():
        stem = re.sub(r"^_page_\d+_", "", os.path.splitext(os.path.basename(name))[0])
        stem = re.sub(r"[^A-Za-z0-9]+", "_", stem).strip("_") or "img"
        if is_color(img):
            fn, im, kw = f"{prefix}_{stem}.jpg", img.convert("RGB"), {"quality": 92}
        else:
            fn, im, kw = f"{prefix}_{stem}.png", img.convert("L"), {"optimize": True}
        im.save(os.path.join(img_dir, fn), dpi=(HIGHRES_DPI, HIGHRES_DPI), **kw)
        mapping[name] = "img/" + fn
        saved.append(fn)
    def repl(m):
        tgt = m.group(2).strip()
        return f"![{m.group(1)}]({mapping.get(tgt, tgt)})"
    text = re.sub(r"!\[([^\]]*)\]\(([^)]+)\)", repl, text)
    return text, saved

def block_layout(document, page, md_cls):
    """Top-level blocks of a page with bbox, html, per-block markdown and line boxes."""
    from marker.schema import BlockTypes
    from marker.renderers.markdown import cleanup_text
    from marker.renderers.json import JSONRenderer
    out = []
    jr = JSONRenderer({"keep_pageheader_in_output": True, "keep_pagefooter_in_output": True})
    rendered = document.render(jr.block_config)
    pg = rendered.children[0]
    def conv(bo, depth=0):
        cls_children = bo.children or []
        html, _ = jr.extract_block_html(document, bo)
        blk = document.get_block(bo.id)
        lines = []
        if blk is not None:
            for ln in blk.contained_blocks(document, (BlockTypes.Line,)):
                lines.append([round(v, 2) for v in ln.polygon.bbox])
        try:
            md = cleanup_text(md_cls.convert(html)).strip()
        except Exception:
            md = ""
        d = {"id": str(bo.id), "type": str(bo.id.block_type), "bbox": [round(v, 2) for v in bo.polygon.bbox],
             "html": html, "md": md, "lines": lines}
        if depth == 0 and str(bo.id.block_type) in ("ListGroup", "TableGroup", "FigureGroup", "PictureGroup"):
            d["children"] = [conv(c, 1) for c in cls_children]
        return d
    for bo in pg.children:
        out.append(conv(bo))
    return {"page_bbox": [round(v, 2) for v in pg.polygon.bbox], "blocks": out}

def main():
    spec = json.load(open(sys.argv[1]))
    if isinstance(spec, list):
        spec = {"extract_images": False, "jobs": spec}
    jobs, extract = spec["jobs"], bool(spec.get("extract_images", True))
    from marker.models import create_model_dict
    from marker.converters.pdf import PdfConverter
    from marker.output import text_from_rendered
    models = create_model_dict()
    cfg = {"force_ocr": True, "disable_image_extraction": not extract,
           "highres_image_dpi": HIGHRES_DPI,
           "output_format": "markdown", "disable_tqdm": True}
    converter = PdfConverter(config=cfg, artifact_dict=models)
    for j in jobs:
        t = time.time()
        try:
            if j.get("json"):  # faithful layout: build once, render markdown + block layout
                document = converter.build_document(j["pdf"])
                renderer = converter.resolve_dependencies(converter.renderer)
                rendered = renderer(document)
                lay = block_layout(document, document.pages[0], renderer.md_cls)
                with open(j["json"] + ".part", "w", encoding="utf-8") as f:
                    json.dump(lay, f, ensure_ascii=False, indent=1)
                os.replace(j["json"] + ".part", j["json"])
            else:
                rendered = converter(j["pdf"])
            text, _, images = text_from_rendered(rendered)
            saved = []
            if extract and images and j.get("img_dir"):
                text, saved = save_images(text, images, j["img_dir"], j.get("img_prefix", "img"))
            tmp = j["md"] + ".part"
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(text)
            os.replace(tmp, j["md"])
            res = {"md": j["md"], "ok": True, "sec": round(time.time() - t, 1), "images": saved}
        except Exception as e:
            res = {"md": j["md"], "ok": False, "sec": round(time.time() - t, 1),
                   "err": f"{type(e).__name__}: {e}"[:500]}
            traceback.print_exc(file=sys.stderr)
        print("@@RESULT@@" + json.dumps(res, ensure_ascii=False), flush=True)

if __name__ == "__main__":
    main()
