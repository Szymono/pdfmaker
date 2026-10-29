#!/usr/bin/env python3
"""pdfmaker: scanned (image-only) PDF -> Markdown with LaTeX math -> text PDF.  100% local/offline.

Usage:
  convert.sh input.pdf [--pages 10-20] [--out outdir] [--mode math|text]
             [--engine xelatex|lualatex|pdflatex] [--force] [--skip-ocr] [--no-images]
             [--layout flow|faithful] [--compare] [--fakebold auto|X]

  --mode math  (default) Marker + Surya OCR (Polish text + LaTeX formulas), CPU ok
  --mode text  Tesseract 'pol' plain text (fast fallback, no formulas) + searchable PDF
Re-running with the same --out resumes: pages whose .md already exists are not OCR'd again
(use --force to redo).  --skip-ocr only rebuilds .tex/.pdf from existing .md (after manual edits).
Figures (math mode) are cropped from the scan by Marker and saved as pages/img/pNNNN_<Figure|Picture>_<k>.png|jpg,
referenced from pages/page_NNNN.md as img/<file>; --no-images disables this (figures are then dropped).
References to images that do not exist are replaced by a visible placeholder (never break the build).

  --layout flow      (default) continuous text, pages follow each other (old behaviour)
  --layout faithful  1 scanned page = 1 output page of the same size; every OCR block is placed where it is on
                     the scan (font size/weight measured from the scan), grey header bars, frames, rules, bullets
                     and figures are reproduced (lib/layout.py).  Needs the block layout pages/page_NNNN.json
                     (written by the OCR step; pages OCR'd earlier without it are OCR'd again).  math mode only.
  --compare          (faithful) write compare/page_NNNN_{side,overlay}.png + single-page PDFs and similarity
                     metrics (report.json) against the scan
"""
import argparse, json, os, re, shutil, subprocess, sys, time
from pathlib import Path

HERE = Path(__file__).resolve().parent
VENV_PY = HERE / "venv" / "bin" / "python"
LLAMA = HERE / "llama" / "llama-b11738" / "llama-server"

def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)

def run(cmd, timeout=None, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, **kw)

# ---------------------------------------------------------------- pages
def n_pages(pdf):
    out = run(["qpdf", "--show-npages", str(pdf)]).stdout.strip()
    return int(out)

def parse_pages(spec, total):
    if not spec:
        return list(range(1, total + 1))
    res = []
    for part in spec.split(","):
        part = part.strip()
        if "-" in part:
            a, b = part.split("-", 1)
            a = int(a) if a else 1
            b = int(b) if b else total
            res += list(range(a, b + 1))
        elif part:
            res.append(int(part))
    res = sorted(set(p for p in res if 1 <= p <= total))
    if not res:
        sys.exit(f"No valid pages in '{spec}' (document has {total} pages)")
    return res

def extract_page(src, n, dst):
    r = run(["qpdf", "--warning-exit-0", "--empty", "--pages", str(src), str(n), "--", str(dst)])
    if r.returncode != 0:
        r = run(["pdfseparate", "-f", str(n), "-l", str(n), str(src), str(dst)])
    if r.returncode != 0:
        raise RuntimeError(r.stderr)

# ---------------------------------------------------------------- OCR backends
def _descendants(root):
    """{pid: starttime} of all live descendants of `root` (read from /proc; Linux)."""
    kids, out = {}, {}
    for d in Path("/proc").iterdir():
        if not d.name.isdigit(): continue
        try:
            st = (d / "stat").read_text()
            f = st[st.rindex(")") + 2:].split()
            kids.setdefault(int(f[1]), []).append((int(d.name), f[19]))
        except (OSError, ValueError, IndexError):
            continue
    todo = [root]
    while todo:
        for pid, start in kids.get(todo.pop(), []):
            out[pid] = start; todo.append(pid)
    return out


def _kill_own(procs):
    """Kill only processes this run started (pid + start time must still match)."""
    import signal
    for pid, start in procs.items():
        try:
            st = Path(f"/proc/{pid}/stat").read_text()
            if st[st.rindex(")") + 2:].split()[19] != start: continue
            os.kill(pid, signal.SIGTERM)
        except (OSError, ValueError, IndexError):
            pass


def ocr_marker(jobs, workdir, extract_images=True, with_layout=False):
    """jobs: list of (pagenum, pdf, md). Returns {pagenum: (ok, sec, err, images)}."""
    if not jobs:
        return {}
    env = dict(os.environ)
    if LLAMA.exists():
        env.setdefault("LLAMA_CPP_BINARY", str(LLAMA))
    jf = workdir / "marker_jobs.json"
    for n, _, m in jobs:  # drop stale figures of pages that are re-OCR'd
        for old in (Path(m).parent / "img").glob(f"p{n:04d}_*"):
            old.unlink()
    jf.write_text(json.dumps({"extract_images": extract_images,
                              "jobs": [{"pdf": str(p), "md": str(m), "img_dir": str(Path(m).parent / "img"),
                                        "img_prefix": f"p{n:04d}",
                                        **({"json": str(Path(m).with_suffix(".json"))} if with_layout else {})}
                                       for n, p, m in jobs]}))
    by_md = {str(m): n for n, _, m in jobs}
    results = {}
    errlog = open(workdir / "marker_stderr.log", "a")
    proc = subprocess.Popen([str(VENV_PY), str(HERE / "lib" / "marker_worker.py"), str(jf)],
                            stdout=subprocess.PIPE, stderr=errlog, text=True, env=env)
    own = {}  # descendants (incl. surya's llama-server, which runs in its own session)
    import threading
    def _watch():
        while proc.poll() is None:
            own.update(_descendants(proc.pid)); time.sleep(2)
    threading.Thread(target=_watch, daemon=True).start()
    try:
        for line in proc.stdout:
            own.update(_descendants(proc.pid))
            if line.startswith("@@RESULT@@"):
                r = json.loads(line[len("@@RESULT@@"):])
                n = by_md[r["md"]]
                results[n] = (r["ok"], r["sec"], r.get("err", ""), r.get("images", []))
                imgs = f", {len(r.get('images', []))} image(s)" if r.get("images") else ""
                log(f"  page {n}: {'OK' if r['ok'] else 'FAILED ' + r.get('err','')} ({r['sec']} s{imgs})")
        proc.wait()
    finally:  # never leave the worker / its llama-server running (e.g. after Ctrl-C)
        if proc.poll() is None:
            own.update(_descendants(proc.pid))
            proc.terminate()
            try: proc.wait(timeout=20)
            except subprocess.TimeoutExpired: proc.kill()
        # kill only the llama-server(s) started by THIS run, never other jobs' servers
        _kill_own(own)
    if proc.returncode != 0:
        log(f"  marker worker exited with code {proc.returncode} (see {workdir/'marker_stderr.log'})")
    for n, _, _ in jobs:
        results.setdefault(n, (False, 0.0, "worker crashed before this page", []))
    return results

def ocr_tesseract(pdf, base, dpi=300):
    """Render one page and OCR with tesseract (pol). Writes base.txt and base.pdf (searchable)."""
    t = time.time()
    png = str(base) + ".png"
    r = run(["pdftoppm", "-r", str(dpi), "-gray", "-png", "-singlefile", str(pdf), str(base)])
    if r.returncode != 0:
        raise RuntimeError(r.stderr)
    r = run(["tesseract", png, str(base), "-l", "pol", "txt", "pdf"])
    os.remove(png)
    if r.returncode != 0:
        raise RuntimeError(r.stderr)
    return time.time() - t

# ---------------------------------------------------------------- markdown cleanup
_HTML_ENT = {"&amp;": "&", "&lt;": "<", "&gt;": ">", "&quot;": '"', "&#39;": "'", "&nbsp;": "\xa0", "&apos;": "'"}
_TAG_RE = re.compile(r"</?(?:sup|sub|b|strong|i|em|u|ins|s|strike|del|br|span|p|div|small|big|mark|a|font|tt|code|center|"
                     r"h[1-6]|ul|ol|li|table|tr|td|th|thead|tbody|math)\b[^>]*>", re.I)

def html_to_md(md):
    """Inline HTML that Marker leaves in its Markdown (<sup>, <sub>, <b>, <i>, <br>, ...) -> Markdown that pandoc turns
    into real LaTeX (superscript ^..^, subscript ~..~, **bold**, *italic*, hard line break).  Math ($..$, $$..$$) is
    left alone (it may contain '<' and '>')."""
    if "<" not in md and "&" not in md:
        return md
    parts = re.split(r"(\$\$.+?\$\$|\$[^$\n]+?\$)", md, flags=re.S)
    def sp(t):   # spaces inside ^..^ / ~..~ must be escaped for pandoc
        t = t.strip().replace(" ", "\\ ")
        return t
    def conv(t):
        t = re.sub(r"<br\s*/?>", "\\\\\n", t, flags=re.I)
        t = re.sub(r"<sup>\s*(.*?)\s*</sup>", lambda m: "^" + sp(m.group(1)) + "^" if m.group(1).strip() else "", t, flags=re.S | re.I)
        t = re.sub(r"<sub>\s*(.*?)\s*</sub>", lambda m: "~" + sp(m.group(1)) + "~" if m.group(1).strip() else "", t, flags=re.S | re.I)
        t = re.sub(r"<(b|strong)>(.*?)</\1>", lambda m: "**" + m.group(2).strip() + "**" if m.group(2).strip() else "", t, flags=re.S | re.I)
        t = re.sub(r"<(i|em)>(.*?)</\1>", lambda m: "*" + m.group(2).strip() + "*" if m.group(2).strip() else "", t, flags=re.S | re.I)
        t = re.sub(r"<(s|strike|del)>(.*?)</\1>", lambda m: "~~" + m.group(2).strip() + "~~", t, flags=re.S | re.I)
        t = _TAG_RE.sub("", t)            # any other (or left-over) known tag: keep the content only
        for k, v in _HTML_ENT.items():
            t = t.replace(k, v)
        return t
    return "".join(p if i % 2 else conv(p) for i, p in enumerate(parts))

def clean_md(md):
    md = md.replace("\r\n", "\n")
    md = html_to_md(md)
    # display math on its own paragraph
    md = re.sub(r"\s*\$\$(.+?)\$\$\s*", lambda m: "\n\n$$" + m.group(1).strip() + "$$\n\n", md, flags=re.S)
    # inline math: strip inner padding so pandoc recognises $...$
    def fix_inline(line):
        return re.sub(r"(?<![\$\\])\$(?!\$)[ \t]*([^\$\n]+?)[ \t]*(?<!\\)\$(?!\$)", r"$\1$", line)
    out = []
    for para in md.split("\n"):
        out.append(para if para.startswith("$$") else fix_inline(para))
    md = "\n".join(out)
    md = re.sub(r"(?m)(?<=\S)[ \t]{2,}", " ", md)          # collapse double spaces (not indentation)
    md = re.sub(r"\$[ \t]+(?=[.,;:!?)\]])", "$", md)       # "$x$ ;" -> "$x$;"
    md = re.sub(r"(?<=[(\[])[ \t]+\$", "$", md)            # "( $x$" -> "($x$"
    md = re.sub(r"\n{3,}", "\n\n", md)
    return md.strip() + "\n"

# ---------------------------------------------------------------- images
IMG_RE = re.compile(r"!\[([^\]]*)\]\(([^)\s]+)[^)]*\)")
IMG_PLACEHOLDER = "*[rysunek z oryginału -- brak pliku obrazu: {name}]*"

def resolve_images(md, pdir, n):
    """Point every ![](x) at an existing file relative to pdir (pages/). Missing -> placeholder.
    Returns (md, n_found, missing_names)."""
    found, missing = 0, []
    def repl(m):
        nonlocal found
        tgt = m.group(2)
        base = os.path.basename(tgt)
        stem = re.sub(r"^_page_\d+_", "", os.path.splitext(base)[0])
        cands = [tgt, "img/" + base] + [f"img/p{n:04d}_{stem}{e}" for e in (".png", ".jpg", ".jpeg")]
        for c in cands:
            if (pdir / c).is_file():
                found += 1
                return f"![]({c})"
        missing.append(base)
        return IMG_PLACEHOLDER.format(name=base.replace("_", "\\_"))
    md = IMG_RE.sub(repl, md)
    return md, found, missing

def center_images(tex):
    r"""Put stand-alone \includegraphics lines in a center environment."""
    return re.sub(r"(?m)^(\\includegraphics(?:\[[^\]]*\])?\{[^}]+\})[ \t]*$",
                  r"\\begin{center}\n\1\n\\end{center}", tex)

# ---------------------------------------------------------------- LaTeX
POLISH_OPS = ["tg", "ctg", "arctg", "arcctg", "cosec", "sh", "ch", "th", "cth", "arsh",
              "arch", "arth", "sgn", "Arg", "Ln", "rank", "rz", "tr"]

def preamble(engine):
    ops = "\\AtBeginDocument{" + "".join(rf"\def\{o}{{\operatorname{{{o}}}}}" for o in POLISH_OPS) + "}"
    if engine == "pdflatex":
        fonts = r"""\usepackage[T1]{fontenc}
\usepackage[utf8]{inputenc}
\usepackage{lmodern}
\usepackage{amsmath,amssymb,mathtools}
\usepackage[polish]{babel}"""
    else:
        fonts = r"""\usepackage{amsmath,mathtools}
\usepackage{fontspec}
\usepackage{unicode-math}
\setmainfont{Latin Modern Roman}
\setsansfont{Latin Modern Sans}
\setmonofont{DejaVu Sans Mono}[Scale=0.85]
\setmathfont{Latin Modern Math}
\usepackage{newunicodechar}
\newunicodechar{●}{\textbullet}\newunicodechar{○}{\ensuremath{\circ}}
\usepackage[polish]{babel}"""
    return rf"""\documentclass[11pt,a4paper]{{article}}
\usepackage[a4paper,margin=2.2cm]{{geometry}}
{fonts}
\usepackage{{longtable,booktabs,array,calc,graphicx,xcolor,fvextra,enumitem}}
\usepackage[hidelinks]{{hyperref}}
\providecommand{{\tightlist}}{{\setlength{{\itemsep}}{{0pt}}\setlength{{\parskip}}{{0pt}}}}
\providecommand{{\pandocbounded}}[1]{{#1}}
\graphicspath{{{{pages/}}{{../../pages/}}}}
\makeatletter
\def\maxwidth{{\ifdim\Gin@nat@width>\linewidth\linewidth\else\Gin@nat@width\fi}}
\def\maxheight{{\ifdim\Gin@nat@height>0.8\textheight0.8\textheight\else\Gin@nat@height\fi}}
\makeatother
\setkeys{{Gin}}{{width=\maxwidth,height=\maxheight,keepaspectratio}}
\providecommand{{\R}}{{\mathbb{{R}}}}\providecommand{{\N}}{{\mathbb{{N}}}}
\providecommand{{\Z}}{{\mathbb{{Z}}}}\providecommand{{\Q}}{{\mathbb{{Q}}}}
{ops}
\setcounter{{secnumdepth}}{{0}}
\setlength{{\parindent}}{{0pt}}\setlength{{\parskip}}{{4pt plus 2pt}}
\allowdisplaybreaks
\newcommand{{\pdfmakerpage}}[1]{{\par\medskip\noindent{{\color{{gray}}\footnotesize\textsf{{--- [s.~#1] ---}}}}\par}}
\newcommand{{\pdfmakerwarn}}[1]{{\par\noindent{{\color{{red}}\footnotesize\textsf{{[pdfmaker: #1]}}}}\par}}
"""

def md_to_tex(md):
    r = run(["pandoc", "-f", "markdown-auto_identifiers-implicit_figures-raw_html-fancy_lists+tex_math_dollars",
             "-t", "latex", "--wrap=preserve"], input=md, timeout=60)
    if r.returncode != 0:
        raise RuntimeError(r.stderr)
    return center_images(r.stdout)

def verbatim(text):
    text = text.replace(r"\end{Verbatim}", r"\end {Verbatim}")
    return ("\\begin{Verbatim}[breaklines,breakanywhere,fontsize=\\small]\n" + text.rstrip() +
            "\n\\end{Verbatim}\n")

def escape_text(text):
    rep = {"\\": r"\textbackslash{}", "&": r"\&", "%": r"\%", "$": r"\$", "#": r"\#",
           "_": r"\_", "{": r"\{", "}": r"\}", "~": r"\textasciitilde{}", "^": r"\textasciicircum{}"}
    t = "".join(rep.get(c, c) for c in text)
    paras = [p.strip().replace("\n", "\\\\\n") for p in re.split(r"\n\s*\n", t) if p.strip()]
    return "\n\n".join(paras) + "\n"

def compiles(engine, pre, body, tmpdir, name="t"):
    tmpdir.mkdir(parents=True, exist_ok=True)
    tex = tmpdir / f"{name}.tex"
    tex.write_text(pre + "\\begin{document}\n" + body + "\n\\end{document}\n", encoding="utf-8")
    try:
        r = run([engine, "-interaction=nonstopmode", "-halt-on-error", "-no-shell-escape", tex.name],
                cwd=tmpdir, timeout=120)
    except subprocess.TimeoutExpired:
        return False, "timeout"
    if r.returncode == 0:
        return True, ""
    errs = [l for l in r.stdout.splitlines() if l.startswith("!") or l.startswith("l.")]
    return False, " | ".join(errs[:4])

def repair_math(para):
    """Common OCR math glitches that break LaTeX: 'cases' with >2 columns -> array."""
    def fix_cases(m):
        body = m.group(1)
        cols = max(r.count("&") for r in body.split("\\\\")) + 1
        if cols <= 2:
            return m.group(0)
        return r"\left\{\begin{array}{" + "l" * cols + "}" + body + r"\end{array}\right."
    return re.sub(r"\\begin\{cases\}(.*?)\\end\{cases\}", fix_cases, para, flags=re.S)

def build_page_tex(n, md, engine, pre, tmpdir):
    """Return (tex_fragment, status). Fallback chain: full page -> per paragraph -> verbatim."""
    try:
        frag = md_to_tex(md)
        ok, err = compiles(engine, pre, frag, tmpdir)
        if ok:
            return frag, "ok"
    except Exception as e:
        err = f"pandoc: {e}"
    if repair_math(md) != md:
        try:
            frag = md_to_tex(repair_math(md))
            ok, _ = compiles(engine, pre, frag, tmpdir)
            if ok:
                log(f"  page {n}: LaTeX failed ({err[:120]}); fixed by math repair")
                return frag, "ok(math repaired)"
        except Exception:
            pass
    log(f"  page {n}: LaTeX failed ({err[:160]}); trying paragraph by paragraph")
    parts, bad = [], 0
    for para in [p for p in re.split(r"\n\s*\n", md) if p.strip()]:
        try:
            f = md_to_tex(para)
            ok, _ = compiles(engine, pre, f, tmpdir)
        except Exception:
            ok = False
        if not ok and repair_math(para) != para:
            try:
                f = md_to_tex(repair_math(para))
                ok, _ = compiles(engine, pre, f, tmpdir)
            except Exception:
                ok = False
        if not ok:
            bad += 1
            f = "\\pdfmakerwarn{akapit nie skompilował się -- surowy tekst OCR}\n" + verbatim(para)
        parts.append(f)
    frag = "\n\n".join(parts)
    ok, _ = compiles(engine, pre, frag, tmpdir)
    if ok:
        return frag, (f"partial({bad} raw paragraphs)" if bad else "ok(math repaired)")
    return ("\\pdfmakerwarn{strona nie skompilowała się -- surowy tekst OCR}\n" + verbatim(md),
            "verbatim")

# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("input")
    ap.add_argument("--pages", help="e.g. 10-20 or 1,3,5-7 (1-based, default: all)")
    ap.add_argument("--out", help="output dir (default: <input>_out)")
    ap.add_argument("--mode", choices=["math", "text"], default="math")
    ap.add_argument("--engine", choices=["xelatex", "lualatex", "pdflatex"], default="xelatex")
    ap.add_argument("--dpi", type=int, default=300, help="render dpi for tesseract (text mode)")
    ap.add_argument("--force", action="store_true", help="redo OCR even if page .md exists")
    ap.add_argument("--newpage", action="store_true", help="start every source page on a new PDF page")
    ap.add_argument("--skip-ocr", action="store_true", help="only rebuild tex/pdf from existing .md")
    ap.add_argument("--no-images", action="store_true",
                    help="do not extract figures (default: crop them from the scan into pages/img/)")
    ap.add_argument("--layout", choices=["flow", "faithful"], default="flow",
                    help="flow = continuous text (default); faithful = reproduce each scanned page 1:1")
    ap.add_argument("--compare", action="store_true",
                    help="(faithful) side-by-side/overlay PNGs + similarity metrics vs the scan in <out>/compare/")
    ap.add_argument("--fakebold", default="auto",
                    help="(faithful) stroke weight: auto = match the scan's ink, 0 = thin LM fonts, or a number")
    a = ap.parse_args()
    faithful = a.layout == "faithful"
    if faithful and a.mode != "math":
        sys.exit("--layout faithful needs --mode math (block positions come from Marker)")
    if faithful and a.engine == "pdflatex":
        log("--layout faithful uses fontspec fonts -> switching engine to xelatex")
        a.engine = "xelatex"

    src = Path(a.input).resolve()
    if not src.exists():
        sys.exit(f"not found: {src}")
    out = Path(a.out or (str(src.with_suffix("")) + "_out")).resolve()
    pdir, wdir = out / "pages", out / "work"
    pdir.mkdir(parents=True, exist_ok=True); wdir.mkdir(parents=True, exist_ok=True)
    total = n_pages(src)
    pages = parse_pages(a.pages, total)
    log(f"{src.name}: {total} pages, converting {len(pages)} page(s) -> {out}  [mode={a.mode}, engine={a.engine}]")
    t0 = time.time()
    stats = {n: {"page": n} for n in pages}
    old_report = {}
    if (out / "report.json").exists():  # keep stats of pages not processed in this run
        try:
            old_report = json.loads((out / "report.json").read_text(encoding="utf-8"))
        except Exception:
            old_report = {}
        for n in pages:
            prev = old_report.get(str(n))
            if isinstance(prev, dict):
                stats[n].update({k: v for k, v in prev.items() if k.startswith("ocr")})

    # 1. split pages
    for n in pages:
        pp = wdir / f"src_{n:04d}.pdf"
        if not pp.exists():
            extract_page(src, n, pp)

    # 2. OCR
    md_path = lambda n: pdir / f"page_{n:04d}.md"
    if not a.skip_ocr:
        if a.mode == "math":
            todo = [(n, wdir / f"src_{n:04d}.pdf", md_path(n)) for n in pages
                    if a.force or not md_path(n).exists() or md_path(n).stat().st_size == 0
                    or (faithful and not md_path(n).with_suffix(".json").exists())]
            log(f"Marker OCR on {len(todo)} page(s) (models load once; first run downloads ~1.6 GB)")
            tm = time.time()
            res = ocr_marker(todo, wdir, extract_images=not a.no_images, with_layout=faithful)
            stats["_marker_wall_sec"] = round(time.time() - tm, 1)
            for n, (ok, sec, err, imgs) in res.items():
                stats[n].update(ocr="marker" if ok else "failed", ocr_sec=sec, ocr_err=err, ocr_images=len(imgs))
                if not ok:  # fallback to tesseract plain text for this page
                    log(f"  page {n}: falling back to tesseract")
                    try:
                        s = ocr_tesseract(wdir / f"src_{n:04d}.pdf", wdir / f"tess_{n:04d}", a.dpi)
                        txt = (wdir / f"tess_{n:04d}.txt").read_text(encoding="utf-8")
                        md_path(n).with_suffix(".txt").write_text(txt, encoding="utf-8")
                        stats[n].update(ocr="tesseract-fallback", ocr_sec=sec + s)
                    except Exception as e:
                        stats[n]["ocr_err"] += f"; tesseract: {e}"
        else:
            for n in pages:
                tp = md_path(n).with_suffix(".txt")
                if tp.exists() and not a.force:
                    continue
                try:
                    s = ocr_tesseract(wdir / f"src_{n:04d}.pdf", wdir / f"tess_{n:04d}", a.dpi)
                    shutil.copy(wdir / f"tess_{n:04d}.txt", tp)
                    stats[n].update(ocr="tesseract", ocr_sec=round(s, 1))
                    log(f"  page {n}: tesseract OK ({s:.1f} s)")
                except Exception as e:
                    stats[n].update(ocr="failed", ocr_err=str(e)[:300])
                    log(f"  page {n}: tesseract FAILED {e}")

    # 3. per-page LaTeX with compile check
    if faithful:
        return faithful_build(a, src, out, pdir, wdir, pages, stats, old_report, md_path, t0)
    pre = preamble(a.engine)
    tmp = wdir / "texcheck"
    bodies = []
    for n in pages:
        mdp, txp = md_path(n), md_path(n).with_suffix(".txt")
        tt = time.time()
        if a.mode == "math" and mdp.exists():
            md = clean_md(mdp.read_text(encoding="utf-8"))
            md, nimg, miss = resolve_images(md, pdir, n)
            if nimg or miss:
                stats[n].update(images=nimg, images_missing=miss)
            if miss:
                log(f"  page {n}: {len(miss)} missing image(s) -> placeholder: {', '.join(miss)}")
            frag, st = build_page_tex(n, md, a.engine, pre, tmp)
        elif txp.exists():
            frag = escape_text(txp.read_text(encoding="utf-8"))
            ok, err = compiles(a.engine, pre, frag, tmp)
            st = "text" if ok else "verbatim"
            if not ok:
                frag = verbatim(txp.read_text(encoding="utf-8"))
        else:
            frag, st = f"\\pdfmakerwarn{{brak wyniku OCR dla strony {n}}}\n", "missing"
        (pdir / f"page_{n:04d}.tex").write_text(frag, encoding="utf-8")
        stats[n].update(tex=st, tex_sec=round(time.time() - tt, 1))
        bodies.append(("\\clearpage\n" if a.newpage and bodies else "") + f"\\pdfmakerpage{{{n}}}\n\\input{{pages/page_{n:04d}.tex}}\n")
        if st != "ok":
            log(f"  page {n}: tex status = {st}")

    # 4. combined document
    book = out / "book.tex"
    book.write_text(pre + "\\begin{document}\n" + "\n".join(bodies) + "\\end{document}\n", encoding="utf-8")
    md_all = "\n\n".join(f"<!-- strona {n} -->\n" + (md_path(n).read_text(encoding='utf-8') if md_path(n).exists()
                         else md_path(n).with_suffix('.txt').read_text(encoding='utf-8') if md_path(n).with_suffix('.txt').exists() else '')
                         for n in pages)
    md_all = md_all.replace("](img/", "](pages/img/")  # book.md lives one level above pages/
    (out / "book.md").write_text(md_all, encoding="utf-8")
    log(f"Compiling {book.name} with {a.engine}")
    r = run([a.engine, "-interaction=nonstopmode", "-no-shell-escape", book.name], cwd=out, timeout=1800)
    pdf = out / "book.pdf"
    if not pdf.exists():
        log("ERROR: combined compile failed, see book.log")
    if a.mode == "text":
        sp = [str(wdir / f"tess_{n:04d}.pdf") for n in pages if (wdir / f"tess_{n:04d}.pdf").exists()]
        if sp:
            run(["qpdf", "--warning-exit-0", "--empty", "--pages", *sp, "--", str(out / "searchable.pdf")])
    for f in ("book.aux", "book.out"):
        (out / f).unlink(missing_ok=True)

    write_report(out, stats, old_report, t0, pdf, pages)

def write_report(out, stats, old_report, t0, pdf, pages):
    stats["_total_sec"] = round(time.time() - t0, 1)
    merged = {k: v for k, v in old_report.items() if isinstance(v, dict) and k.isdigit()}
    merged.update({str(k): v for k, v in stats.items() if isinstance(k, int)})
    merged = {k: merged[k] for k in sorted(merged, key=int)}
    st_all = [v.get("tex") for v in merged.values()]
    merged["_summary"] = {"pages": len(merged), "tex_ok": sum(1 for x in st_all if str(x).startswith("ok")),
                          "partial": sum(1 for x in st_all if str(x).startswith("partial")),
                          "verbatim": st_all.count("verbatim"), "missing": st_all.count("missing"),
                          "ocr_failed": sum(1 for v in merged.values() if isinstance(v, dict) and v.get("ocr") == "failed"),
                          "images": sum(v.get("images", 0) for v in merged.values() if isinstance(v, dict)),
                          "images_missing": sum(len(v.get("images_missing", [])) for v in merged.values()
                                                if isinstance(v, dict))}
    merged["_last_run"] = {k: v for k, v in stats.items() if isinstance(k, str)}
    (out / "report.json").write_text(json.dumps(merged, indent=1, ensure_ascii=False), encoding="utf-8")
    log(f"Summary: {merged['_summary']}")
    log(f"Done in {stats['_total_sec']} s -> {pdf if pdf.exists() else 'NO PDF'}")
    for n in pages:
        s = stats[n]
        sim = s.get("similarity")
        sim = f"  ssim={sim['ssim_blur']} ink_f1={sim['ink_f1']} rows={sim['rows_corr']}" if sim else ""
        log(f"  p.{n}: ocr={s.get('ocr','cached')} {s.get('ocr_sec','')}s  tex={s.get('tex')}{sim}")

# ---------------------------------------------------------------- faithful layout
def faithful_build(a, src, out, pdir, wdir, pages, stats, old_report, md_path, t0):
    sys.path.insert(0, str(HERE / "lib"))
    import layout as L
    fb = None if a.fakebold == "auto" else float(a.fakebold)
    size = None
    bodies = []
    for n in pages:
        tt = time.time()
        jp = md_path(n).with_suffix(".json")
        if jp.exists():
            lay = json.loads(jp.read_text(encoding="utf-8"))
            size = size or (lay["page_bbox"][2], lay["page_bbox"][3])
            try:
                frag, info = L.build_page(n, lay, wdir / f"src_{n:04d}.pdf", pdir / "img", clean_md=clean_md,
                                          engine=a.engine, repair_math=repair_math, work=wdir / "texcheck",
                                          log=log, fakebold=fb)
                st = "ok" if not info.get("fallback_blocks") else f"partial({info['fallback_blocks']} raw blocks)"
                stats[n]["layout"] = info
                stats[n]["images"] = info.get("images", 0)
            except Exception as e:
                log(f"  page {n}: faithful layout FAILED: {type(e).__name__}: {e}")
                frag, st = ("\\begin{pmpage}\\put(40,-60){\\pdfmakerwarn{layout failed for page %d}}\\end{pmpage}\n" % n,
                            "failed")
        else:
            frag, st = ("\\begin{pmpage}\\put(40,-60){\\pdfmakerwarn{no layout json for page %d -- rerun OCR}}"
                        "\\end{pmpage}\n" % n, "missing")
        (pdir / f"page_{n:04d}.tex").write_text(frag, encoding="utf-8")
        stats[n].update(tex=st, tex_sec=round(time.time() - tt, 1))
        bodies.append(f"\\input{{pages/page_{n:04d}.tex}}\n")
        log(f"  page {n}: faithful layout {st} ({stats[n]['tex_sec']} s)")
    if size is None:  # no layout at all: page size of the source PDF
        r = run(["pdfinfo", str(src)]).stdout
        m = re.search(r"Page size:\s+([\d.]+) x ([\d.]+)", r)
        size = (float(m.group(1)), float(m.group(2))) if m else (595.28, 841.89)
    pre = L.preamble(size[0], size[1], a.engine)
    book = out / "book.tex"
    book.write_text(pre + "\\begin{document}\n" + "".join(bodies) + "\\end{document}\n", encoding="utf-8")
    md_all = "\n\n".join(f"<!-- strona {n} -->\n" + (md_path(n).read_text(encoding='utf-8') if md_path(n).exists() else '')
                         for n in pages).replace("](img/", "](pages/img/")
    (out / "book.md").write_text(md_all, encoding="utf-8")
    log(f"Compiling {book.name} with {a.engine} (faithful layout)")
    run([a.engine, "-interaction=nonstopmode", "-no-shell-escape", book.name], cwd=out, timeout=3600)
    pdf = out / "book.pdf"
    if not pdf.exists():
        log("ERROR: combined compile failed, see book.log")
    elif a.compare:
        import compare as C
        cdir = out / "compare"
        cdir.mkdir(exist_ok=True)
        for k, n in enumerate(pages, 1):
            try:
                m = C.compare(wdir / f"src_{n:04d}.pdf", 1, pdf, k, cdir, f"page_{n:04d}")
                run(["qpdf", "--warning-exit-0", "--empty", "--pages", str(pdf), str(k), "--",
                     str(cdir / f"page_{n:04d}.pdf")])
                stats[n]["similarity"] = m
            except Exception as e:
                log(f"  page {n}: compare failed: {e}")
        log(f"Comparison images in {cdir}")
    for f in ("book.aux", "book.out"):
        (out / f).unlink(missing_ok=True)
    write_report(out, stats, old_report, t0, pdf, pages)

if __name__ == "__main__":
    main()
