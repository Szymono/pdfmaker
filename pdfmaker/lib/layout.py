"""pdfmaker --layout faithful: rebuild a scanned page 1:1 as real text + LaTeX math.

Input per page: Marker block layout (pages/page_NNNN.json written by marker_worker.py: block type, bbox in PDF
points, per-block Markdown from OCR) + the scan itself.  The scan is analysed automatically (OpenCV):
  * halftone-shaded rectangles (grey bars behind headers / "Przykład" labels) -> bbox + left/right grey level,
  * rectangular frames and long horizontal rules,
  * graphic markers in the left margin (filled bullets ●, rings ○),
  * x-height / line pitch / stroke width per block -> font size, line spacing, bold detection,
  * figures/tables are cropped from the scan at their place.
Output: a LaTeX fragment that draws one page of exactly the scan's size; every block is placed at its scanned
bbox (picture environment, unit 1bp, origin top-left), text is typeset in a box of the bbox width and shrunk by
a TeX loop until it fits the bbox height (never overflows into the next block).  Text is never edited by hand:
it is the OCR Markdown converted with pandoc.
"""
import json, os, re, subprocess, tempfile
import numpy as np
import cv2

# x-height / digit-height of the fonts used (measured: em fraction)
XH = {"rm": 0.43, "sf": 0.47, "rmbf": 0.44}
XH_MODE = 0.44              # Scan.xh_mode() of LM Roman 10 text / font size (calibrated, see calibrate())
DIGH = {"rm": 0.66, "sf": 0.70, "rmbf": 0.68}
ANALYSIS_DPI = 600          # scan analysis resolution (book scans are 600 dpi 1-bit)
FIG_DPI = 300               # resolution of cropped figures
BULLETS = "●•○◦■□▪"
SHADE_GAIN = 1.5            # a flat grey looks lighter than the same coverage of black halftone dots

# candidate sans fonts for headers / labels; the best one per page is chosen by comparing the predicted width
# of the OCR text (at the size given by the measured x-height) with the ink width on the scan
SF_FONTS = {"lmsansbold": "lmsans10-bold.otf", "lmsansdc": "lmsansdemicond10-regular.otf",
            "lmsans": "lmsans10-regular.otf", "herosbold": "texgyreheros-bold.otf",
            "heros": "texgyreheros-regular.otf"}
_EXTRA_FONTS = {"lmroman": "lmroman10-regular.otf"}
SF_BOLD = {"lmsansbold", "lmsansdc", "herosbold"}
_FONT_CACHE = {}

def font_metrics(key):
    """(PIL font at 1000 units, x-height fraction) for a SF_FONTS key, or None."""
    if key in _FONT_CACHE:
        return _FONT_CACHE[key]
    res = None
    try:
        from PIL import ImageFont
        fn = SF_FONTS.get(key) or _EXTRA_FONTS[key]
        path = subprocess.run(["kpsewhich", fn], capture_output=True, text=True).stdout.strip()
        if path:
            F = ImageFont.truetype(path, 1000)
            bb, bh = F.getbbox("x"), F.getbbox("H")
            res = (F, (bb[3] - bb[1]) / 1000.0, (bh[3] - bh[1]) / 1000.0)
    except Exception:
        res = None
    _FONT_CACHE[key] = res
    return res

def choose_sf(samples, cands):
    """samples: [(plain text, ink width pt, core height pt, 'x'|'H')] -> best key of cands."""
    best, best_err = cands[0], None
    for key in cands:
        fm = font_metrics(key)
        if not fm or not samples:
            continue
        F, xr, hr = fm
        errs = [abs(np.log(F.getlength(t) * (h / (xr if k == "x" else hr)) / 1000.0 / w))
                for t, w, h, k in samples if w > 5 and h]
        if not errs:
            continue
        e = float(np.median(errs)) + (0.0 if key.startswith("lm") else 0.06)   # prefer the TeX look (LM)
        if best_err is None or e < best_err:
            best, best_err = key, e
    return best, best_err

TEXT_TYPES = {"Text", "TextInlineMath", "Caption", "Footnote", "Handwriting", "Code", "Form",
              "TableOfContents", "ListItem", "Reference", "ComplexRegion"}
IMAGE_TYPES = {"Figure", "Picture", "Diagram", "Table", "ChemicalBlock"}
GROUP_TYPES = {"ListGroup", "FigureGroup", "PictureGroup", "TableGroup"}


# ------------------------------------------------------------------ helpers
def render_gray(pdf, dpi, page=1):
    with tempfile.TemporaryDirectory() as td:
        base = os.path.join(td, "p")
        subprocess.run(["pdftoppm", "-f", str(page), "-l", str(page), "-r", str(dpi), "-gray", "-png",
                        "-singlefile", str(pdf), base], check=True, capture_output=True)
        return cv2.imread(base + ".png", cv2.IMREAD_GRAYSCALE)

def fmt(v):
    return f"{v:.2f}".rstrip("0").rstrip(".")

def fmt_size(v):
    """Quantize font size to 0.25pt so XeTeX font slots stay bounded across long books."""
    return fmt(round(float(v) * 4) / 4)

def fmt_fakebold(v):
    """Snap FakeBold to {0, 0.25, 0.5} to match the 3 predeclared roman / 2 math families."""
    x = float(v)
    if x < 0.125:
        return "0"
    if x < 0.375:
        return "0.25"
    return "0.5"


# ------------------------------------------------------------------ scan analysis
IMAGE_PAGE_DARK = 0.30   # pages with more dark pixels are treated as pictures (covers, photos)


class Scan:
    def __init__(self, img, dpi, page_w, page_h):
        self.img, self.dpi = img, dpi
        self.s = dpi / 72.0                      # px per pt
        self.W, self.H = page_w, page_h
        ink = (img < 160).astype(np.uint8)
        # halftone dots are 1-3 px at 600 dpi; text strokes are >= 4 px -> opening separates them
        k = max(2, int(round(dpi / 200)))
        self.text = cv2.morphologyEx(ink, cv2.MORPH_OPEN, np.ones((k, k), np.uint8))
        # restore thin glyph parts next to strokes (hairlines, serifs)
        near = cv2.dilate(self.text, np.ones((2 * k + 1, 2 * k + 1), np.uint8))
        self.text = np.maximum(self.text, ink & near)
        self.dots = ink & (1 - near)             # isolated small dots = halftone
        self.shades = self._shades()
        # remove halftone from the text mask completely
        for sh in self.shades:
            x0, y0, x1, y1 = self.px(sh["bbox"])
            reg = self.text[y0:y1, x0:x1]
            n, lab, st, _ = cv2.connectedComponentsWithStats(reg, connectivity=8)
            small = np.where(st[:, cv2.CC_STAT_AREA] < (0.35 * self.s) ** 2 * 4)[0]
            reg[np.isin(lab, small[small > 0])] = 0
        self.frames, self.rules = self._lines()
        self.markers = []

    def px(self, bb, pad=0.0):
        x0, y0, x1, y1 = bb
        s = self.s
        return (max(0, int((x0 - pad) * s)), max(0, int((y0 - pad) * s)),
                min(self.img.shape[1], int(np.ceil((x1 + pad) * s))), min(self.img.shape[0], int(np.ceil((y1 + pad) * s))))

    def pt(self, x0, y0, x1, y1):
        s = self.s
        return [x0 / s, y0 / s, x1 / s, y1 / s]

    def _shades(self):
        s = self.s
        d = self.dots.astype(np.float32)
        win = int(4 * s) | 1
        dens = cv2.blur(d, (win, win))
        m = (dens > 0.03).astype(np.uint8)
        m = cv2.morphologyEx(m, cv2.MORPH_CLOSE, np.ones((int(5 * s), int(12 * s)), np.uint8))
        m = cv2.morphologyEx(m, cv2.MORPH_OPEN, np.ones((int(4 * s), int(8 * s)), np.uint8))
        n, lab, st, _ = cv2.connectedComponentsWithStats(m, connectivity=4)
        res = []
        for i in range(1, n):
            x, y, w, h, a = st[i]
            if w < 25 * s or h < 6 * s or a < 0.6 * w * h:
                continue
            # tighten to the real dot extent
            sub = self.dots[y:y + h, x:x + w]
            cols = np.where(cv2.blur(sub.astype(np.float32), (1, int(3 * s) | 1)).mean(0) > 0.01)[0]
            rows = np.where(sub.mean(1) > 0.01)[0]
            if len(cols) < 2 or len(rows) < 2:
                continue
            x0, x1, y0, y1 = x + cols[0], x + cols[-1] + 1, y + rows[0], y + rows[-1] + 1
            sub = 1.0 - self.img[y0:y1, x0:x1].astype(np.float32) / 255.0
            txt = cv2.dilate(self.text[y0:y1, x0:x1], np.ones((int(s), int(s)), np.uint8))
            # grey = halftone coverage, measured in rows free of text (top/bottom of the bar) when possible
            rowtxt = txt.mean(1)
            free = rowtxt < 0.01
            nb = 12
            prof = []
            for b in range(nb):
                a0, a1 = int(b * sub.shape[1] / nb), int((b + 1) * sub.shape[1] / nb)
                if free.sum() >= 0.5 * s:
                    blk = sub[free, a0:a1]
                    prof.append(float(blk.mean()))
                else:
                    mask = txt[:, a0:a1] == 0
                    prof.append(float(sub[:, a0:a1][mask].mean()) if mask.sum() > 50 else np.nan)
            prof = np.array(prof)
            ok = ~np.isnan(prof)
            if ok.sum() >= 2:
                xs = (np.arange(nb) + 0.5) / nb
                b1, b0 = np.polyfit(xs[ok], prof[ok], 1)
                gl, gr = b0, b0 + b1
            else:
                gl = gr = float(np.nanmean(prof)) if ok.any() else 0.08
            gl, gr = SHADE_GAIN * gl, SHADE_GAIN * gr
            res.append({"bbox": self.pt(x0, y0, x1, y1), "gray_left": float(np.clip(gl, 0.02, 0.9)),
                        "gray_right": float(np.clip(gr, 0.02, 0.9))})
        return res

    def _lines(self):
        s = self.s
        t = self.text
        hl = cv2.morphologyEx(t, cv2.MORPH_OPEN, np.ones((1, int(30 * s)), np.uint8))
        vl = cv2.morphologyEx(t, cv2.MORPH_OPEN, np.ones((int(12 * s), 1), np.uint8))
        both = cv2.dilate(np.maximum(hl, vl), np.ones((3, 3), np.uint8))
        n, lab, st, _ = cv2.connectedComponentsWithStats(both, connectivity=8)
        frames, used = [], np.zeros(n, bool)
        for i in range(1, n):
            x, y, w, h, a = st[i]
            if w < 30 * s or h < 10 * s:
                continue
            reg = lab[y:y + h, x:x + w] == i
            bh, bw = max(3, h // 4), max(3, min(w // 4, int(8 * s)))
            top, bot = reg[:bh].any(0).mean(), reg[-bh:].any(0).mean()
            lef, rig = reg[:, :bw].any(1).mean(), reg[:, -bw:].any(1).mean()
            if min(top, bot) > 0.8 and min(lef, rig) > 0.7:
                hh = hl[y:y + h, x:x + w]
                th = float(hh[:bh].sum() + hh[-bh:].sum()) / (2.0 * w) / s
                th = max(0.3, min(th, 1.5))
                frames.append({"bbox": self.pt(x, y, x + w, y + h), "thick": th})
                used[i] = True
        rules = []
        n2, lab2, st2, _ = cv2.connectedComponentsWithStats(hl, connectivity=8)
        for i in range(1, n2):
            x, y, w, h, a = st2[i]
            if w < 0.25 * self.W * s:
                continue
            bb = self.pt(x, y, x + w, y + h)
            if any(_inside(bb, f["bbox"], 2) for f in frames):
                continue
            th = float(a) / w / s
            rules.append({"bbox": [bb[0], (bb[1] + bb[3]) / 2 - th / 2, bb[2], (bb[1] + bb[3]) / 2 + th / 2],
                          "thick": max(0.3, th)})
        # line pixels are not text
        lines = np.zeros_like(t)
        for i in range(1, n):
            if used[i]:
                lines |= (lab == i).astype(np.uint8)
        for i in range(1, n2):
            x, y, w, h, a = st2[i]
            if w >= 0.25 * self.W * s:
                lines |= (lab2 == i).astype(np.uint8)
        lines = cv2.dilate(lines & np.maximum(hl, vl), np.ones((3, 3), np.uint8))
        self.text[lines > 0] = 0
        return frames, rules

    def find_markers(self, left_edge):
        """Filled discs / rings left of the text column (graphic markers of examples/exercises)."""
        s = self.s
        x1 = int((left_edge - 1.0) * s)
        if x1 <= 0:
            return []
        reg = self.text[:, :x1]
        n, lab, st, cen = cv2.connectedComponentsWithStats(reg, connectivity=8)
        out = []
        for i in range(1, n):
            x, y, w, h, a = st[i]
            d = (w + h) / 2.0 / s
            if not (2.2 <= d <= 10) or not (0.75 < w / max(h, 1) < 1.33):
                continue
            fill = a / float(w * h)
            if fill > 0.68:
                kind = "disc"
            elif 0.25 < fill < 0.6:
                inner = (lab[y + h // 3:y + 2 * h // 3, x + w // 3:x + 2 * w // 3] == i).mean()
                kind = "ring" if inner < 0.1 else None
            else:
                kind = None
            if kind:
                out.append({"kind": kind, "cx": (x + w / 2) / s, "cy": (y + h / 2) / s, "r": d / 2,
                            "thick": max(0.4, (a / (np.pi * w)) / s) if kind == "ring" else 0})
        self.markers = out
        return out

    # ---- per-block measurements
    def ink_bbox(self, bb, exclude=(), pad=1.0):
        x0, y0, x1, y1 = self.px(bb, pad=pad)
        reg = self.text[y0:y1, x0:x1].copy()
        for ex in exclude:
            ex0, ey0, ex1, ey1 = self.px(ex, pad=0.6)
            a0, a1 = max(ex0 - x0, 0), max(min(ex1 - x0, reg.shape[1]), 0)
            b0, b1 = max(ey0 - y0, 0), max(min(ey1 - y0, reg.shape[0]), 0)
            reg[b0:b1, a0:a1] = 0
        ys, xs = np.where(reg > 0)
        if len(xs) == 0:
            return list(bb)
        return self.pt(x0 + xs.min(), y0 + ys.min(), x0 + xs.max() + 1, y0 + ys.max() + 1)

    def measure(self, bb):
        """x-height (pt), line pitch (pt), number of text lines, stroke width (pt), comps count."""
        s = self.s
        x0, y0, x1, y1 = self.px(bb)
        reg = self.text[y0:y1, x0:x1]
        if reg.size == 0 or reg.sum() == 0:
            return {"xh": None, "pitch": None, "nlines": 0, "stroke": None, "ncomp": 0, "dig": None}
        n, lab, st, _ = cv2.connectedComponentsWithStats(reg, connectivity=8)
        hs = st[1:, cv2.CC_STAT_HEIGHT] / s
        ws = st[1:, cv2.CC_STAT_WIDTH] / s
        sel = hs[(hs > 1.5) & (hs < 12) & (ws > 0.8) & (ws < 12)]
        xh = None
        if len(sel) >= 3:
            hist, edges = np.histogram(sel, bins=np.arange(1.5, 12.01, 0.25))
            hist = np.convolve(hist, [1, 2, 1], "same")
            xh = float(edges[np.argmax(hist)] + 0.125)
        # rows -> line bands
        prof = reg.sum(1) > max(1, 0.3 * s)
        bands, start = [], None
        for i, v in enumerate(np.append(prof, False)):
            if v and start is None:
                start = i
            elif not v and start is not None:
                if i - start > 1.2 * s:
                    bands.append((start, i))
                start = None
        # merge bands closer than 1pt (accents / descenders)
        merged = []
        for b in bands:
            if merged and b[0] - merged[-1][1] < 1.0 * s:
                merged[-1] = (merged[-1][0], b[1])
            else:
                merged.append(b)
        # line cores: rows where the profile is >= 45% of the band maximum (x-height zone); a band may hold
        # several touching lines -> several cores.  baseline = bottom of a core.
        cores, baselines = [], []
        mass = [float(reg[a:b].sum()) for a, b in merged]
        for (a, b), ms in zip(merged, mass):
            p = reg[a:b].sum(1).astype(np.float32)
            if p.max() <= 0:
                continue
            # a fragment of the neighbouring block (descender of the heading above, ...): short AND hardly any ink
            if (b - a) < 0.5 * max(bb - aa for aa, bb in merged) and ms < 0.3 * max(mass):
                continue
            win = int(8 * s) | 1   # local maximum (+-4pt): a short line next to a long one keeps its core
            pm = cv2.dilate(p.reshape(-1, 1), np.ones((win, 1), np.uint8)).ravel()
            on = np.append((p >= 0.45 * pm) & (p > 0.02 * p.max()), False)
            bc, bb_ = [], []
            st0 = None
            for i, v in enumerate(on):
                if v and st0 is None:
                    st0 = i
                elif not v and st0 is not None:
                    if i - st0 >= 1.0 * s:
                        bc.append((i - st0) / s)
                        bb_.append((a + i + y0) / s)
                    st0 = None
            # drop spurious short cores (round letters, accents) -- relative to the biggest core of the SAME
            # line band only (a line of digits/parentheses must not erase the cores of plain-letter lines)
            if bc:
                big_b = max(bc)
                for c_, b_ in zip(bc, bb_):
                    if c_ >= 0.55 * big_b:
                        cores.append(c_)
                        baselines.append(b_)
        if cores:
            big = max(cores)
            bl = []
            for v in baselines:   # cores closer than 1.6 x-height belong to the same line
                if bl and v - bl[-1] < 1.6 * big:
                    bl[-1] = v
                else:
                    bl.append(v)
            baselines = bl
        xh_prof = float(np.median(cores)) if cores else None
        if xh is None or len(sel) < 3:
            xh = xh_prof if xh_prof else xh
        diffs = list(np.diff(baselines)) if len(baselines) >= 2 else []
        pitch = float(np.median(diffs)) if diffs else None
        # stroke width ~ ink area / skeleton length (approx via distance transform maximum mean)
        dist = cv2.distanceTransform(reg, cv2.DIST_L2, 3)
        ridge = (dist >= cv2.dilate(dist, np.ones((3, 3), np.uint8))) & (reg > 0)
        stroke = float(2 * dist[ridge].mean()) / s if ridge.any() else None
        return {"xh": xh, "pitch": pitch, "nlines": max(len(merged), len(baselines)), "stroke": stroke,
                "ncomp": int(len(sel)), "diffs": diffs,
                "bands": [((a + y0) / s, (b + y0) / s) for a, b in merged], "baselines": baselines,
                "cores": cores}

    def max_gap(self, bb):
        """Widest horizontal white gap (pt) inside a one-line block (spread-out items like 'a) ...   b) ...')."""
        x0, y0, x1, y1 = self.px(bb)
        cols = self.text[y0:y1, x0:x1].any(0)
        best, run = 0, 0
        for v in cols:
            run = 0 if v else run + 1
            best = max(best, run)
        return best / self.s

    def segments(self, bb, gap_pt):
        """Horizontal ink runs of a one-line region separated by white gaps >= gap_pt: [(x0, x1) pt]."""
        x0, y0, x1, y1 = self.px(bb)
        cols = self.text[y0:y1, x0:x1].any(0)
        gap = int(gap_pt * self.s)
        segs, start, last = [], None, None
        for i, v in enumerate(cols):
            if v:
                if start is None:
                    start = i
                elif i - last > gap:
                    segs.append((start, last + 1))
                    start = i
                last = i
        if start is not None:
            segs.append((start, last + 1))
        return [((x0 + a) / self.s, (x0 + b) / self.s) for a, b in segs]

    def xh_mode(self, bbs):
        """x-height (pt): mode (0.1pt bins) of heights of letter-sized connected components in the boxes."""
        hs = []
        for bb in bbs:
            x0, y0, x1, y1 = self.px(bb)
            reg = self.text[y0:y1, x0:x1]
            if reg.size == 0:
                continue
            n, lab, st, _ = cv2.connectedComponentsWithStats(reg, connectivity=8)
            h, w = st[1:, 3] / self.s, st[1:, 2] / self.s
            hs += list(h[(h > 2.0) & (h < 8) & (w > 1.2) & (w < 7)])
        if len(hs) < 20:
            return None
        hist, e = np.histogram(hs, bins=np.arange(2.0, 8.01, 0.1))
        hist = np.convolve(hist, [1, 2, 1], "same")
        return float(e[int(np.argmax(hist))] + 0.05)

    def first_word_bold(self, bb, body_stroke, line_h, thr=1.12):
        """Is the first word (left part of the first line) bold?  For run-in labels like 'a)'."""
        s = self.s
        x0, y0, x1, y1 = self.px(bb)
        y1 = min(y1, y0 + int(line_h * s))
        reg = self.text[y0:y1, x0:x1]
        cols = reg.any(0)
        if not cols.any():
            return False
        # first word = until first gap >= 0.25 * line height
        xs = np.where(cols)[0]
        gap = int(0.25 * line_h * s)
        end = xs[0]
        for x in xs[1:]:
            if x - end > gap:
                break
            end = x
        w = reg[:, xs[0]:end + 1]
        dist = cv2.distanceTransform(w, cv2.DIST_L2, 3)
        ridge = (dist >= cv2.dilate(dist, np.ones((3, 3), np.uint8))) & (w > 0)
        if not ridge.any() or not body_stroke:
            return False
        return float(2 * dist[ridge].mean()) / s > thr * body_stroke

    def crop(self, bb, path, pad=1.5):
        x0, y0, x1, y1 = self.px(bb, pad)
        sub = self.img[y0:y1, x0:x1]
        f = FIG_DPI / self.dpi
        if f < 1:
            sub = cv2.resize(sub, None, fx=f, fy=f, interpolation=cv2.INTER_AREA)
        cv2.imwrite(str(path), sub)
        return self.pt(x0, y0, x1, y1)


# stroke width / font size of Latin Modern Roman rendered + measured like the scan, for FakeBold 0..3
_FB_TABLE = [(0.0, 0.0642), (0.5, 0.0713), (1.0, 0.0728), (2.0, 0.0774), (3.0, 0.0856)]

SCAN_BLUR = 1.10   # a 600 dpi 1-bit scan of a printed page measures ~10% thicker strokes than the font itself

def ink_gain(stroke_pt, size_pt, cap=0.5, damp=0.4):
    """FakeBold value for the BODY text: only the stroke weight that exceeds regular Latin Modern after allowing for
    the print+scan blur (SCAN_BLUR).  Regular body text therefore stays regular (0); only really heavy scans get
    a little FakeBold.  Capped at 0.5."""
    if not stroke_pt or not size_pt:
        return 0.0
    r = stroke_pt / size_pt / SCAN_BLUR
    xs, ys = [a for a, _ in _FB_TABLE], [b for _, b in _FB_TABLE]
    if r <= ys[0] * 1.03:
        return 0.0
    return float(np.clip(damp * np.interp(r, ys, xs), 0.0, cap))

def stem_frac(key):
    """Stem width of 'l' in em for a SF_FONTS key (to tell regular from bold by the measured stroke)."""
    if ("stem", key) in _FONT_CACHE:
        return _FONT_CACHE[("stem", key)]
    v = None
    fm = font_metrics(key)
    if fm:
        try:
            from PIL import Image, ImageDraw
            im = Image.new("L", (600, 1400), 0)
            ImageDraw.Draw(im).text((100, 100), "l", font=fm[0], fill=255)
            a = np.array(im) > 128
            rows = np.where(a.any(1))[0]
            v = float(a[int(rows.mean())].sum()) / 1000.0
        except Exception:
            v = None
    _FONT_CACHE[("stem", key)] = v
    return v

def _inside(a, b, tol=0):
    return a[0] >= b[0] - tol and a[1] >= b[1] - tol and a[2] <= b[2] + tol and a[3] <= b[3] + tol

def _overlap(a, b):
    w = min(a[2], b[2]) - max(a[0], b[0])
    h = min(a[3], b[3]) - max(a[1], b[1])
    return max(0, w) * max(0, h)


# ------------------------------------------------------------------ LaTeX side
def preamble(page_w, page_h, engine="xelatex"):
    if engine == "pdflatex":
        fonts = r"""\usepackage[T1]{fontenc}\usepackage[utf8]{inputenc}\usepackage{lmodern}
\usepackage{amsmath,amssymb,mathtools}\usepackage[polish]{babel}
\newcommand{\pmsf}{\fontfamily{lmss}\fontseries{sbc}\selectfont}"""
    else:
        # XeTeX has a hard ~9000 font-slot limit. Per-page \addfontfeatures / \setmathfont FakeBold
        # and continuous \fontsize values (OCR sizes + shrink loops) each allocate a slot. Fix:
        # declare a fixed set of FakeBold families once, switch with \mathversion, and snap \pmS
        # to a 0.25bp grid so long faithful books stay under the limit.
        _rm_opts = ("BoldFont=lmroman10-bold.otf,ItalicFont=lmroman10-italic.otf,"
                    "BoldItalicFont=lmroman10-bolditalic.otf,SlantedFont=lmromanslant10-regular.otf")
        fonts = r"""\usepackage{amsmath,mathtools}
\usepackage{fontspec}
\usepackage{unicode-math}
\setmainfont{lmroman10-regular.otf}[""" + _rm_opts + r"""]
\setsansfont{Latin Modern Sans}
\setmonofont{DejaVu Sans Mono}[Scale=0.85]
\setmathfont{Latin Modern Math}
\newfontfamily\pmrmz{lmroman10-regular.otf}[""" + _rm_opts + r"""]
\newfontfamily\pmrma{lmroman10-regular.otf}[""" + _rm_opts + r""",FakeBold=0.25]
\newfontfamily\pmrmb{lmroman10-regular.otf}[""" + _rm_opts + r""",FakeBold=0.5]
""" + "\n".join(rf"\newfontfamily\pmsf{k}{{{f}}}" for k, f in SF_FONTS.items()) + r"""
\newcommand{\pmsf}{\pmsflmsansbold}
\usepackage{newunicodechar}
\newunicodechar{●}{\textbullet}\newunicodechar{○}{\ensuremath{\circ}}
\usepackage[polish]{babel}"""
    return rf"""\documentclass{{article}}
\usepackage[paperwidth={fmt(page_w)}bp,paperheight={fmt(page_h)}bp,margin=0pt,headheight=0pt,headsep=0pt,footskip=0pt]{{geometry}}
{fonts}
\usepackage{{graphicx,xcolor,tikz,array,booktabs}}
\usepackage[hidelinks]{{hyperref}}
\providecommand{{\tightlist}}{{\setlength{{\itemsep}}{{0pt}}\setlength{{\parskip}}{{0pt}}}}
\providecommand{{\pandocbounded}}[1]{{#1}}
\providecommand{{\R}}{{\mathbb{{R}}}}\providecommand{{\N}}{{\mathbb{{N}}}}
\providecommand{{\Z}}{{\mathbb{{Z}}}}\providecommand{{\Q}}{{\mathbb{{Q}}}}
\AtBeginDocument{{{''.join(rf'\def\{o}{{\operatorname{{{o}}}}}' for o in POLISH_OPS)}}}
\graphicspath{{{{pages/}}{{../../pages/}}{{../pages/}}}}
\pagestyle{{empty}}
\setlength{{\parindent}}{{0pt}}\setlength{{\parskip}}{{0pt}}\setlength{{\topskip}}{{0pt}}
\setlength{{\unitlength}}{{1bp}}
\tolerance=9000 \emergencystretch=2em \hbadness=10000 \vbadness=10000 \hfuzz=50pt \vfuzz=50pt
\allowdisplaybreaks
% ---- pdfmaker faithful layout macros
\newsavebox\pmbox \newdimen\pmS \newdimen\pmB \newdimen\pmT \newdimen\pmW \newdimen\pmL \newif\ifpmagain
% Snap \pmS to a 0.25bp grid so XeTeX does not allocate a new font slot for every OCR/shrink size.
% Snap \pmS to a 0.25bp grid (XeTeX font-slot hygiene). Grow/shrink steps are also 0.25bp so the
% loop cannot round back to the same size and hang.
\newcommand\pmsnaps{{\pmT=\pmS\advance\pmT by 8192sp\divide\pmT by 16384\multiply\pmT by 16384\pmS=\pmT}}
% \pmpar{{x}}{{y}}{{w}}{{h}}{{size}}{{baselineskip factor}}{{font/style}}{{indent}}{{content}}: paragraph box, (x,y)=first baseline,
% width w; font shrinks (0.25bp steps, down to 80% -- never unreadable) until the box height fits h (+10%); a rest
% of more than +25% is squeezed in height (moderately), so blocks do not run into the next one
\newsavebox\pmcb
% \pmcell{{w}}{{content}}: a cell of a row ('a) ..  b) ..'): starts at the scanned x offset, never overprints the next one
\newcommand\pmcell[2]{{\sbox\pmcb{{#2}}\ifdim\wd\pmcb>#1bp\relax\usebox\pmcb\hspace{{0.8em}}\else\hbox to #1bp{{\usebox\pmcb\hss}}\fi}}
\newcommand\pmlines{{0}}
\newcommand\pmpar[9]{{%
  \def\pmbuild{{\pmB=#6\pmS
    \sbox\pmbox{{\begin{{minipage}}[t]{{#3bp}}\fontsize{{\pmS}}{{\pmB}}\selectfont#7%
      \lineskiplimit=0pt\lineskip=0.15\pmS\relax\hspace*{{#8bp}}#9\par\xdef\pmgot{{\the\prevgraf}}\end{{minipage}}}}%
    \pmT=\ht\pmbox \advance\pmT by \dp\pmbox}}%
  \pmS=#5bp\relax\pmsnaps \pmL=#4bp\relax \pmL=1.1\pmL \advance\pmL by 1.5bp
  \pmbuild
  % grow (max +7%) while the paragraph has fewer lines than on the scan; step = one grid cell
  \loop \pmagainfalse
    \ifnum\pmgot<\pmlines\relax \ifdim\pmS<1.07\dimexpr#5bp\relax \ifdim\pmT<\pmL \pmagaintrue\fi\fi\fi
  \ifpmagain \advance\pmS by 0.25bp \pmbuild \repeat
  \ifdim\pmT>\pmL \ifdim\pmS>#5bp \advance\pmS by -0.25bp \pmbuild\fi\fi
  % shrink while too tall (down to 80%) or more lines than on the scan (down to 93%)
  \loop \pmagainfalse
    \ifdim\pmT>\pmL \ifdim\pmS>0.8\dimexpr#5bp\relax \pmagaintrue\fi\fi
    \ifnum\pmlines>0 \ifnum\pmgot>\pmlines\relax \ifdim\pmS>0.93\dimexpr#5bp\relax \pmagaintrue\fi\fi\fi
  \ifpmagain \advance\pmS by -0.25bp \pmbuild \repeat
  \pmL=1.25\pmL
  \ifdim\pmT>\pmL \sbox\pmbox{{\resizebox*{{!}}{{\pmL}}{{\usebox\pmbox}}}}\fi
  \pmvscale\put(#1,-#2){{\usebox\pmbox}}\gdef\pmlines{{0}}}}
% \pmline{{x}}{{baseline}}{{w}}{{size}}{{style}}{{content}}: single line, scaled to the scanned width w (-inf..+12%)
\newcommand\pmline[6]{{%
  \pmS=#4bp\relax\pmsnaps
  \sbox\pmbox{{\fontsize{{\pmS}}{{1.2\pmS}}\selectfont#5#6}}%
  \pmW=#3bp\relax \pmT=1.12\wd\pmbox
  \ifdim\pmT>\pmW \sbox\pmbox{{\resizebox{{\pmW}}{{!}}{{\usebox\pmbox}}}}\fi
  \pmvscale\put(#1,-#2){{\usebox\pmbox}}}}
% \pmspread: like \pmline, but inter-word spaces stretch so the line spans the full width w (as in the scan)
\newcommand\pmspread[6]{{%
  \pmS=#4bp\relax\pmsnaps
  \sbox\pmbox{{\fontsize{{\pmS}}{{1.2\pmS}}\selectfont#5#6}}%
  \pmW=#3bp\relax
  \ifdim\wd\pmbox>\pmW \sbox\pmbox{{\resizebox{{\pmW}}{{!}}{{\usebox\pmbox}}}}%
  \else \sbox\pmbox{{\fontsize{{\pmS}}{{1.2\pmS}}\selectfont#5\spaceskip=.33em plus 1fil\xspaceskip=.4em plus 1fil
    \hbox to \pmW{{#6}}}}\fi
  \pmvscale\put(#1,-#2){{\usebox\pmbox}}}}
% \pmeq{{cx}}{{cy}}{{w}}{{h}}{{size}}{{rows}}{{mode 0|1 jot|2 arraystretch}}{{math}}: display formula centred at
% (cx,cy): scaled to the scanned width w (max 130%), row spacing opened up to reach the scanned height h, never taller
\newcommand\pmeq[8]{{\begingroup
  \pmS=#5bp\relax\pmsnaps
  \sbox\pmbox{{\fontsize{{\pmS}}{{1.2\pmS}}\selectfont$\displaystyle #8$}}%
  \pmW=#3bp\relax \pmL=#4bp\relax
  \pmT=1.3\wd\pmbox \ifdim\pmT<\pmW \pmW=\pmT\fi
  \ifnum#6>1 \ifnum#7>0
    \pmT=\ht\pmbox \advance\pmT by \dp\pmbox
    \pmB=\dimexpr\pmL*\wd\pmbox/\pmW\relax
    \ifdim\pmB>1.04\pmT
      \ifnum#7=1 \advance\pmB by -\pmT \divide\pmB by \numexpr#6-1\relax \advance\jot by \pmB
      \else \edef\arraystretch{{\pmstrip{{\dimexpr1pt*\pmB/\pmT\relax}}}}\fi
      \sbox\pmbox{{\fontsize{{\pmS}}{{1.2\pmS}}\selectfont$\displaystyle #8$}}%
    \fi\fi\fi
  \sbox\pmbox{{\resizebox{{\pmW}}{{!}}{{\usebox\pmbox}}}}%
  \pmL=1.12\pmL \advance\pmL by 1.5bp
  \pmT=\ht\pmbox \advance\pmT by \dp\pmbox
  \ifdim\pmT>\pmL \sbox\pmbox{{\resizebox*{{!}}{{\pmL}}{{\usebox\pmbox}}}}\fi
  \put(#1,-#2){{\makebox(0,0){{\usebox\pmbox}}}}\endgroup}}
\makeatletter\newcommand\pmstrip[1]{{\strip@pt#1}}\makeatother
\newcommand\pmshade[6]{{\put(#1,-#2){{\begin{{tikzpicture}}[overlay]\shade[left color=black!#5,right color=black!#6]
  (0,0) rectangle (#3bp,-#4bp);\end{{tikzpicture}}}}}}
\newcommand\pmframe[5]{{\put(#1,-#2){{\begin{{tikzpicture}}[overlay]\draw[line width=#5bp] (0,0) rectangle (#3bp,-#4bp);\end{{tikzpicture}}}}}}
\newcommand\pmrule[4]{{\put(#1,-#2){{\rule[-#4bp]{{#3bp}}{{#4bp}}}}}}
\newcommand\pmdisc[3]{{\put(#1,-#2){{\tikz[overlay]\fill (0,0) circle (#3bp);}}}}
\newcommand\pmring[4]{{\put(#1,-#2){{\tikz[overlay]\draw[line width=#4bp] (0,0) circle (#3bp);}}}}
\newcommand\pmimg[5]{{\put(#1,-#2){{\raisebox{{-#4bp}}{{\includegraphics[width=#3bp,height=#4bp]{{#5}}}}}}}}
\newcommand\pmfb{{0}}
\newcommand\pmst{{1}}
\newcommand\pmsy{{1}}
% Pick among 3 predeclared roman FakeBold families; math FakeBold only toggles between 2 \setmathfont setups
% (never per-fragment \addfontfeatures). Size snapping (\pmsnaps) keeps XeTeX slots bounded.
\newcommand\pmweight[3]{{\edef\pmfbnew{{#1}}%
  \dimen0=#1pt\relax
  \ifdim\dimen0<0.375pt\relax\def\pmmathfb{{0}}\else\def\pmmathfb{{1}}\fi
  \ifx\pmmathfb\pmmathfbcur\else
    \ifdim\dimen0<0.375pt\relax\setmathfont{{Latin Modern Math}}%
    \else\setmathfont{{Latin Modern Math}}[FakeBold=0.5]\fi
    \let\pmmathfbcur\pmmathfb
  \fi
  \def\pmfb{{#1}}\def\pmst{{#2}}\def\pmsy{{#3}}%
  \ifdim\dimen0<0.125pt\relax\def\pmrm{{\pmrmz}}%
  \else\ifdim\dimen0<0.375pt\relax\def\pmrm{{\pmrma}}%
  \else\def\pmrm{{\pmrmb}}\fi\fi}}
\newcommand\pmmathfbcur{{-1}}
\newcommand\pmvscale{{\ifdim\pmsy pt=1pt\else\sbox\pmbox{{\scalebox{{1}}[\pmsy]{{\usebox\pmbox}}}}\fi}}
\newcommand\pmrm{{\pmrmz}}
\newenvironment{{pmpage}}{{\clearpage\noindent\begin{{picture}}(0,0)}}{{\end{{picture}}\par\null\clearpage}}
\newcommand{{\pdfmakerwarn}}[1]{{{{\color{{red}}\footnotesize\textsf{{[pdfmaker: #1]}}}}}}
"""

POLISH_OPS = ["tg", "ctg", "arctg", "arcctg", "cosec", "sh", "ch", "th", "cth", "arsh",
              "arch", "arth", "sgn", "Arg", "Ln", "rank", "rz", "tr"]


def esc(text):
    rep = {"\\": r"\textbackslash{}", "&": r"\&", "%": r"\%", "$": r"\$", "#": r"\#",
           "_": r"\_", "{": r"\{", "}": r"\}", "~": r"\textasciitilde{}", "^": r"\textasciicircum{}"}
    return "".join(rep.get(c, c) for c in text)


def md_batch_to_tex(mds):
    """Convert a list of markdown snippets with one pandoc call."""
    sep = "PMKSEPARATOR"
    src = ("\n\n" + sep + "\n\n").join(m if m.strip() else "\\ " for m in mds)
    r = subprocess.run(["pandoc", "-f", "markdown-auto_identifiers-implicit_figures-raw_html-fancy_lists"
                        "-smart+tex_math_dollars", "-t", "latex", "--wrap=none"],
                       input=src, capture_output=True, text=True, timeout=120)
    if r.returncode != 0:
        raise RuntimeError(r.stderr)
    parts = re.split(r"\n*" + sep + r"\n*", r.stdout.strip())
    if len(parts) != len(mds):
        # fallback: one by one
        parts = []
        for m in mds:
            rr = subprocess.run(["pandoc", "-f", "markdown-auto_identifiers-implicit_figures-raw_html-fancy_lists"
                                 "-smart+tex_math_dollars", "-t", "latex", "--wrap=none"],
                                input=m, capture_output=True, text=True, timeout=60)
            parts.append(rr.stdout.strip() if rr.returncode == 0 else esc(m))
    return [p.strip() for p in parts]


def fix_caps(md):
    """OCR often lowercases Polish diacritics inside all-caps words: 'CIąGŁE' -> 'CIĄGŁE'."""
    def f(m):
        w = m.group(0)
        low = [c for c in w if c.islower()]
        if sum(c.isupper() for c in w) >= 2 and low and all(c in "ąćęłńóśźż" for c in low):
            return w.upper()
        return w
    return re.sub(r"\w{3,}", f, md)

def strip_md_heading(md):
    md = re.sub(r"(?m)^\s{0,3}#{1,6}\s+", "", md)
    return md.strip()

def strip_list_marker(md):
    return re.sub(r"(?m)^\s*[-*+]\s+", "", md, count=1)

def strip_bullets(md):
    return re.sub(r"^\s*(\*\*)?\s*[" + BULLETS + r"]\s*", r"\1", md).strip()

LABEL_RE = re.compile(r"^(\(?[a-zA-Z0-9]{1,3}\)|\d{1,2}\.)(?=\s)")

def balance_math(m):
    """OCR math often has unbalanced \\begin/\\end or braces (a nested system of cases missing its last \\end): close what
    is open, drop what closes nothing -- the general cause of 'raw' (non compiling) formula blocks."""
    stack, out, pos = [], [], 0
    for t in re.finditer(r"\\(begin|end)\{([A-Za-z*]+)\}", m):
        out.append(m[pos:t.start()])
        pos = t.end()
        if t.group(1) == "begin":
            stack.append(t.group(2))
            out.append(t.group(0))
        elif t.group(2) in stack:
            while stack and stack[-1] != t.group(2):      # close inner environments first
                out.append("\\end{" + stack.pop() + "}")
            stack.pop()
            out.append(t.group(0))
        # else: stray \end -> dropped
    out.append(m[pos:])
    m = "".join(out)
    # braces
    depth, res, i = 0, [], 0
    while i < len(m):
        c = m[i]
        if c == "\\" and i + 1 < len(m):
            res.append(m[i:i + 2]); i += 2; continue
        if c == "{":
            depth += 1
        elif c == "}":
            if depth == 0:
                i += 1; continue
            depth -= 1
        res.append(c); i += 1
    return "".join(res) + "}" * depth + "".join("\\end{" + e + "}" for e in reversed(stack))

def balance_md_math(md):
    return re.sub(r"(?<!\\)\$([^$\n]+?)(?<!\\)\$", lambda mm: "$" + balance_math(mm.group(1)) + "$", md)

def eq_math(md):
    m = md.strip()
    m = re.sub(r"^\$\$|\$\$$", "", m).strip()
    m = re.sub(r"^\\\[|\\\]$", "", m).strip()
    # several $$..$$ in one block -> stack them
    if "$$" in m:
        parts = [p.strip() for p in m.split("$$") if p.strip()]
        m = r"\begin{gathered}" + r"\\".join(parts) + r"\end{gathered}"
    # top-level \\ without environment -> gathered
    depth, top = 0, False
    for tok in re.finditer(r"\\begin\{|\\end\{|\\\\", m):
        t = tok.group(0)
        if t == "\\begin{":
            depth += 1
        elif t == "\\end{":
            depth -= 1
        elif depth == 0:
            top = True
    if top:
        m = r"\begin{gathered}" + m + r"\end{gathered}"
    m = m.replace("\n", " ")
    return balance_math(m)


def eq_rows(m):
    """(number of rows of the outermost multi-line environment, 1=jot env | 2=array-like | 0)."""
    env = re.search(r"\\begin\{(aligned|gathered|split|alignedat|cases|array|matrix|pmatrix|bmatrix)\}", m)
    if not env:
        return 1, 0
    depth, rows = 0, 1
    for tok in re.finditer(r"\\begin\{|\\end\{|\\\\", m):
        t = tok.group(0)
        if t == "\\begin{":
            depth += 1
        elif t == "\\end{":
            depth -= 1
        elif depth == 1:
            rows += 1
    return rows, (1 if env.group(1) in ("aligned", "gathered", "split", "alignedat") else 2)


LIST_ITEM_RE = re.compile(r"^\s*(?:[-*+]|\d{1,2}[.)])\s+")
HR_RE = re.compile(r"(?m)^[ \t]*(?:-[ \t]*){3,}$|^[ \t]*(?:\*[ \t]*){3,}$|^[ \t]*(?:_[ \t]*){3,}$")

def parse_list(md):
    """Markdown list -> [item text] (bullet markers dropped, numbers kept) or None when md is not a list."""
    lines = [l for l in md.split("\n") if l.strip()]
    if not lines or not all(LIST_ITEM_RE.match(l) or l.startswith((" ", "\t")) for l in lines[:1]) \
            or not LIST_ITEM_RE.match(lines[0]):
        return None
    items = []
    for l in lines:
        mm = LIST_ITEM_RE.match(l)
        if mm:
            numbered = mm.group(0).strip()[0].isdigit()
            items.append((mm.group(0).strip() + " " if numbered else "") + l[mm.end():].strip())
        else:
            items[-1] += " " + l.strip()
    return items

def expand_list(b, items, scan, size_body, marker_boxes):
    """A list block is NOT typeset as a LaTeX list (that shrinks to fit the bbox and adds bullets): every row of the
    scan becomes its own single-line block at its own baseline; items that share a row on the scan ('a) ..  b) ..')
    become cells at their scanned x position.  Returns [block dicts] or None (-> plain paragraphs)."""
    bands = b["m"].get("bands") or []
    N, K = len(items), len(bands)
    if not N or not K or K > N:
        return None
    x0, _, x1, _ = b["ink"]
    rows = []
    for (ya, yb) in bands:
        bb = [x0 - 0.3, ya - 0.3, x1 + 0.3, yb + 0.3]
        rows.append({"ink": scan.ink_bbox(bb, exclude=marker_boxes, pad=0.0), "bb": bb})
    cells = []
    if K == N:
        cells = [[it] for it in items]
        xs = [[None] for _ in items]
    else:
        segs = [scan.segments(r["ink"], 1.2 * size_body) for r in rows]
        if sum(len(g) for g in segs) != N:
            return None
        k = 0
        for g in segs:
            cells.append(items[k:k + len(g)])
            k += len(g)
        xs = [[a for a, _ in g] for g in segs]
    out = []
    labelled = any(LABEL_RE.match(re.sub(r"[*_]", "", it)) or it[:1].isdigit() for it in items)
    for r, cs, cx in zip(rows, cells, xs):
        nb = dict(b)
        nb["type"] = "Text"
        nb["ink"] = r["ink"]
        nb["bbox"] = r["bb"]
        nb["m"] = scan.measure(r["ink"])
        nb["force_single"] = True
        if labelled:   # the main baseline of the row = baseline of its label 'c)' (not of a fraction's numerator)
            lx = cx[0] if cx and cx[0] is not None else r["ink"][0]
            lab = scan.ink_bbox([lx - 0.3, r["bb"][1], lx + 1.5 * size_body, r["bb"][3]], pad=0.0)
            lm = scan.measure(lab)
            if lm["baselines"]:
                nb["base"] = float(np.median(lm["baselines"]))
        pre = "" if labelled else "• "
        nb["cells"] = [(cx[j] if j < len(cx) else None, pre + c) for j, c in enumerate(cs)]
        out.append(nb)
    return out


# ------------------------------------------------------------------ page builder
def build_page(n, layout, src_pdf, img_dir, clean_md=lambda s: s, engine="xelatex", repair_math=None,
               work=None, log=print, fakebold=None):
    """Return (tex_fragment, info dict).  layout: dict from page_NNNN.json."""
    pw, ph = layout["page_bbox"][2], layout["page_bbox"][3]
    img = render_gray(src_pdf, ANALYSIS_DPI)
    dark = float((img < 160).mean())
    if dark > IMAGE_PAGE_DARK:
        # photographic page (e.g. a cover): text-on-dark cannot be reproduced as text faithfully,
        # so the whole scan is placed as one picture (OCR text stays in book.md)
        os.makedirs(img_dir, exist_ok=True)
        fn = f"p{n:04d}_L1_FullPage.png"
        cv2.imwrite(os.path.join(img_dir, fn), img)
        info = {"image_page": True, "dark_pct": round(100 * dark, 1), "images": 1, "blocks": len(layout["blocks"]),
                "fallback_blocks": 0}
        frag = (f"% page {n} (pdfmaker --layout faithful: image page, {100 * dark:.0f}% dark)\n"
                f"\\begin{{pmpage}}\n\\pmimg{{0}}{{0}}{{{fmt(pw)}}}{{{fmt(ph)}}}{{img/{fn}}}\n\\end{{pmpage}}\n")
        log(f"    page {n}: {100 * dark:.0f}% dark pixels -> placed as full-page image")
        return frag, info
    scan = Scan(img, ANALYSIS_DPI, pw, ph)
    # flatten blocks (groups -> children)
    blocks = []
    for b in layout["blocks"]:
        if b["type"] in GROUP_TYPES and b.get("children"):
            for c in b["children"]:
                blocks.append(dict(c))
        else:
            blocks.append(dict(b))
    blocks = [b for b in blocks if b["bbox"][2] - b["bbox"][0] > 0.5 and b["bbox"][3] - b["bbox"][1] > 0.5]
    # main text column left edge (mode of x0 of text blocks)
    x0s = [round(b["bbox"][0]) for b in blocks if b["type"] in TEXT_TYPES]
    if x0s:
        vals, cnt = np.unique(x0s, return_counts=True)
        left_edge = float(vals[np.argmax(cnt)])
    else:
        left_edge = 0.1 * pw
    markers = scan.find_markers(left_edge)
    marker_boxes = [[m["cx"] - m["r"], m["cy"] - m["r"], m["cx"] + m["r"], m["cy"] + m["r"]] for m in markers]

    # overlapping neighbours (Marker boxes often overlap by a few pt): cut at the emptiest row between them
    order = sorted(range(len(blocks)), key=lambda i: blocks[i]["bbox"][1])
    for ii, i in enumerate(order):
        A = blocks[i]["bbox"] = list(blocks[i]["bbox"])
        for j in order[ii + 1:]:
            B = blocks[j]["bbox"] = list(blocks[j]["bbox"])
            if B[1] >= A[3]:
                continue
            hov = min(A[2], B[2]) - max(A[0], B[0])
            if hov < 0.3 * min(A[2] - A[0], B[2] - B[0]):
                continue
            if A[3] - B[1] > 0.6 * min(A[3] - A[1], B[3] - B[1]):
                continue   # real nesting/large overlap: leave alone
            x0, y0, x1, y1 = scan.px([max(A[0], B[0]), B[1], min(A[2], B[2]), A[3]])
            prof = scan.text[y0:y1, x0:x1].sum(1)
            if len(prof):
                cut = (y0 + int(np.argmin(prof))) / scan.s
                A[3], B[1] = cut, cut

    # measurements
    for b in blocks:
        b["ink"] = scan.ink_bbox(b["bbox"], exclude=marker_boxes, pad=0.3)
        b["m"] = scan.measure(b["ink"])
    body = [b for b in blocks if b["type"] in TEXT_TYPES and b["m"]["xh"] and b["m"]["ncomp"] >= 8]
    xh_fine = scan.xh_mode([b["ink"] for b in body]) if body else None
    if body:
        wts = np.array([b["m"]["ncomp"] for b in body], float)
        xhs = np.repeat([b["m"]["xh"] for b in body], wts.astype(int))
        xh_body = float(np.median(xhs))
    else:
        xh_body = 4.3 * 0.95
    size_body = xh_fine / XH_MODE if xh_fine else xh_body / XH["rm"]
    # width-based estimate (more relevant for layout): plain text blocks, LM Roman 10 advance widths
    fmr = font_metrics("lmroman")
    wsz = []
    if fmr:
        for b in blocks:
            md = b.get("md", "") or ""
            if b["type"] not in TEXT_TYPES or "$" in md or b["m"]["ncomp"] < 8:
                continue
            txt = re.sub(r"[*_#`]|^\s*[-+]\s+", "", md).replace("\n", " ").strip()
            L = fmr[0].getlength(txt) / 1000.0
            w = b["ink"][2] - b["ink"][0]
            nl = max(1, b["m"]["nlines"])
            if L <= 0 or len(txt) < 8:
                continue
            if nl == 1:
                wsz.append(w / L)
    # scans are often scaled a little differently in x and y.  Font size from the widths (line breaks / line
    # lengths stay right), the remaining height difference -> vertical scale sy of every text box
    stretch, sy = 1.0, 1.0
    size_xh = size_body
    if len(wsz) >= 2 and 0.8 < np.median(wsz) / size_xh < 1.25:
        size_body = float(np.median(wsz))
        sy = float(np.clip(size_xh / size_body, 0.9, 1.15))
        if abs(sy - 1) < 0.02:
            sy = 1.0
    pitches = [d for b in body for d in b["m"].get("diffs", []) if 2.0 * xh_body < d < 3.3 * xh_body]
    pitch_body = float(np.median(pitches)) if pitches else 1.2 * size_body * sy
    bskip = float(np.clip(pitch_body / (size_body * sy), 1.08, 1.45)) if pitches else 1.2
    strokes = [b["m"]["stroke"] for b in body if b["m"]["stroke"]]
    stroke_body = float(np.median(strokes)) if strokes else None
    fb = fakebold if fakebold is not None else ink_gain(stroke_body, size_body)
    info = {"vscale": round(sy, 3), "size_xheight_pt": round(size_xh, 2), "width_samples": len(wsz), "fakebold": round(fb, 2), "size_body_pt": round(size_body, 2), "baselineskip": round(bskip, 3),
            "stroke_body_pt": round(stroke_body, 3) if stroke_body else None,
            "shades": len(scan.shades), "frames": len(scan.frames), "rules": len(scan.rules),
            "markers": len(markers), "blocks": len(blocks)}

    # ink that no OCR block covers (e.g. a drawing Marker did not detect): crop it as a picture so nothing is lost
    cov = scan.text.copy()
    for b in blocks:
        x0, y0, x1, y1 = scan.px(b["bbox"], pad=1.5)
        cov[y0:y1, x0:x1] = 0
    for mb in marker_boxes:
        x0, y0, x1, y1 = scan.px(mb, pad=1.0)
        cov[y0:y1, x0:x1] = 0
    tot = max(1, int(scan.text.sum()))
    k = int(4 * scan.s) | 1
    grp = cv2.dilate(cov, np.ones((k, k), np.uint8))
    nn, lab, st, _ = cv2.connectedComponentsWithStats(grp, connectivity=8)
    extra = []
    for i in range(1, nn):
        x, y, w, h, _ = st[i]
        inkpx = int(cov[y:y + h, x:x + w].sum())
        if w > 25 * scan.s and h > 15 * scan.s and inkpx > 0.002 * tot:
            bb = scan.pt(x + k // 2, y + k // 2, x + w - k // 2, y + h - k // 2)
            extra.append({"type": "Picture", "bbox": bb, "ink": bb, "md": "", "uncovered": True})
    info["uncovered_ink_pct"] = round(100.0 * cov.sum() / tot, 2)
    info["uncovered_pictures"] = len(extra)

    # ------------- decide style + content per block
    items = [("img", b, "") for b in extra]   # (kind, block, md)
    for b in blocks:
        t = b["type"]
        raw = re.sub(r"[ \t]{2,}\n", "\\\\\n", b.get("md", "") or "")   # keep OCR hard line breaks
        md = clean_md(raw).strip()
        if t in IMAGE_TYPES:
            items.append(("img", b, ""))
            continue
        if not md:
            continue
        if t == "Equation" and md.startswith("$$"):
            items.append(("eq", b, md))
            continue
        if t in ("SectionHeader", "PageHeader", "PageFooter"):
            md = strip_md_heading(md)
        if t == "ListItem":
            md = strip_list_marker(md)
        if any(_overlap(b["bbox"], mb) > 0 or (abs(mb[3] - b["bbox"][3]) < 12 and mb[2] <= b["bbox"][0] + 12
                                               and b["bbox"][0] - mb[2] < 15) for mb in marker_boxes):
            md = strip_bullets(md)
        md = re.sub("\xa0{2,}", r" \\quad ", md).replace("\xa0", " ")
        md = fix_caps(md)
        md = balance_md_math(HR_RE.sub("", md)).strip()      # horizontal rules are drawn from the scan (pmrule), never typeset
        if not md:
            continue
        lst = parse_list(md)
        if lst:
            rows = expand_list(b, lst, scan, size_body, marker_boxes)
            if rows:
                items.extend(("txt", r, " ".join(c for _, c in r["cells"])) for r in rows)
                continue
            md = "\n\n".join(("" if LABEL_RE.match(re.sub(r"[*_]", "", it)) or it[:1].isdigit() else "• ") + it
                              for it in lst)
        items.append(("txt", b, md))

    # style per text item
    body_ratio = (stroke_body / xh_body) if stroke_body else None
    for i, (kind, b, md) in enumerate(items):
        if kind != "txt":
            continue
        m = b["m"]
        t = b["type"]
        plain = re.sub(r"\$[^$]*\$", "x", md)
        plain = re.sub(r"[*_#`]", "", plain).strip()
        b["plain"] = plain
        b["heavy"] = bool(body_ratio and m["stroke"] and m["xh"] and m["stroke"] / m["xh"] > 1.25 * body_ratio
                          and m["stroke"] > 1.3 * stroke_body)
        short = len(plain.split()) <= 6 and m["nlines"] <= 1
        b["fam"] = "sf" if (t in ("SectionHeader", "PageHeader", "PageFooter") or (b["heavy"] and short
                                                                                   and "$" not in md)) else "rm"
        b["has_x"] = re.search(r"[a-ząćęłńóśźż]", plain) is not None
        b["size"] = size_body
        hard = md.count("\\\n") + md.count("\\\\")
        b["single"] = (m["nlines"] <= 1 or b["fam"] == "sf" or (t == "Equation" and not hard)
                       or bool(b.get("force_single")))
        b["bold_label"] = bool(b["fam"] == "rm" and stroke_body and LABEL_RE.match(plain) is not None
                               and scan.first_word_bold(b["ink"], stroke_body, pitch_body, 1.12))
        b["indent"] = 0.0
        b["spread"] = bool(b["single"] and b["fam"] == "rm" and not b.get("cells")
                           and scan.max_gap(b["ink"]) > 1.2 * size_body)
        # tall lines with \lim / \frac / \sum ... -> the scan shows them in display style
        lh = (b["ink"][3] - b["ink"][1]) / max(1, m["nlines"])
        b["display"] = bool(re.search(r"\\(lim|frac|sum|int|prod|bigcup|bigcap|bigwedge|bigvee)", md)
                            and lh > 1.45 * size_body) or b["type"] == "Equation"
        if b["fam"] == "rm" and b["heavy"] and not b["bold_label"]:
            b["bold"] = True
        if b["fam"] == "rm" and t in TEXT_TYPES and m["xh"] and m["ncomp"] >= 40 and m["nlines"] >= 2 and b["has_x"]:
            sz = m["xh"] / XH["rm"]
            if abs(sz / size_body - 1) >= 0.15:     # footnotes / small print
                b["size"] = float(np.clip(sz, 0.5 * size_body, 2 * size_body))

    # sans blocks: weight class from stroke, font by comparing predicted and scanned width, size by x/cap height
    sfb = [b for k, b, _ in items if k == "txt" and b["fam"] == "sf" and b["m"]["xh"]]
    heavy = sum(b["heavy"] for b in sfb) >= 0.5 * len(sfb) if sfb else True
    # weight by direct evidence: measured stroke vs the stem predicted for regular / bold sans at the size that the
    # scanned width implies (x-heights of short headings are unreliable).  Scan strokes are ~1.07 x regular, ~0.85 x bold.
    fr, fbd = font_metrics("lmsans"), font_metrics("lmsansbold")
    sr, sb_ = stem_frac("lmsans"), stem_frac("lmsansbold")
    if fr and fbd and sr and sb_:
        votes = []
        for b in sfb:
            w = b["ink"][2] - b["ink"][0]
            if len(b["plain"]) < 4 or w < 15 or not b["m"]["stroke"] or b["m"]["nlines"] > 1 or re.fullmatch(r"[0-9.,\s]+", b["plain"]):
                continue
            szb = w / (fbd[0].getlength(b["plain"]) / 1000.0)
            szr = w / (fr[0].getlength(b["plain"]) / 1000.0)
            eb = abs(np.log(b["m"]["stroke"] / (0.85 * sb_ * szb)))
            er = abs(np.log(b["m"]["stroke"] / (1.07 * sr * szr)))
            votes.append(eb < er)
        if votes:
            heavy = sum(votes) >= 0.5 * len(votes)
    cands = [k for k in SF_FONTS if (k in SF_BOLD) == heavy]
    samples = [(b["plain"], b["ink"][2] - b["ink"][0], b["m"]["xh"], "x" if b["has_x"] else "H") for b in sfb
               if b["m"]["nlines"] <= 1 and len(b["plain"]) >= 2]
    sf_key, sf_err = choose_sf(samples, cands)
    info["sans_font"] = sf_key
    fm = font_metrics(sf_key)
    for b in sfb:
        ratio = (fm[1] if b["has_x"] else fm[2]) if fm else (XH["sf"] if b["has_x"] else DIGH["sf"])
        ref = b["m"]["xh"]
        if re.fullmatch(r"[0-9.,\s]+", b["plain"]) and b["m"]["bands"]:
            ref = max(bb1 - bb0 for bb0, bb1 in b["m"]["bands"])   # digits: whole glyph height
        b["size"] = float(np.clip(ref / ratio / sy, 0.5 * size_body, 6 * size_body))
    # page headers with page number: same font size for all header items
    hdr = [b for k, b, _ in items if k == "txt" and b["type"] == "PageHeader"]
    if len(hdr) >= 2:
        sizes = [b["size"] for b in hdr if b.get("has_x")]
        if sizes:
            for b in hdr:
                b["size"] = float(np.median(sizes))

    # batch pandoc
    txt_idx = [i for i, it in enumerate(items) if it[0] == "txt"]
    mds, slots = [], {}
    for i in txt_idx:
        b = items[i][1]
        parts = [c for _, c in b["cells"]] if b.get("cells") else [items[i][2]]
        slots[i] = []
        for md in parts:
            if b.get("bold_label"):
                md = LABEL_RE.sub(lambda mm: "**" + mm.group(1) + "**", md, count=1)
            slots[i].append(len(mds))
            mds.append(md)
    try:
        texs = md_batch_to_tex(mds) if mds else []
    except Exception as e:
        log(f"  page {n}: pandoc failed ({e}); raw text used")
        texs = [esc(m) for m in mds]
    tex_of = {}
    for i in txt_idx:
        b = items[i][1]
        ts = [texs[j] for j in slots[i]]
        if b.get("cells") and len(ts) > 1:
            xs_ = [cx for cx, _ in b["cells"]]
            ts = [rf"\pmcell{{{fmt(xs_[k + 1] - xs_[k])}}}{{{t}}}" for k, t in enumerate(ts[:-1])] + [ts[-1]]
        tex_of[i] = "".join(ts)

    # ------------- emit
    lines = []
    def add(cmd, src_idx=None):
        lines.append((cmd, src_idx))
    for sh in scan.shades:
        x0, y0, x1, y1 = sh["bbox"]
        add(rf"\pmshade{{{fmt(x0)}}}{{{fmt(y0)}}}{{{fmt(x1 - x0)}}}{{{fmt(y1 - y0)}}}"
            rf"{{{fmt(100 * sh['gray_left'])}}}{{{fmt(100 * sh['gray_right'])}}}")
    for fr in scan.frames:
        x0, y0, x1, y1 = fr["bbox"]
        t = fr["thick"]
        add(rf"\pmframe{{{fmt(x0 + t / 2)}}}{{{fmt(y0 + t / 2)}}}{{{fmt(x1 - x0 - t)}}}{{{fmt(y1 - y0 - t)}}}{{{fmt(t)}}}")
    for ru in scan.rules:
        x0, y0, x1, y1 = ru["bbox"]
        add(rf"\pmrule{{{fmt(x0)}}}{{{fmt(y0)}}}{{{fmt(x1 - x0)}}}{{{fmt(ru['thick'])}}}")
    for mk in markers:
        if mk["kind"] == "disc":
            add(rf"\pmdisc{{{fmt(mk['cx'])}}}{{{fmt(mk['cy'])}}}{{{fmt(mk['r'])}}}")
        else:
            add(rf"\pmring{{{fmt(mk['cx'])}}}{{{fmt(mk['cy'])}}}{{{fmt(mk['r'] - mk['thick'] / 2)}}}{{{fmt(mk['thick'])}}}")
    nimg = 0
    os.makedirs(img_dir, exist_ok=True)
    for old in os.listdir(img_dir):
        if old.startswith(f"p{n:04d}_L"):
            os.remove(os.path.join(img_dir, old))
    for i, (kind, b, md) in enumerate(items):
        x0, y0, x1, y1 = b["ink"]
        w, h = x1 - x0, y1 - y0
        if kind == "img":
            nimg += 1
            fn = f"p{n:04d}_L{nimg}_{b['type']}.png"
            cx0, cy0, cx1, cy1 = scan.crop(b["bbox"], os.path.join(img_dir, fn))
            add(rf"\pmimg{{{fmt(cx0)}}}{{{fmt(cy0)}}}{{{fmt(cx1 - cx0)}}}{{{fmt(cy1 - cy0)}}}{{img/{fn}}}", i)
        elif kind == "eq":
            em = eq_math(md)
            rows, mode = eq_rows(em)
            add(rf"\pmeq{{{fmt((x0 + x1) / 2)}}}{{{fmt((y0 + y1) / 2)}}}{{{fmt(w)}}}{{{fmt(h)}}}"
                rf"{{{fmt_size(size_body)}}}{{{rows}}}{{{mode}}}{{{em}}}", i)
        else:
            tex = tex_of.get(i, esc(md))
            style = rf"\pmsf{sf_key}" if b["fam"] == "sf" else r"\pmrm"
            if b.get("bold"):
                style += r"\bfseries"
            if b.get("display"):
                style += r"\everymath{\displaystyle}"
            bls = b["m"].get("baselines") or []
            if b["single"]:
                # main baseline: the core baseline nearest to the ink centre + x-height/2 (skips the
                # numerator/denominator lines of fractions)
                xh_b = b["m"]["xh"] or 0.45 * b["size"]
                target = (y0 + y1) / 2 + xh_b / 2
                base = b["base"] if b.get("base") else (min(bls, key=lambda v: abs(v - target)) if bls else target)
                macro = "pmspread" if b.get("spread") else "pmline"
                add(rf"\{macro}{{{fmt(x0)}}}{{{fmt(base)}}}{{{fmt(w + 0.5)}}}{{{fmt_size(b['size'])}}}{{{style}}}"
                    rf"{{{tex}}}", i)
            else:
                base = bls[0] if bls else y0 + 0.75 * b["size"]
                nl = len(bls) if len(bls) >= 2 else 0
                add(rf"\def\pmlines{{{nl}}}\pmpar{{{fmt(x0)}}}{{{fmt(base)}}}{{{fmt(w + 0.5)}}}{{{fmt(h / sy)}}}{{{fmt_size(b["size"])}}}"
                    rf"{{{fmt(bskip)}}}{{{style}}}{{{fmt(b['indent'])}}}{{{tex}}}", i)
    info["images"] = nimg

    def assemble(ls):
        body = "\n".join(c for c, _ in ls)
        return (f"% page {n} (pdfmaker --layout faithful)\n\\pmweight{{{fmt_fakebold(fb)}}}{{{fmt(stretch)}}}{{{fmt(sy)}}}\n"
                f"\\begin{{pmpage}}\n{body}\n\\end{{pmpage}}\n")

    # ------------- compile check with per-block fallback
    if work is not None:
        pre = preamble(pw, ph, engine)
        bad = set()
        for attempt in range(12):
            frag = assemble(lines)
            ok, errline = _compile(engine, pre, frag, work)
            if ok:
                break
            # map error line -> block line
            hdr_lines = pre.count("\n") + 1 + 3   # \begin{document} + comment + \pmweight + \begin{pmpage}
            idx = errline - hdr_lines - 1 if errline else None
            if idx is None or not (0 <= idx < len(lines)) or lines[idx][1] is None or idx in bad:
                # unknown culprit: bisect by compiling blocks one by one
                for j, (c, si) in enumerate(lines):
                    if si is None or j in bad:
                        continue
                    ok1, _ = _compile(engine, pre, assemble([(c, si)]), work)
                    if not ok1:
                        lines[j] = (_fallback(items[si], lines[j][0], repair_math), si)
                        bad.add(j)
                continue
            lines[idx] = (_fallback(items[lines[idx][1]], lines[idx][0], repair_math, first=idx not in bad), lines[idx][1])
            bad.add(idx)
        info["fallback_blocks"] = len(bad)
    return assemble(lines), info


def _fallback(item, cmd, repair_math, first=True):
    kind, b, md = item
    if first and repair_math and kind == "eq" and repair_math(md) != md:
        m = re.match(r"(\\pmeq(?:\{[^}]*\}){7})", cmd)
        return m.group(1) + "{" + eq_math(repair_math(md)) + "}"
    plain = esc(re.sub(r"\s+", " ", md))
    x0, y0, x1, y1 = b["ink"]
    return (rf"\pmpar{{{fmt(x0)}}}{{{fmt(y0 + 0.75 * b.get('size', 9))}}}{{{fmt(x1 - x0)}}}{{{fmt(y1 - y0)}}}{{{fmt_size(b.get('size', 9))}}}{{1.2}}"
            rf"{{\normalfont\color{{red!70!black}}}}{{0}}{{{plain}}}")


def _compile(engine, pre, frag, work):
    os.makedirs(work, exist_ok=True)
    tex = os.path.join(work, "lay.tex")
    with open(tex, "w", encoding="utf-8") as f:
        f.write(pre + "\\begin{document}\n" + frag + "\\end{document}\n")
    try:
        r = subprocess.run([engine, "-interaction=nonstopmode", "-halt-on-error", "-no-shell-escape", "lay.tex"],
                           cwd=work, capture_output=True, text=True, timeout=180)
    except subprocess.TimeoutExpired:
        return False, None
    if r.returncode == 0:
        return True, None
    m = re.search(r"^l\.(\d+)", r.stdout, re.M)
    return False, int(m.group(1)) if m else None
