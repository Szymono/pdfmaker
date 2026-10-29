"""Visual similarity of an output page vs the scanned original (pdfmaker --compare).

Metrics (both pages rendered at the same dpi, grayscale, same size):
  ssim_blur   SSIM of Gaussian-blurred (sigma = 1.5 pt) images: layout/texture similarity incl. grey boxes
  ink_iou     IoU of ink masks after dilation by ~1.5 pt (where is ink at all)
  ink_f1      F1 of ink with ~1.5 pt tolerance (precision = output ink near original ink, recall = vice versa)
  rows_corr   correlation of horizontal ink profiles (vertical position of lines/paragraphs)
  cols_corr   correlation of vertical ink profiles (horizontal extent / indentation)
Writes <name>_side.png (original | output) and <name>_overlay.png (original red, output blue, both black).
"""
import os, subprocess, tempfile
import numpy as np
import cv2

def render(pdf, page, dpi):
    with tempfile.TemporaryDirectory() as td:
        b = os.path.join(td, "r")
        subprocess.run(["pdftoppm", "-f", str(page), "-l", str(page), "-r", str(dpi), "-gray", "-png",
                        "-singlefile", str(pdf), b], check=True, capture_output=True)
        return cv2.imread(b + ".png", cv2.IMREAD_GRAYSCALE)

def _ssim(a, b):
    try:
        from skimage.metrics import structural_similarity
        return float(structural_similarity(a, b, data_range=255))
    except Exception:
        a, b = a.astype(np.float64), b.astype(np.float64)
        C1, C2 = (0.01 * 255) ** 2, (0.03 * 255) ** 2
        mu1, mu2 = cv2.GaussianBlur(a, (11, 11), 1.5), cv2.GaussianBlur(b, (11, 11), 1.5)
        s1 = cv2.GaussianBlur(a * a, (11, 11), 1.5) - mu1 ** 2
        s2 = cv2.GaussianBlur(b * b, (11, 11), 1.5) - mu2 ** 2
        s12 = cv2.GaussianBlur(a * b, (11, 11), 1.5) - mu1 * mu2
        m = ((2 * mu1 * mu2 + C1) * (2 * s12 + C2)) / ((mu1 ** 2 + mu2 ** 2 + C1) * (s1 + s2 + C2))
        return float(m.mean())

def _corr(x, y):
    x, y = x - x.mean(), y - y.mean()
    d = np.sqrt((x * x).sum() * (y * y).sum())
    return float((x * y).sum() / d) if d > 0 else 0.0

def compare(orig_pdf, orig_page, out_pdf, out_page, outdir, name, dpi=100):
    a = render(orig_pdf, orig_page, dpi)
    b = render(out_pdf, out_page, dpi)
    if b.shape != a.shape:
        b = cv2.resize(b, (a.shape[1], a.shape[0]), interpolation=cv2.INTER_AREA)
    s = dpi / 72.0
    sig = 1.5 * s
    ab, bb = cv2.GaussianBlur(a, (0, 0), sig), cv2.GaussianBlur(b, (0, 0), sig)
    ssim = _ssim(ab, bb)
    ia, ib = (a < 140).astype(np.uint8), (b < 140).astype(np.uint8)
    k = np.ones((int(3 * s) | 1, int(3 * s) | 1), np.uint8)
    da, db = cv2.dilate(ia, k), cv2.dilate(ib, k)
    iou = float((da & db).sum() / max(1, (da | db).sum()))
    prec = float((ib & da).sum() / max(1, ib.sum()))
    rec = float((ia & db).sum() / max(1, ia.sum()))
    f1 = 2 * prec * rec / max(1e-9, prec + rec)
    box = int(2 * s) + 1
    sm = lambda v: np.convolve(v.astype(np.float32), np.ones(box) / box, "same")
    rows = _corr(sm((255 - ab.astype(np.float32)).sum(1)), sm((255 - bb.astype(np.float32)).sum(1)))
    cols = _corr(sm((255 - ab.astype(np.float32)).sum(0)), sm((255 - bb.astype(np.float32)).sum(0)))
    os.makedirs(outdir, exist_ok=True)
    gap = np.full((a.shape[0], 12), 128, np.uint8)
    cv2.imwrite(os.path.join(outdir, f"{name}_side.png"), np.hstack([a, gap, b]))
    oa, ob = (255 - a).astype(np.float32) / 255, (255 - b).astype(np.float32) / 255
    over = np.stack([255 * (1 - oa),                        # B: low where original ink  (BGR order!)
                     255 * (1 - np.maximum(oa, ob)),        # G
                     255 * (1 - ob)], axis=2)               # R: low where output ink
    cv2.imwrite(os.path.join(outdir, f"{name}_overlay.png"), np.clip(over, 0, 255).astype(np.uint8))
    return {"ssim_blur": round(ssim, 4), "ink_iou": round(iou, 4), "ink_f1": round(f1, 4),
            "ink_precision": round(prec, 4), "ink_recall": round(rec, 4),
            "rows_corr": round(rows, 4), "cols_corr": round(cols, 4)}
