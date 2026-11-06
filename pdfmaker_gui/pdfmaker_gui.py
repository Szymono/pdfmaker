#!/usr/bin/env python3
"""pdfmaker GUI – okienkowa nakładka na skrypt pdfmaker (convert.sh).

Osobny program: nie zawiera logiki OCR, tylko uruchamia ../pdfmaker/convert.sh
(lub folder wskazany w ustawieniach) z wybranymi opcjami.  Dostępne opcje
odczytuje (w tle) z `convert.py --help`, więc nowe flagi skryptu pojawiają się
w oknie automatycznie.

Wygląd: zwykły Tk/ttk; jeśli zainstalowany jest pakiet `ttkbootstrap`, używa jego
motywów (jasny/ciemny).  Bez ttkbootstrap GUI działa na zwykłym ttk.

Uruchomienie:  ./gui.sh   (albo:  python3 pdfmaker_gui.py [plik.pdf])
"""
from __future__ import annotations

import json
import os
import queue
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import warnings
from dataclasses import dataclass, field
from pathlib import Path

HERE = Path(__file__).resolve().parent
CONFIG = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "pdfmaker_gui.json"

MAX_UI_LOG_LINES = 4000        # dziennik w oknie (pełny log jest w pliku w folderze roboczym)
POLL_MS = 150
MAX_EVENTS_PER_POLL = 3000
IDLE_WARN_SEC = 600            # tylko informacja – zadanie NIE jest przerywane automatycznie

# --------------------------------------------------------------------------- #
#  Backend (bez Tk – można testować bez okna)
# --------------------------------------------------------------------------- #


def atomic_write_text(path: Path, text: str) -> None:
    """Zapis atomowy: plik tymczasowy w tym samym folderze + os.replace."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix="." + path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def atomic_copy(src: Path, dst: Path) -> None:
    """Kopiuje src -> dst przez plik tymczasowy; istniejący dst jest podmieniany dopiero na końcu."""
    dst = Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix="." + dst.name + ".", suffix=".tmp", dir=str(dst.parent))
    os.close(fd)
    try:
        shutil.copy2(src, tmp)
        os.replace(tmp, dst)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def load_config() -> dict:
    try:
        d = json.loads(CONFIG.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def save_config(cfg: dict) -> bool:
    try:
        atomic_write_text(CONFIG, json.dumps(cfg, indent=1, ensure_ascii=False))
        return True
    except Exception:
        return False


def is_script_dir(p) -> bool:
    return bool(p) and (Path(p) / "convert.sh").is_file()


def find_script_dir(cfg: dict | None = None) -> Path | None:
    """Szuka folderu pdfmaker: ustawienia -> $PDFMAKER_DIR -> ../pdfmaker -> ten folder -> ~/pdfmaker."""
    cands = []
    if cfg and cfg.get("script_dir"):
        cands.append(cfg["script_dir"])
    if os.environ.get("PDFMAKER_DIR"):
        cands.append(os.environ["PDFMAKER_DIR"])
    cands += [HERE.parent / "pdfmaker", HERE, Path.home() / "pdfmaker"]
    for c in cands:
        if is_script_dir(c):
            return Path(c).resolve()
    return None


@dataclass
class Opt:
    name: str                 # bez "--", np. "layout"
    kind: str                 # "flag" | "choice" | "value"
    choices: list = field(default_factory=list)
    metavar: str = ""
    help: str = ""


_OPT_RE = re.compile(r"^\s{1,6}(?:-\w(?: \S+)?, )?--([\w-]+)(?: (\{[^}]*\}|[A-Z_][A-Z0-9_]*))?(?:\s{2,}(.*))?$")


def parse_help(text: str) -> list[Opt]:
    """Parsuje wyjście argparse --help (sekcja opcji)."""
    opts, cur, in_opts = [], None, False
    for line in text.splitlines():
        if re.match(r"^(options|optional arguments):", line):
            in_opts = True
            continue
        if not in_opts:
            continue
        if line and not line.startswith(" "):      # kolejna sekcja
            in_opts = line.endswith(":") and "option" in line.lower()
            cur = None
            continue
        m = _OPT_RE.match(line)
        if m:
            name, arg, hlp = m.group(1), m.group(2), (m.group(3) or "").strip()
            if arg and arg.startswith("{"):
                o = Opt(name, "choice", [c.strip() for c in arg[1:-1].split(",") if c.strip()], help=hlp)
            elif arg:
                o = Opt(name, "value", metavar=arg, help=hlp)
            else:
                o = Opt(name, "flag", help=hlp)
            opts.append(o)
            cur = o
        elif cur is not None and line.strip():
            cur.help = (cur.help + " " + line.strip()).strip()
    return opts


def script_python(script_dir: Path) -> list[str]:
    vpy = script_dir / "venv" / "bin" / "python"
    return [str(vpy)] if vpy.exists() else [sys.executable]


FALLBACK_OPTS = [Opt("pages", "value"), Opt("mode", "choice", ["math", "text"]),
                 Opt("engine", "choice", ["xelatex", "lualatex", "pdflatex"]),
                 Opt("newpage", "flag"), Opt("no-images", "flag"), Opt("force", "flag")]


def read_script_options(script_dir: Path) -> tuple[list[Opt], str]:
    """Zwraca (opcje, surowy tekst --help).  Najpierw convert.py, potem convert.sh.  (Blokujące – wołać w wątku.)"""
    tries = []
    if (script_dir / "convert.py").exists():
        tries.append(script_python(script_dir) + [str(script_dir / "convert.py"), "--help"])
    tries.append(["bash", str(script_dir / "convert.sh"), "--help"])
    for cmd in tries:
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=60, cwd=script_dir)
            opts = parse_help(r.stdout)
            if opts:
                return opts, r.stdout
        except Exception:
            continue
    return [], ""


def pdf_page_count(pdf: Path) -> int | None:
    """Liczba stron (pdfinfo, awaryjnie pikepdf).  Blokujące – w GUI wołać w wątku."""
    try:
        r = subprocess.run(["pdfinfo", str(pdf)], capture_output=True, text=True, timeout=30)
        m = re.search(r"^Pages:\s+(\d+)", r.stdout, re.M)
        if m:
            return int(m.group(1))
    except Exception:
        pass
    try:  # awaryjnie pikepdf, jeśli jest
        import pikepdf  # type: ignore
        with pikepdf.open(pdf) as p:
            return len(p.pages)
    except Exception:
        return None


def parse_pages(spec: str, total: int) -> list[int]:
    """Jak w convert.py: '10-20' lub '1,3,5-7' (od 1); puste = wszystkie."""
    spec = (spec or "").strip()
    if not spec:
        return list(range(1, total + 1))
    out = set()
    for part in spec.replace(" ", "").split(","):
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            a = int(a) if a else 1
            b = int(b) if b else total
            out.update(range(a, min(b, total) + 1))
        else:
            out.add(int(part))
    return sorted(n for n in out if 1 <= n <= total)


def valid_pages_spec(spec: str) -> bool:
    """Składnia zakresu: '10-20', '1,3,5-7', '-5', '400-'; puste = wszystkie."""
    spec = (spec or "").strip()
    if not spec:
        return True
    parts = [p.strip() for p in spec.split(",")]
    for p in parts:
        if not re.fullmatch(r"\d+|\d*-\d*", p) or p == "-":
            return False
        if "-" in p:
            a, b = p.split("-", 1)
            if a and b and int(a) > int(b):
                return False
        if p.isdigit() and int(p) < 1:
            return False
    return True


def work_dir_for(output: Path) -> Path:
    """Folder roboczy obok pliku wynikowego: <wynik>_praca/ (wznawianie działa przy tej samej ścieżce)."""
    return output.with_name(output.stem + "_praca")


def searchable_for(output: Path) -> Path:
    return output.with_name(output.stem + "_przeszukiwalny.pdf")


def _rp(p) -> Path:
    return Path(p).expanduser().resolve()


def same_path(a, b) -> bool:
    try:
        if Path(a).exists() and Path(b).exists():
            return os.path.samefile(a, b)
    except OSError:
        pass
    return _rp(a) == _rp(b)


def is_within(child, parent) -> bool:
    """child == parent albo child leży wewnątrz parent (po resolve())."""
    c, p = _rp(child), _rp(parent)
    return c == p or p in c.parents


def check_paths(input_pdf, output, script_dir=None) -> list[str]:
    """Zwraca listę błędów konfiguracji ścieżek (pusta = OK).  Nie dotyka plików poza odczytem stanu."""
    errs: list[str] = []
    if not input_pdf:
        return ["Nie wybrano pliku źródłowego."]
    if not output:
        return ["Nie wybrano pliku wynikowego."]
    inp, out = _rp(input_pdf), _rp(output)
    wd = work_dir_for(out)
    if not inp.is_file():
        errs.append(f"Plik źródłowy nie istnieje: {inp}")
    if out.suffix.lower() != ".pdf":
        errs.append("Plik wynikowy musi mieć rozszerzenie .pdf.")
    if out.is_dir():
        errs.append(f"Ścieżka wyniku jest folderem: {out}")
    if same_path(inp, out):
        errs.append("Plik wynikowy jest tym samym plikiem co źródło – źródło zostałoby nadpisane. Wybierz inną nazwę.")
    if same_path(inp, searchable_for(out)):
        errs.append("Dodatkowy plik „…_przeszukiwalny.pdf” nadpisałby źródło. Wybierz inną nazwę wyniku.")
    if is_within(inp, wd):
        errs.append(f"Plik źródłowy leży w folderze roboczym tego wyniku ({wd}). Wybierz inną nazwę wyniku.")
    if is_within(out, wd):
        errs.append(f"Plik wynikowy nie może leżeć w swoim folderze roboczym ({wd}).")
    if wd.exists() and not wd.is_dir():
        errs.append(f"Folder roboczy {wd} istnieje, ale jest plikiem.")
    if script_dir:
        sd = _rp(script_dir)
        if is_within(sd, wd):
            errs.append("Folder roboczy obejmowałby folder skryptu pdfmaker. Wybierz inne miejsce zapisu.")
        if wd == sd or is_within(wd, sd / "venv") or is_within(wd, sd / "lib"):
            errs.append("Folder roboczy nie może leżeć w folderze programu (venv/lib skryptu).")
    return errs


def is_faithful(values: dict) -> bool:
    return values.get("layout") in ("faithful", True)


def option_disabled_reason(name: str, values: dict) -> str | None:
    """Czy opcja ma sens przy aktualnym trybie (wg semantyki convert.py).  None = aktywna."""
    mode = values.get("mode") or "math"
    faithful = is_faithful(values)
    if name == "layout" and mode == "text":
        return "Wierny układ wymaga trybu „Matematyka / LaTeX” (pozycje bloków daje Marker)."
    if name in ("compare", "fakebold") and not faithful:
        return "Działa tylko przy układzie „Wierny układ strony”."
    if name == "newpage" and faithful:
        return "W wiernym układzie każda strona skanu i tak jest osobną stroną."
    if name == "no-images" and mode == "text":
        return "Tryb „Sam tekst” nie wycina ilustracji (dotyczy tylko Markera)."
    return None


def validate_values(values: dict, total_pages: int | None = None) -> list[str]:
    errs = []
    spec = str(values.get("pages") or "")
    if not valid_pages_spec(spec):
        errs.append("Błędny zakres stron (np. 10-20 albo 1,3,5-7).")
    elif total_pages and spec.strip() and not parse_pages(spec, total_pages):
        errs.append(f"Zakres stron nie obejmuje żadnej strony (dokument ma {total_pages} stron).")
    dpi = str(values.get("dpi") or "").strip()
    if dpi:
        if not dpi.isdigit() or not (50 <= int(dpi) <= 1200):
            errs.append("DPI musi być liczbą całkowitą 50–1200 (puste = 300).")
    fb = str(values.get("fakebold") or "").strip()
    if fb and fb != "auto":
        try:
            v = float(fb.replace(",", "."))
            if not (0 <= v <= 10):
                raise ValueError
        except ValueError:
            errs.append("Pogrubienie: „auto”, 0 albo liczba ≥ 0 (np. 0.3).")
    if is_faithful(values) and (values.get("mode") or "math") != "math":
        errs.append("Wierny układ strony wymaga trybu „Matematyka / LaTeX”.")
    return errs


def build_command(script_dir: Path, input_pdf: Path, workdir: Path, values: dict, opts: list[Opt]) -> list[str]:
    """values: {nazwa_opcji: wartość}; flagi -> bool, reszta -> str (puste = domyślne skryptu)."""
    known = {o.name: o for o in opts}
    cmd = ["bash", str(script_dir / "convert.sh"), str(input_pdf), "--out", str(workdir)]
    for name, val in values.items():
        o = known.get(name)
        if o is None or name in ("out", "help"):
            continue
        if o.kind == "flag":
            if val:
                cmd.append("--" + name)
        else:
            val = str(val).strip()
            if name == "fakebold":
                val = val.replace(",", ".")
            if val:
                if o.kind == "choice" and o.choices and val not in o.choices:
                    continue
                cmd += ["--" + name, val]
    return cmd


def read_report(workdir: Path) -> dict | None:
    try:
        return json.loads((workdir / "report.json").read_text(encoding="utf-8"))
    except Exception:
        return None


def tex_counts(rep: dict | None, pages: list[int] | None = None) -> dict:
    counts: dict = {}
    for k, v in (rep or {}).items():
        if k.isdigit() and isinstance(v, dict) and (not pages or int(k) in pages):
            st = str(v.get("tex", "brak")).split()[0].split("(")[0]
            counts[st] = counts.get(st, 0) + 1
    return counts


def read_summary(workdir: Path, pages: list[int] | None = None) -> dict | None:
    """_summary z report.json + zliczenie statusów 'tex' (dla stron z tego zadania, jeśli podane)."""
    rep = read_report(workdir)
    if not rep:
        return None
    s = dict(rep.get("_summary") or {})
    s["_tex_counts"] = tex_counts(rep, pages)
    return s


TEX_STATUS_PL = {"ok": "OK", "partial": "częściowo", "verbatim": "jako surowy tekst (verbatim)",
                 "text": "tekst", "missing": "brak OCR", "failed": "błąd", "brak": "brak statusu"}


def summary_text(s: dict | None) -> str:
    if not s:
        return "Brak raportu (report.json)."
    counts = s.get("_tex_counts") or {}
    if counts:
        parts = [f"stron: {sum(counts.values())}"]
        order = ["ok", "text", "partial", "verbatim", "missing", "failed"]
        for k in order + sorted(set(counts) - set(order)):
            if counts.get(k):
                parts.append(f"{TEX_STATUS_PL.get(k, k)}: {counts[k]}")
    else:
        parts = [f"stron: {s.get('pages', '?')}", f"OK: {s.get('tex_ok', 0)}",
                 f"częściowo: {s.get('partial', 0)}", f"verbatim: {s.get('verbatim', 0)}"]
    if s.get("ocr_failed"):
        parts.append(f"błędy OCR: {s['ocr_failed']}")
    if s.get("images"):
        parts.append(f"obrazy: {s['images']}")
    if s.get("images_missing"):
        parts.append(f"brakujące obrazy: {s['images_missing']}")
    return ", ".join(parts)


def fmt_dur(sec: float) -> str:
    sec = int(max(0, sec))
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h} h {m:02d} min"
    if m:
        return f"{m} min {s:02d} s"
    return f"{s} s"


# ---- fazy pipeline'u odczytywane z dziennika convert.py ----
PHASE_LABELS = {
    "start": "Przygotowanie",
    "ocr": "OCR",
    "tex": "Skład stron (LaTeX)",
    "compile": "Kompilacja PDF",
    "merge": "Scalanie części",
    "compare": "Porównanie ze skanem",
    "report": "Raport",
}

_PAGE_DONE_RE = re.compile(r"\bpage (\d+): (?:OK|FAILED|tesseract OK|tesseract FAILED)")
_TEX_PAGE_RE = re.compile(r"\bpage (\d+): (?:faithful layout|reusing existing page tex|tex status)")
_CHUNK_RE = re.compile(r"\bchunk (\d+)/(\d+):")


def detect_phase(line: str) -> tuple[str, dict] | None:
    """Rozpoznaje fazę z linii logu convert.py.  Zwraca (faza, dane) albo None."""
    if "Marker OCR on" in line:
        m = re.search(r"Marker OCR on (\d+) page", line)
        return "ocr", {"engine": "Marker", "todo": int(m.group(1)) if m else None}
    m = _PAGE_DONE_RE.search(line)
    if m:
        return "ocr", {"page": int(m.group(1)), "engine": "Tesseract" if "tesseract" in line else "Marker"}
    m = _TEX_PAGE_RE.search(line)
    if m:
        return "tex", {"page": int(m.group(1))}
    m = _CHUNK_RE.search(line)
    if m:
        return "compile", {"chunk": int(m.group(1)), "chunks": int(m.group(2))}
    if "Compiling" in line:
        return "compile", {}
    if re.search(r"\bMerged \d+ chunks", line):
        return "merge", {"finished": True}
    if "Comparison images in" in line:
        return "compare", {"finished": True}
    if line.lstrip("[0-9:] ").startswith("Summary:") or "] Summary:" in line:
        return "report", {}
    return None


def inspect_outputs(workdir: Path, t0: float, pages: list[int] | None = None) -> dict:
    """Fakty o wynikach w folderze roboczym (do bramki sukcesu).  Blokujące (pdfinfo)."""
    book = workdir / "book.pdf"
    f = {"book_exists": False, "book_fresh": False, "book_size": 0, "book_pages": None,
         "report_fresh": False, "tex_counts": {}, "ocr_failed": 0, "images_missing": 0}
    try:
        st = book.stat()
        f.update(book_exists=True, book_fresh=st.st_mtime >= t0 - 1, book_size=st.st_size)
    except OSError:
        pass
    if f["book_exists"] and f["book_fresh"] and f["book_size"] > 0:
        f["book_pages"] = pdf_page_count(book)
    rp = workdir / "report.json"
    try:
        f["report_fresh"] = rp.stat().st_mtime >= t0 - 1
    except OSError:
        pass
    rep = read_report(workdir) if f["report_fresh"] else None
    if rep:
        f["tex_counts"] = tex_counts(rep, pages)
        for k, v in rep.items():
            if k.isdigit() and isinstance(v, dict) and (not pages or int(k) in pages):
                if v.get("ocr") == "failed":
                    f["ocr_failed"] += 1
                f["images_missing"] += len(v.get("images_missing", []) or [])
    return f


STATE_LABELS = {"ok": "Ukończono", "warn": "Ukończono z ostrzeżeniami",
                "error": "Błąd", "cancelled": "Anulowano"}


def evaluate_result(rc: int | None, stopping: bool, facts: dict, n_pages: int = 0,
                    faithful: bool = False, log_errors: int = 0) -> tuple[str, list[str]]:
    """Bramka sukcesu.  'ok'/'warn' tylko gdy rc == 0 ORAZ powstał świeży, niepusty book.pdf."""
    if stopping:
        return "cancelled", ["Zatrzymane przez użytkownika. Ponowny Start z tym samym wynikiem wznowi pracę."]
    errs, warns = [], []
    if rc is None:
        errs.append("Proces skryptu nie został uruchomiony.")
    elif rc != 0:
        errs.append(f"Skrypt zakończył się kodem {rc} (≠ 0).")
    if not facts.get("book_exists"):
        errs.append("Nie powstał book.pdf w folderze roboczym.")
    elif not facts.get("book_fresh"):
        errs.append("book.pdf pochodzi z wcześniejszego uruchomienia – w tym przebiegu nie powstał nowy.")
    elif not facts.get("book_size"):
        errs.append("book.pdf jest pusty.")
    else:
        bp = facts.get("book_pages")
        if bp is None:
            warns.append("Nie udało się sprawdzić liczby stron book.pdf (brak pdfinfo?).")
        elif bp < 1:
            errs.append("book.pdf nie ma żadnej strony.")
        elif faithful and n_pages and bp != n_pages:
            warns.append(f"book.pdf ma {bp} stron, a przetwarzano {n_pages} (wierny układ powinien dać 1:1).")
    if errs:
        if rc not in (None, 0) and facts.get("book_fresh"):
            errs.append("Świeży book.pdf istnieje, ale przy kodzie ≠ 0 nie jest uznany za wynik (zostaje w folderze roboczym).")
        return "error", errs + warns
    if not facts.get("report_fresh"):
        warns.append("report.json nie został zaktualizowany w tym przebiegu.")
    else:
        c = facts.get("tex_counts") or {}
        bad = {k: c.get(k, 0) for k in ("partial", "verbatim", "missing", "failed") if c.get(k)}
        if bad:
            warns.append("Strony z problemami: " + ", ".join(f"{TEX_STATUS_PL.get(k, k)}: {v}" for k, v in bad.items()))
        if facts.get("ocr_failed"):
            warns.append(f"Błędy OCR: {facts['ocr_failed']} str.")
        if facts.get("images_missing"):
            warns.append(f"Brakujące obrazy: {facts['images_missing']}.")
    if log_errors:
        warns.append(f"Dziennik zawiera {log_errors} komunikat(ów) ERROR/Traceback.")
    return ("warn" if warns else "ok"), warns


class Job(threading.Thread):
    """Uruchamia convert.sh w osobnej grupie procesów.  Parametry są zamrożone w chwili Start
    (zmiany w oknie nie wpływają na działające zadanie).  Zdarzenia w kolejce:
       ("log", str) ("logfile", ścieżka) ("progress", dict)
       ("done", stan, rc, wynik|None, summary|None, powody, ścieżka_logu)   – ZAWSZE dokładnie raz.
    """

    def __init__(self, script_dir: Path, input_pdf: Path, output: Path, values: dict,
                 opts: list[Opt], events: queue.Queue, total_pages: int | None = None):
        super().__init__(daemon=True)
        self.script_dir, self.input_pdf, self.output = Path(script_dir), Path(input_pdf), Path(output)
        self.values = dict(values)
        self.opts = list(opts)
        self.q = events
        self.workdir = work_dir_for(self.output)
        self.searchable = searchable_for(self.output)
        self.proc: subprocess.Popen | None = None
        self.stopping = False
        self._lock = threading.Lock()
        self.total_pages = total_pages
        self.pages: list[int] = []
        self.done_pages: set[int] = set()
        self.tex_pages: set[int] = set()
        self.phase = "start"
        self.phase_info: dict = {}
        self.t0 = time.time()
        self.logfile = self.workdir / time.strftime("pdfmaker_gui_%Y%m%d_%H%M%S.log")
        self._lf = None
        self.log_errors = 0
        self.last_line_t = time.time()
        self.rc: int | None = None
        self.final = ("error", None, None, None, ["Zadanie zakończyło się bez wyniku (nieoczekiwanie)."])

    # -- dziennik: do pliku (pełny) + do okna --
    def _log(self, s: str):
        if self._lf:
            try:
                self._lf.write(s)
            except Exception:
                pass
        self.q.put(("log", s))

    def _scan_files(self, since: float | None) -> set[int]:
        """Strony z gotowym wynikiem OCR (pages/page_NNNN.(md|txt|json))."""
        pdir = self.workdir / "pages"
        found = set()
        exts = (".json",) if is_faithful(self.values) else (".md", ".txt")
        for n in self.pages:
            for ext in exts:
                f = pdir / f"page_{n:04d}{ext}"
                try:
                    if f.exists() and (since is None or f.stat().st_mtime >= since):
                        found.add(n)
                        break
                except OSError:
                    pass
        return found

    def _scan_compare(self) -> int:
        cdir = self.workdir / "compare"
        try:
            return sum(1 for f in cdir.glob("page_*_side.png") if f.stat().st_mtime >= self.t0 - 1)
        except OSError:
            return 0

    def command(self) -> list[str]:
        return build_command(self.script_dir, self.input_pdf, self.workdir, self.values, self.opts)

    def run(self):
        try:
            self._run()
        except Exception:
            self._log("\n[GUI] Wyjątek w wątku zadania:\n" + traceback.format_exc())
            self.final = ("error", self.rc, None, None,
                          ["Wyjątek w GUI podczas zadania – szczegóły (traceback) w dzienniku."])
        finally:
            if self._lf:
                try:
                    self._lf.write(f"\n[GUI] Stan końcowy: {STATE_LABELS.get(self.final[0], self.final[0])}\n")
                    for r in self.final[4] or []:
                        self._lf.write(f"[GUI]  - {r}\n")
                    self._lf.close()
                except Exception:
                    pass
            self.q.put(("done",) + tuple(self.final) + (str(self.logfile) if self.logfile.exists() else None,))

    def _progress(self, eta_new: int, t_ocr0: float | None):
        total = len(self.pages)
        d = {"phase": self.phase, "label": PHASE_LABELS.get(self.phase, self.phase),
             "done": None, "total": None, "eta": None, "eta_uncertain": True,
             "idle": time.time() - self.last_line_t, "detail": ""}
        if self.phase == "ocr" and total:
            d.update(done=len(self.done_pages), total=total)
            eng = self.phase_info.get("engine")
            if eng:
                d["label"] = f"OCR ({eng})"
            if eta_new > 0 and t_ocr0 and len(self.done_pages) < total:
                d["eta"] = (time.time() - t_ocr0) / eta_new * (total - len(self.done_pages))
                d["eta_uncertain"] = eta_new < 3
        elif self.phase == "tex" and total:
            if is_faithful(self.values):
                d.update(done=len(self.tex_pages), total=total)
            elif self.tex_pages:
                d["detail"] = f"ostatnio: strona {max(self.tex_pages)}"
        elif self.phase == "compile" and self.phase_info.get("chunks"):
            d.update(done=self.phase_info["chunk"] - 1, total=self.phase_info["chunks"])
            d["detail"] = f"część {self.phase_info['chunk']}/{self.phase_info['chunks']}"
        elif self.phase == "compare" and total:
            d.update(done=min(self._scan_compare(), total), total=total)
        self.q.put(("progress", d))

    def _run(self):
        self.workdir.mkdir(parents=True, exist_ok=True)
        self._lf = open(self.logfile, "w", encoding="utf-8", buffering=1)
        self.q.put(("logfile", str(self.logfile)))
        cmd = self.command()
        self._log("$ " + " ".join(_shq(c) for c in cmd) + "\n")
        self._log(f"[GUI] Pełny dziennik: {self.logfile}\n")
        total = self.total_pages or pdf_page_count(self.input_pdf) or 0
        self.pages = parse_pages(self.values.get("pages", ""), total) if total else []
        force = bool(self.values.get("force"))
        self.t0 = t0 = time.time()
        cached = set() if force else self._scan_files(None)
        if self.values.get("skip-ocr"):
            cached = set(self.pages)
        self.done_pages |= cached
        n_cached = len(cached)
        env = dict(os.environ, PYTHONUNBUFFERED="1")
        with self._lock:     # Stop przed Popen: nie uruchamiaj procesu
            if self.stopping:
                self._log("[GUI] Zatrzymano przed uruchomieniem skryptu.\n")
                self.final = ("cancelled", None, None, None, ["Zatrzymano przed uruchomieniem skryptu."])
                return
            try:
                self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                             stdin=subprocess.DEVNULL, text=True, bufsize=1,
                                             cwd=str(self.script_dir), env=env, start_new_session=True,
                                             errors="replace")
            except Exception as e:
                self._log(f"[GUI] Nie udało się uruchomić skryptu: {e}\n")
                self.final = ("error", None, None, None, [f"Nie udało się uruchomić skryptu: {e}"])
                return
        self.phase = "start" if not self.values.get("skip-ocr") else "tex"
        last_scan = 0.0
        stop_reader = threading.Event()
        lines: queue.Queue = queue.Queue()
        proc = self.proc

        def reader():
            try:
                for line in proc.stdout:
                    lines.put(line)
            finally:
                stop_reader.set()
        threading.Thread(target=reader, daemon=True).start()
        t_ocr0: float | None = None

        while True:
            try:
                line = lines.get(timeout=0.5)
            except queue.Empty:
                line = None
            changed = False
            if line is not None:
                self.last_line_t = time.time()
                self._log(line)
                if "ERROR" in line or "Traceback (most recent call last)" in line:
                    self.log_errors += 1
                ph = detect_phase(line)
                if ph:
                    name, info = ph
                    if name == "ocr":
                        if t_ocr0 is None:
                            t_ocr0 = time.time()
                        if "page" in info:
                            self.done_pages.add(info["page"])
                    if name == "tex" and "page" in info:
                        self.tex_pages.add(info["page"])
                    if name != self.phase:
                        self.phase_info = {}
                    self.phase = name
                    self.phase_info.update({k: v for k, v in info.items() if k != "page"})
                    changed = True
            if time.time() - last_scan > 2:
                last_scan = time.time()
                before = len(self.done_pages)
                self.done_pages |= self._scan_files(None if not force else t0)
                if self.phase in ("start", "ocr") and len(self.done_pages) > before and t_ocr0 is None:
                    t_ocr0 = time.time()
                if self.phase == "compile" and self.values.get("compare") and self._scan_compare():
                    self.phase, self.phase_info = "compare", {}
                changed = True
            if changed:
                self._progress(len(self.done_pages) - n_cached, t_ocr0)
            if stop_reader.is_set() and lines.empty():
                break
        self.rc = rc = proc.wait()
        self._log(f"\n[GUI] Skrypt zakończył się kodem {rc}.\n")
        facts = inspect_outputs(self.workdir, t0, self.pages)
        state, reasons = evaluate_result(rc, self.stopping, facts, len(self.pages),
                                         is_faithful(self.values), self.log_errors)
        result = None
        if state in ("ok", "warn"):
            try:
                atomic_copy(self.workdir / "book.pdf", self.output)
                result = self.output
                self._log(f"[GUI] Zapisano: {self.output}\n")
                sp = self.workdir / "searchable.pdf"
                if sp.exists() and sp.stat().st_mtime >= t0 - 1:
                    atomic_copy(sp, self.searchable)
                    self._log(f"[GUI] Skopiowano też przeszukiwalny skan: {self.searchable}\n")
            except Exception as e:
                state = "error"
                reasons = [f"Nie udało się skopiować wyniku do {self.output}: {e}"] + reasons
        self.final = (state, rc, result, read_summary(self.workdir, self.pages), reasons)

    def stop(self):
        """Czysto zatrzymuje całą grupę procesów (convert.py, marker_worker, llama-server).  Nie blokuje."""
        with self._lock:
            self.stopping = True
            p = self.proc
        if not p or p.poll() is not None:
            return
        threading.Thread(target=self._kill_tree, args=(p,), daemon=True).start()

    def _kill_tree(self, p):
        # zapamiętaj całe drzewo potomków (także procesy, które same zmieniły sesję, np. llama-server)
        tree = _descendants(p.pid)
        try:
            pgid = os.getpgid(p.pid)
        except ProcessLookupError:
            pgid = None
        # SIGINT -> convert.py wykona 'finally' (zatrzyma marker_worker i llama-server)
        for sig, wait in ((signal.SIGINT, 30), (signal.SIGTERM, 10), (signal.SIGKILL, 3)):
            if pgid is None:
                break
            try:
                os.killpg(pgid, sig)
            except ProcessLookupError:
                break
            try:
                p.wait(timeout=wait)
            except subprocess.TimeoutExpired:
                continue
            deadline = time.time() + wait
            while time.time() < deadline:
                try:
                    os.killpg(pgid, 0)   # ktoś z grupy jeszcze żyje?
                except ProcessLookupError:
                    break
                time.sleep(0.5)
            else:
                continue
            break
        # dobij tylko NASZE procesy, które przetrwały (nie rusza innych uruchomień skryptu)
        tree |= _descendants(p.pid)
        for pid, cmd in tree.items():
            if _cmdline(pid) == cmd:
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        self._log("[GUI] Zatrzymano (procesy zakończone).\n")


def _cmdline(pid: int) -> str | None:
    try:
        return Path(f"/proc/{pid}/cmdline").read_bytes().decode(errors="replace")
    except OSError:
        return None


def _descendants(root_pid: int) -> dict:
    """{pid: cmdline} wszystkich potomków root_pid (Linux /proc)."""
    children: dict = {}
    for d in Path("/proc").iterdir():
        if not d.name.isdigit():
            continue
        try:
            stat = (d / "stat").read_text()
            ppid = int(stat[stat.rindex(")") + 2:].split()[1])
        except (OSError, ValueError, IndexError):
            continue
        children.setdefault(ppid, []).append(int(d.name))
    out, stack = {}, [root_pid]
    while stack:
        for c in children.get(stack.pop(), []):
            cmd = _cmdline(c)
            if cmd is not None and c not in out:
                out[c] = cmd
                stack.append(c)
    return out


def _shq(s: str) -> str:
    return s if re.fullmatch(r"[\w./:=+,-]+", s) else "'" + s.replace("'", "'\\''") + "'"


def open_path(p: Path) -> None:
    opener = "xdg-open" if sys.platform.startswith("linux") else ("open" if sys.platform == "darwin" else None)
    if opener and shutil.which(opener):
        subprocess.Popen([opener, str(p)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
    elif hasattr(os, "startfile"):
        os.startfile(str(p))  # type: ignore[attr-defined]


def notify(title: str, body: str, urgent: bool = False) -> None:
    """Powiadomienie systemowe (notify-send), jeśli dostępne."""
    if not shutil.which("notify-send"):
        return
    try:
        subprocess.Popen(["notify-send", "-a", "pdfmaker", "-i", "x-office-document",
                          "-u", "critical" if urgent else "normal", title, body],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    except Exception:
        pass


# --------------------------------------------------------------------------- #
#  GUI (Tkinter/ttk + opcjonalnie ttkbootstrap i tkinterdnd2)
# --------------------------------------------------------------------------- #

MODE_LABELS = {
    "math": "Matematyka / LaTeX (wzory, Marker)",
    "text": "Sam tekst (Tesseract, szybko)",
}
LAYOUT_LABELS = {
    "faithful": "Wierny układ strony",
    "flow": "Ciągły tekst",
}
FLAG_LABELS = {
    "newpage": "Każda strona źródła od nowej strony",
    "no-images": "Bez obrazów (nie wycinaj ilustracji)",
    "force": "Wymuś ponowny OCR (nie wznawiaj)",
    "skip-ocr": "Tylko przebuduj PDF z gotowego tekstu (bez OCR)",
    "layout": "Wierny układ strony",
    "compare": "Porównanie ze skanem",
}
VALUE_LABELS = {"dpi": "DPI Tesseracta", "fakebold": "Pogrubienie czcionki"}
VALUE_HINTS = {"dpi": "puste = 300", "fakebold": "auto / 0 / liczba"}
HELP_PL = {
    "pages": "Które strony skanu przetworzyć, np. 10-20 albo 1,3,5-7 (numeracja od 1). "
             "„-5” = od początku do 5, „400-” = od 400 do końca. Puste = cała książka.",
    "mode": "Matematyka / LaTeX: OCR Markerem z rozpoznawaniem wzorów (wolno, ok. 1–6 min/stronę). "
            "Sam tekst: Tesseract, szybko, bez wzorów; dodatkowo powstaje przeszukiwalny skan.",
    "layout": "Ciągły tekst: tekst płynie strona za stroną (domyślnie). "
              "Wierny układ: każda strona odtworzona 1:1 jak w skanie (pozycje bloków z Markera). "
              "Wymaga trybu Matematyka.",
    "engine": "Silnik LaTeX do składu. Wierny układ używa fontów fontspec, więc pdflatex jest tam "
              "automatycznie zamieniany na xelatex.",
    "dpi": "Rozdzielczość renderowania stron dla Tesseracta: tryb „Sam tekst” oraz awaryjny OCR, "
           "gdy Marker zawiedzie na stronie. Puste = 300.",
    "fakebold": "Tylko wierny układ: grubość kreski fontu. auto = dopasuj do tuszu w skanie, "
                "0 = cienkie fonty Latin Modern, albo liczba (np. 0.3).",
    "force": "Ponownie OCR-uje strony, które mają już wynik w folderze roboczym. "
             "Bez tej opcji Start wznawia pracę (gotowe strony są pomijane).",
    "newpage": "Ciągły tekst: każda strona skanu zaczyna się od nowej strony PDF.",
    "skip-ocr": "Nie uruchamia OCR – tylko składa LaTeX/PDF z tekstu, który już jest w folderze roboczym "
                "(np. po ręcznej poprawce plików .md).",
    "no-images": "Nie wycina ilustracji ze skanu (domyślnie Marker zapisuje je do pages/img/).",
    "compare": "Tylko wierny układ: obrazy porównawcze (obok siebie / nałożone) i miary podobieństwa "
               "względem skanu w folderze roboczym compare/.",
}
FIXED = {"help", "out", "pages", "mode", "engine", "layout"}

THEMES_TB = {"light": ["bootstrap-light", "flatly", "litera", "cosmo"],
             "dark": ["bootstrap-dark", "darkly", "superhero", "cyborg"]}
FONT_NAMES = ("TkDefaultFont", "TkTextFont", "TkFixedFont", "TkMenuFont", "TkHeadingFont",
              "TkCaptionFont", "TkSmallCaptionFont", "TkIconFont", "TkTooltipFont")


def _labelled(choice: str, table: dict) -> str:
    return f"{table[choice]}  [{choice}]" if choice in table else choice


def _unlabel(text: str) -> str:
    m = re.search(r"\[([^\]]+)\]\s*$", text or "")
    return m.group(1) if m else (text or "")


class ThemeManager:
    """Motyw jasny/ciemny.  ttkbootstrap (jeśli jest) dopięty do ISTNIEJĄCEGO roota (Style na
    istniejącym Tk – tkinterdnd2 dalej działa); bez niego własne motywy na bazie 'clam'."""

    def __init__(self, root, ttk):
        self.root, self.ttk = root, ttk
        self.bootstrap = False
        self.style = None
        if os.environ.get("PDFMAKER_GUI_NO_TTKBOOTSTRAP") != "1":
            try:
                import ttkbootstrap as tb  # type: ignore
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore")
                    # ttkbootstrap 2.x nie łata już zwykłych widgetów ttk – włącz to jawnie,
                    # żeby np. ttk.Progressbar dostał styl motywu (w 1.x to domyślne zachowanie)
                    if hasattr(tb, "enable_global_api"):
                        tb.enable_global_api()
                    self.style = tb.Style()
                self.bootstrap = True
            except Exception:
                self.style = None
        if self.style is None:
            self.style = ttk.Style(root)
            self._make_plain_themes()

    def _make_plain_themes(self):
        s = self.style
        names = s.theme_names()
        if "pdfmaker-light" not in names:
            s.theme_create("pdfmaker-light", parent="clam", settings={})
        if "pdfmaker-dark" not in names:
            bg, fg, fld, sel, brd = "#2b2d31", "#e4e6eb", "#3a3d43", "#3c7dd9", "#4a4d55"
            s.theme_create("pdfmaker-dark", parent="clam", settings={
                ".": {"configure": {"background": bg, "foreground": fg, "fieldbackground": fld,
                                    "troughcolor": "#24262a", "bordercolor": brd, "lightcolor": brd,
                                    "darkcolor": "#1e1f22", "selectbackground": sel,
                                    "selectforeground": "#ffffff", "insertcolor": fg},
                      "map": {"foreground": [("disabled", "#8a8d93")],
                              "background": [("disabled", bg)]}},
                "TButton": {"configure": {"background": "#40434a"},
                            "map": {"background": [("active", "#4c5058"), ("disabled", "#33353a")]}},
                "TEntry": {"configure": {"fieldbackground": fld, "foreground": fg}},
                "TCombobox": {"configure": {"fieldbackground": fld, "foreground": fg, "background": "#40434a",
                                            "arrowcolor": fg},
                              "map": {"fieldbackground": [("readonly", fld), ("disabled", bg)],
                                      "foreground": [("disabled", "#8a8d93")]}},
                "TCheckbutton": {"configure": {"indicatorbackground": fld},
                                 "map": {"background": [("active", bg)],
                                         "indicatorbackground": [("disabled", bg)]}},
                "TLabelframe": {"configure": {"background": bg, "bordercolor": brd}},
                "TLabelframe.Label": {"configure": {"background": bg, "foreground": fg}},
                "TScrollbar": {"configure": {"background": "#40434a", "arrowcolor": fg}},
                "Horizontal.TProgressbar": {"configure": {"background": sel}},
                "TPanedwindow": {"configure": {"background": "#1e1f22"}},
            })

    def apply(self, mode: str):
        s = self.style
        if self.bootstrap:
            names = s.theme_names()
            pick = next((t for t in THEMES_TB.get(mode, THEMES_TB["light"]) if t in names), None)
            if pick:
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore")
                    try:
                        s.theme_use(pick)
                    except Exception:
                        pass
                    self._prime()
        else:
            s.theme_use("pdfmaker-dark" if mode == "dark" else "pdfmaker-light")

    def _prime(self):
        """ttkbootstrap buduje style leniwie (przy tworzeniu własnych widgetów).  Tworzymy na chwilę
        po jednym widgecie każdego używanego typu, żeby zwykłe widgety ttk dostały pełny styl motywu."""
        try:
            import ttkbootstrap as tb  # type: ignore
            tmp = self.ttk.Frame(self.root)
            for cls, kw in (("Progressbar", {"orient": "horizontal"}), ("Scrollbar", {"orient": "vertical"}),
                            ("Scrollbar", {"orient": "horizontal"}), ("Button", {}), ("Label", {}),
                            ("Entry", {}), ("Combobox", {}), ("Checkbutton", {}), ("Frame", {}),
                            ("Labelframe", {}), ("Panedwindow", {"orient": "vertical"})):
                c = getattr(tb, cls, None)
                if c is not None:
                    try:
                        c(tmp, **kw).destroy()
                    except Exception:
                        pass
            tmp.destroy()
        except Exception:
            pass

    def palette(self, mode: str) -> dict:
        s = self.style
        bg = s.lookup(".", "background") or ("#2b2d31" if mode == "dark" else "#f0f0f0")
        fg = s.lookup(".", "foreground") or ("#e4e6eb" if mode == "dark" else "#000000")
        fld = s.lookup("TEntry", "fieldbackground") or s.lookup(".", "fieldbackground") or bg
        dark = mode == "dark"
        return {"bg": bg, "fg": fg, "field": fld,
                "muted": "#9aa0a6" if dark else "#5f6368",
                "accent": "#5b9bff" if dark else "#2f6fd6",
                "drop": "#33363d" if dark else "#eef3fb",
                "drop_hot": "#2c4a75" if dark else "#d4e6ff",
                "ok": "#81c995" if dark else "#1e7b34",
                "warn": "#fdd663" if dark else "#a15c00",
                "err": "#f28b82" if dark else "#b3261e",
                "select": "#3c7dd9"}


class Tooltip:
    """Dymek z opisem po najechaniu myszą.  text: str albo funkcja zwracająca str."""

    def __init__(self, app, widget, text):
        self.app, self.w, self.text = app, widget, text
        self.tip = None
        self._after = None
        widget.bind("<Enter>", self._enter, add="+")
        widget.bind("<Leave>", self._leave, add="+")
        widget.bind("<ButtonPress>", self._leave, add="+")

    def _enter(self, _e=None):
        self._after = self.w.after(550, self._show)

    def _leave(self, _e=None):
        if self._after:
            self.w.after_cancel(self._after)
            self._after = None
        if self.tip:
            self.tip.destroy()
            self.tip = None

    def _show(self):
        txt = self.text() if callable(self.text) else self.text
        if not txt or self.tip:
            return
        tk = self.app.tk
        pal = self.app.pal
        self.tip = t = tk.Toplevel(self.w)
        t.wm_overrideredirect(True)
        x = self.w.winfo_rootx() + 12
        y = self.w.winfo_rooty() + self.w.winfo_height() + 4
        t.wm_geometry(f"+{x}+{y}")
        tk.Label(t, text=txt, justify="left", wraplength=460, bg=pal["field"], fg=pal["fg"],
                 relief="solid", bd=1, padx=8, pady=5, font="TkTooltipFont").pack()


class ScrollFrame:
    """Przewijana ramka (Canvas + pionowy pasek)."""

    def __init__(self, app, parent):
        tk, ttk = app.tk, app.ttk
        self.outer = ttk.Frame(parent)
        self.canvas = tk.Canvas(self.outer, highlightthickness=0, bd=0)
        self.vsb = ttk.Scrollbar(self.outer, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=self.vsb.set)
        self.vsb.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)
        self.inner = ttk.Frame(self.canvas, padding=(2, 2, 8, 2))
        self._win = self.canvas.create_window((0, 0), window=self.inner, anchor="nw")
        self.inner.bind("<Configure>", lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>", lambda e: self.canvas.itemconfigure(self._win, width=e.width))
        for w in (self.canvas, self.inner):
            w.bind("<Enter>", self._bind_wheel)
            w.bind("<Leave>", self._unbind_wheel)

    def _bind_wheel(self, _e=None):
        c = self.canvas
        c.bind_all("<MouseWheel>", lambda e: c.yview_scroll(int(-e.delta / 120) or (-1 if e.delta > 0 else 1), "units"))
        c.bind_all("<Button-4>", lambda e: c.yview_scroll(-2, "units"))
        c.bind_all("<Button-5>", lambda e: c.yview_scroll(2, "units"))

    def _unbind_wheel(self, _e=None):
        for seq in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
            self.canvas.unbind_all(seq)


def main_gui(argv: list[str]) -> None:
    import tkinter as tk
    from tkinter import ttk, filedialog, messagebox

    try:
        from tkinterdnd2 import TkinterDnD  # type: ignore
        root = TkinterDnD.Tk()
        dnd = True
    except Exception:
        root = tk.Tk()
        dnd = False
    app = App(root, dnd, tk, ttk, filedialog, messagebox)
    if len(argv) > 1 and argv[1].lower().endswith(".pdf"):
        app.set_input(Path(argv[1]))
    root.mainloop()


class App:
    def __init__(self, root, dnd, tk, ttk, filedialog, messagebox):
        import tkinter.font as tkfont
        self.root, self.dnd, self.tk, self.ttk = root, dnd, tk, ttk
        self.fd, self.mb, self.tkfont = filedialog, messagebox, tkfont
        self.cfg = load_config()
        self.events: queue.Queue = queue.Queue()
        self.job: Job | None = None            # aktywne albo ostatnie zadanie (zamrożone parametry)
        self.opts: list[Opt] = []
        self.script_dir: Path | None = None
        self.input: Path | None = None
        self.output: Path | None = None
        self.output_auto = True
        self.total_pages: int | None = None
        self.vars: dict = {}
        self.opt_widgets: dict = {}            # nazwa -> [(widget, stan_normalny)]
        self.freeze: list = []                 # widgety blokowane na czas zadania
        self.t_start = 0.0
        self.loading_opts = False
        self.closing = False
        self._opts_token = 0
        self._pages_token = 0
        self._indet = False
        self._last_progress: dict | None = None
        self.theme_mode = self.cfg.get("theme", "light") if self.cfg.get("theme") in ("light", "dark") else "light"
        try:
            self.font_scale = min(2.0, max(0.8, float(self.cfg.get("font_scale", 1.0))))
        except (TypeError, ValueError):
            self.font_scale = 1.0

        root.title("pdfmaker – skan PDF → LaTeX/PDF")
        root.geometry(self.cfg.get("geometry", "980x920"))
        root.minsize(760, 620)
        self.theme = ThemeManager(root, ttk)
        self._base_sizes = {}
        for n in FONT_NAMES:
            try:
                self._base_sizes[n] = int(tkfont.nametofont(n).cget("size")) or 10
            except Exception:
                pass
        self.bigfont = tkfont.Font(font="TkDefaultFont")
        self.boldfont = tkfont.Font(font="TkDefaultFont")
        self.boldfont.configure(weight="bold")
        self.theme.apply(self.theme_mode)
        self.pal = self.theme.palette(self.theme_mode)
        self._build()
        self._apply_fonts()
        self._apply_palette()
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        for seq, d in (("<Control-plus>", 0.1), ("<Control-equal>", 0.1), ("<Control-KP_Add>", 0.1),
                       ("<Control-minus>", -0.1), ("<Control-KP_Subtract>", -0.1)):
            root.bind_all(seq, lambda e, d=d: self.zoom(d))
        root.bind_all("<Control-0>", lambda e: self.zoom(0))
        self.load_script_dir(find_script_dir(self.cfg))
        root.after(300, self._init_sash)
        root.after(POLL_MS, self.poll)

    def _init_sash(self):
        try:
            h = self.pw.winfo_height()
            pos = int(self.cfg.get("sash") or 0)
            if not (120 < pos < h - 150):
                pos = int(h * 0.42)
            self.pw.sashpos(0, pos)
        except Exception:
            pass

    # ================================================================ budowa okna
    def _build(self):
        tk, ttk, root = self.tk, self.ttk, self.root
        main = ttk.Frame(root, padding=8)
        main.pack(fill="both", expand=True)

        # --- pasek górny: skrypt + wygląd
        top = ttk.Frame(main)
        top.pack(fill="x")
        ttk.Label(top, text="Skrypt pdfmaker:").pack(side="left")
        self.script_lbl = ttk.Label(top, text="", style="Muted.TLabel")
        self.script_lbl.pack(side="left", padx=6, fill="x", expand=True)
        b = ttk.Button(top, text="A+", width=3, command=lambda: self.zoom(0.1))
        b.pack(side="right")
        Tooltip(self, b, "Większa czcionka (Ctrl +)")
        b = ttk.Button(top, text="A−", width=3, command=lambda: self.zoom(-0.1))
        b.pack(side="right", padx=(0, 2))
        Tooltip(self, b, "Mniejsza czcionka (Ctrl −, Ctrl 0 = domyślna)")
        self.dark_var = tk.BooleanVar(value=self.theme_mode == "dark")
        ttk.Checkbutton(top, text="Ciemny motyw", variable=self.dark_var,
                        command=self.toggle_theme).pack(side="right", padx=8)
        self.script_btn = ttk.Button(top, text="Folder skryptu…", command=self.pick_script_dir)
        self.script_btn.pack(side="right", padx=4)
        Tooltip(self, self.script_btn, "Wskaż folder z plikiem convert.sh (wybór jest zapamiętywany).")
        self.freeze.append((self.script_btn, "normal"))

        pw = ttk.Panedwindow(main, orient="vertical")
        pw.pack(fill="both", expand=True, pady=(6, 0))
        self.pw = pw

        # ---------------- górna część: przewijane ustawienia
        self.sf = ScrollFrame(self, pw)
        pw.add(self.sf.outer, weight=3)
        body = self.sf.inner

        basic = ttk.LabelFrame(body, text="Podstawowe", padding=8)
        basic.pack(fill="x", pady=(0, 6))
        basic.columnconfigure(1, weight=1)
        self.drop = tk.Label(basic, text="", relief="groove", bd=2, height=3, font=self.bigfont,
                             cursor="hand2", justify="center")
        self.drop.grid(row=0, column=0, columnspan=3, sticky="ew", pady=(0, 4))
        self.drop.bind("<Button-1>", lambda e: self.pick_input())
        self.drop.bind("<Configure>", lambda e: self.drop.configure(wraplength=max(200, e.width - 20)))
        fr = ttk.Frame(basic)
        fr.grid(row=1, column=0, columnspan=3, sticky="ew")
        self.pick_btn = ttk.Button(fr, text="Wybierz plik…", command=self.pick_input)
        self.pick_btn.pack(side="left")
        self.freeze.append((self.pick_btn, "normal"))
        self.info_lbl = ttk.Label(fr, text="")
        self.info_lbl.pack(side="left", padx=8)
        self._drop_text()
        if self.dnd:
            from tkinterdnd2 import DND_FILES  # type: ignore
            for w in (self.drop, root):
                w.drop_target_register(DND_FILES)
                w.dnd_bind("<<Drop>>", self.on_drop)
                w.dnd_bind("<<DropEnter>>", lambda e: (self.drop.configure(bg=self.pal["drop_hot"]), e.action)[1])
                w.dnd_bind("<<DropLeave>>", lambda e: (self.drop.configure(bg=self.pal["drop"]), e.action)[1])
        self.bopt = ttk.Frame(basic)
        self.bopt.grid(row=2, column=0, columnspan=3, sticky="ew", pady=(6, 0))
        of = ttk.Frame(basic)
        of.grid(row=3, column=0, columnspan=3, sticky="ew", pady=(6, 0))
        self.out_btn = ttk.Button(of, text="Zapisz jako…", command=self.pick_output)
        self.out_btn.pack(side="left")
        self.freeze.append((self.out_btn, "normal"))
        Tooltip(self, self.out_btn, "Gdzie zapisać wynikowy PDF. Obok powstaje folder roboczy <wynik>_praca/ "
                                    "(strony, book.tex, report.json, pełny dziennik) – ta sama ścieżka = wznawianie.")
        self.out_lbl = ttk.Label(of, text="(wynik: obok pliku źródłowego)", style="Muted.TLabel")
        self.out_lbl.pack(side="left", padx=8, fill="x", expand=True)
        self.out_lbl.bind("<Configure>", lambda e: self.out_lbl.configure(wraplength=max(200, e.width - 10)))
        self.valid_lbl = ttk.Label(basic, text="", style="Err.TLabel")
        self.valid_lbl.grid(row=4, column=0, columnspan=3, sticky="w", pady=(4, 0))

        adv = ttk.LabelFrame(body, text="Zaawansowane", padding=8)
        adv.pack(fill="x", pady=(0, 6))
        self.adv_btn = ttk.Button(adv, text="", command=self.toggle_advanced)
        self.adv_btn.pack(anchor="w")
        self.adv_inner = ttk.Frame(adv)
        self.adv_open = bool(self.cfg.get("advanced_open", False))
        self._show_advanced()

        # ---------------- dolna część: zadanie, wyniki, dziennik
        bottom = ttk.Frame(pw)
        pw.add(bottom, weight=4)

        jf = ttk.LabelFrame(bottom, text="Aktywne zadanie", padding=8)
        jf.pack(fill="x")
        jf.columnconfigure(3, weight=1)
        self.start_btn = ttk.Button(jf, text="▶ Start", command=self.start)
        self.start_btn.grid(row=0, column=0, sticky="w")
        self.stop_btn = ttk.Button(jf, text="■ Stop", command=self.stop, state="disabled")
        self.stop_btn.grid(row=0, column=1, sticky="w", padx=6)
        Tooltip(self, self.stop_btn, "Czysto zatrzymuje skrypt razem z OCR i llama-server. "
                                     "Ponowny Start z tym samym wynikiem wznawia pracę.")
        self.job_lbl = ttk.Label(jf, text="Brak aktywnego zadania.", style="Muted.TLabel")
        self.job_lbl.grid(row=0, column=3, sticky="ew", padx=8)
        self.job_lbl.bind("<Configure>", lambda e: self.job_lbl.configure(wraplength=max(200, e.width - 10)))
        self.phase_lbl = ttk.Label(jf, text="", style="Head.TLabel")
        self.phase_lbl.grid(row=1, column=0, columnspan=4, sticky="w", pady=(6, 2))
        self.pbar = ttk.Progressbar(jf, mode="determinate", maximum=100)
        self.pbar.grid(row=2, column=0, columnspan=4, sticky="ew")
        Tooltip(self, self.pbar, "Pasek pokazuje postęp BIEŻĄCEJ fazy (OCR, skład, kompilacja…), "
                                 "a nie procent całego przebiegu. Gdy fazy nie da się zmierzyć, pasek „pulsuje”.")
        self.status = ttk.Label(jf, text="Gotowy.")
        self.status.grid(row=3, column=0, columnspan=4, sticky="w", pady=(4, 0))
        self.idle_lbl = ttk.Label(jf, text="", style="Muted.TLabel")
        self.idle_lbl.grid(row=4, column=0, columnspan=4, sticky="w")

        rf = ttk.LabelFrame(bottom, text="Wyniki", padding=8)
        rf.pack(fill="x", pady=(6, 0))
        self.state_lbl = ttk.Label(rf, text="Brak wyników – uruchom zadanie.", style="Muted.TLabel")
        self.state_lbl.pack(anchor="w")
        rb = ttk.Frame(rf)
        rb.pack(fill="x", side="bottom", pady=(6, 0))
        self.reasons_lbl = ttk.Label(rf, text="", justify="left")
        self.summary = ttk.Label(rf, text="", style="Muted.TLabel", justify="left")
        for w in (self.reasons_lbl, self.summary):
            w.bind("<Configure>", lambda e, w=w: w.configure(wraplength=max(200, e.width - 10)))
        self.res_btns = {}
        for key, txt, cmd, tip in (
                ("pdf", "Otwórz PDF", self.open_pdf, "Otwiera wynikowy PDF ostatniego zadania."),
                ("dir", "Folder wyniku", self.open_dir, "Folder z plikiem wynikowym ostatniego/aktywnego zadania."),
                ("work", "Folder roboczy", self.open_workdir, "<wynik>_praca/: strony, book.tex, book.md, report.json."),
                ("report", "Raport (report.json)", self.open_report, "Szczegóły stron: status OCR i LaTeX."),
                ("compare", "Porównanie", self.open_compare, "Folder compare/ (tylko wierny układ z porównaniem)."),
                ("log", "Pełny dziennik", self.open_log, "Pełny dziennik zadania zapisany w folderze roboczym.")):
            bt = ttk.Button(rb, text=txt, command=cmd, state="disabled")
            bt.pack(side="left", padx=(0, 4), pady=2)
            Tooltip(self, bt, tip)
            self.res_btns[key] = bt

        lf = ttk.LabelFrame(bottom, text="Dziennik", padding=4)
        lf.pack(fill="both", expand=True, pady=(6, 0))
        lt = ttk.Frame(lf)
        lt.pack(fill="x")
        self.wrap_var = tk.BooleanVar(value=bool(self.cfg.get("wrap_log", False)))
        ttk.Checkbutton(lt, text="Zawijaj wiersze", variable=self.wrap_var, command=self._set_wrap).pack(side="left")
        ttk.Button(lt, text="Kopiuj pełny dziennik", command=self.copy_log).pack(side="right")
        ttk.Button(lt, text="Otwórz plik dziennika", command=self.open_log).pack(side="right", padx=4)
        self.bottom_btn = ttk.Button(lt, text="↓ Na dół", command=self._log_to_end)
        self.paused_lbl = ttk.Label(lt, text="Autoprzewijanie wstrzymane (przewinięto w górę)", style="Warn.TLabel")
        lg = ttk.Frame(lf)
        lg.pack(fill="both", expand=True)
        lg.rowconfigure(0, weight=1)
        lg.columnconfigure(0, weight=1)
        self.log = tk.Text(lg, height=12, font="TkFixedFont", state="disabled", wrap="none", bd=0,
                           highlightthickness=0)
        vsb = ttk.Scrollbar(lg, orient="vertical", command=self.log.yview)
        hsb = ttk.Scrollbar(lg, orient="horizontal", command=self.log.xview)
        self.log.configure(yscrollcommand=self._log_yscroll(vsb), xscrollcommand=hsb.set)
        self.log.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        hsb.grid(row=1, column=0, sticky="ew")
        self._paused_shown = False
        self._set_wrap()

    # ================================================================ wygląd
    def toggle_theme(self):
        self.theme_mode = "dark" if self.dark_var.get() else "light"
        self.theme.apply(self.theme_mode)
        self.pal = self.theme.palette(self.theme_mode)
        self._apply_fonts()
        self._apply_palette()
        self.cfg["theme"] = self.theme_mode
        save_config(self.cfg)

    def zoom(self, delta: float):
        self.font_scale = 1.0 if delta == 0 else round(min(2.0, max(0.8, self.font_scale + delta)), 2)
        self._apply_fonts()
        self.cfg["font_scale"] = self.font_scale
        save_config(self.cfg)

    def _apply_fonts(self):
        for n, base in self._base_sizes.items():
            try:
                size = max(6, round(abs(base) * self.font_scale))
                self.tkfont.nametofont(n).configure(size=size if base > 0 else -size)
            except Exception:
                pass
        df = self.tkfont.nametofont("TkDefaultFont")
        d = abs(int(df.cget("size"))) or 10
        fam = df.cget("family")      # motyw mógł zmienić rodzinę fontu – trzymaj się jej
        self.bigfont.configure(family=fam, size=round(d * 1.3))
        self.boldfont.configure(family=fam, size=d, weight="bold")

    def _apply_palette(self):
        p, s = self.pal, self.theme.style
        try:
            s.configure("Muted.TLabel", foreground=p["muted"])
            s.configure("Ok.TLabel", foreground=p["ok"], font=self.boldfont)
            s.configure("Warn.TLabel", foreground=p["warn"])
            s.configure("WarnB.TLabel", foreground=p["warn"], font=self.boldfont)
            s.configure("Err.TLabel", foreground=p["err"])
            s.configure("ErrB.TLabel", foreground=p["err"], font=self.boldfont)
            s.configure("Head.TLabel", font=self.boldfont)
        except Exception:
            pass
        for opt, val in (("*TCombobox*Listbox.background", p["field"]), ("*TCombobox*Listbox.foreground", p["fg"]),
                         ("*TCombobox*Listbox.selectBackground", p["select"]),
                         ("*TCombobox*Listbox.selectForeground", "#ffffff")):
            self.root.option_add(opt, val)
        self.drop.configure(bg=p["drop"], fg=p["fg"], activebackground=p["drop_hot"])
        self.sf.canvas.configure(bg=p["bg"])
        self.log.configure(bg=p["field"], fg=p["fg"], insertbackground=p["fg"],
                           selectbackground=p["select"], selectforeground="#ffffff")
        if self.script_dir is None:
            self.script_lbl.configure(style="Err.TLabel")

    def _set_wrap(self):
        self.log.configure(wrap="word" if self.wrap_var.get() else "none")
        if self.cfg.get("wrap_log") != self.wrap_var.get():
            self.cfg["wrap_log"] = bool(self.wrap_var.get())
            save_config(self.cfg)

    def toggle_advanced(self):
        self.adv_open = not self.adv_open
        self.cfg["advanced_open"] = self.adv_open
        save_config(self.cfg)
        self._show_advanced()

    def _show_advanced(self):
        if self.adv_open:
            self.adv_btn.configure(text="▾ Ukryj opcje zaawansowane")
            self.adv_inner.pack(fill="x", pady=(6, 0))
        else:
            self.adv_btn.configure(text="▸ Pokaż opcje zaawansowane (silnik, DPI, wznawianie, obrazy…)")
            self.adv_inner.pack_forget()

    # ================================================================ folder skryptu / opcje
    def load_script_dir(self, d: Path | None):
        self.script_dir = d
        for fr in (self.bopt, self.adv_inner):
            for w in fr.winfo_children():
                w.destroy()
        self.vars, self.opt_widgets, self.opts = {}, {}, []
        self._opts_token += 1
        if not d:
            self.script_lbl.configure(text="NIE ZNALEZIONO – wskaż folder z convert.sh", style="Err.TLabel")
            self.loading_opts = False
            self._refresh_states()
            return
        self.script_lbl.configure(text=str(d), style="Muted.TLabel")
        self.status.configure(text="Odczytuję opcje skryptu (w tle)…")
        self.loading_opts = True
        self._refresh_states()
        token = self._opts_token

        def work():
            try:
                opts, _ = read_script_options(d)
            except Exception:
                opts = []
            self.events.put(("opts", token, opts))
        threading.Thread(target=work, daemon=True).start()

    def _on_opts(self, token, opts):
        if token != self._opts_token:
            return
        self.loading_opts = False
        if not opts:
            opts = list(FALLBACK_OPTS)
            self._log("Uwaga: nie udało się odczytać --help skryptu, używam podstawowych opcji.\n")
        self.opts = opts
        self.build_options()
        if not (self.job and self.job.is_alive()):
            self.status.configure(text="Gotowy.")
        self._refresh_states()
        self._validate_live()

    def _reg(self, name, *widgets_states):
        self.opt_widgets.setdefault(name, []).extend(widgets_states)

    def _tip_for(self, name, extra=""):
        def text():
            base = HELP_PL.get(name) or extra or ""
            o = next((o for o in self.opts if o.name == name), None)
            if not HELP_PL.get(name) and o and o.help:
                base = (base + "\n" if base else "") + f"(--{name}) {o.help}"
            r = option_disabled_reason(name, self._effective_raw())
            if r:
                base += ("\n\n" if base else "") + "Nieaktywne: " + r
            return base
        return text

    def build_options(self):
        ttk, tk = self.ttk, self.tk
        saved = self.cfg.get("options", {}) or {}
        byname = {o.name: o for o in self.opts}
        rows = {"b": 0, "a": 0}

        def grid_row(where):
            f = self.bopt if where == "b" else self.adv_inner
            r = rows[where]
            rows[where] += 1
            return f, r

        def combo_row(where, label, o, table, default):
            f, r = grid_row(where)
            lb = ttk.Label(f, text=label)
            lb.grid(row=r, column=0, sticky="w", pady=2, padx=(0, 8))
            vals = [_labelled(c, table) for c in o.choices]
            if default is None:
                vals = ["(domyślnie)"] + vals
            v = tk.StringVar()
            sv = saved.get(o.name)
            pick = next((x for x in vals if _unlabel(x) == sv), None) or \
                next((x for x in vals if _unlabel(x) == default), vals[0])
            v.set(pick)
            cb = ttk.Combobox(f, textvariable=v, values=vals, state="readonly", width=44)
            cb.grid(row=r, column=1, sticky="w", pady=2)
            self.vars[o.name] = ("choice", v)
            self._reg(o.name, (cb, "readonly"))
            Tooltip(self, cb, self._tip_for(o.name))
            Tooltip(self, lb, self._tip_for(o.name))
            v.trace_add("write", lambda *a: (self._refresh_states(), self._validate_live()))

        def value_row(where, o, label, hint, width=10, value=""):
            f, r = grid_row(where)
            lb = ttk.Label(f, text=label)
            lb.grid(row=r, column=0, sticky="w", pady=2, padx=(0, 8))
            v = tk.StringVar(value=value)
            e = ttk.Entry(f, textvariable=v, width=width)
            e.grid(row=r, column=1, sticky="w", pady=2)
            ttk.Label(f, text=hint, style="Muted.TLabel").grid(row=r, column=2, sticky="w", padx=6)
            self.vars[o.name] = ("value", v)
            self._reg(o.name, (e, "normal"))
            Tooltip(self, e, self._tip_for(o.name))
            Tooltip(self, lb, self._tip_for(o.name))
            v.trace_add("write", lambda *a: self._validate_live())
            return v

        if "pages" in byname:
            pv = value_row("b", byname["pages"], "Zakres stron:", "np. 10-20 lub 1,3,5-7 (puste = wszystkie)", 20)
            pv.trace_add("write", lambda *a: self._update_info())
        if "mode" in byname and byname["mode"].kind == "choice":
            combo_row("b", "Tryb:", byname["mode"], MODE_LABELS, byname["mode"].choices[0])
        if "layout" in byname:
            o = byname["layout"]
            if o.kind == "choice":
                combo_row("b", "Układ strony:", o, LAYOUT_LABELS, None)
            elif o.kind == "flag":
                f, r = grid_row("b")
                v = tk.BooleanVar(value=bool(saved.get("layout", False)))
                cb = ttk.Checkbutton(f, text=FLAG_LABELS["layout"], variable=v)
                cb.grid(row=r, column=1, sticky="w")
                self.vars["layout"] = ("flag", v)
                self._reg("layout", (cb, "normal"))
                Tooltip(self, cb, self._tip_for("layout"))
                v.trace_add("write", lambda *a: self._refresh_states())
            else:
                value_row("b", o, "Układ strony:", "", 20, str(saved.get("layout", "")))
        if "engine" in byname and byname["engine"].kind == "choice":
            combo_row("a", "Silnik LaTeX:", byname["engine"], {}, byname["engine"].choices[0])

        for o in self.opts:
            if o.name in FIXED or o.kind == "flag":
                continue
            label = VALUE_LABELS.get(o.name, "--" + o.name) + ":"
            if o.kind == "choice":
                combo_row("a", label, o, {}, None)
            else:
                value_row("a", o, label, VALUE_HINTS.get(o.name, "puste = domyślnie"), 10,
                          str(saved.get(o.name, "")))
        flags = [o for o in self.opts if o.kind == "flag" and o.name not in FIXED]
        if flags:
            f, r = grid_row("a")
            ff = ttk.Frame(f)
            ff.grid(row=r, column=0, columnspan=3, sticky="w", pady=(6, 0))
            for i, o in enumerate(flags):
                v = tk.BooleanVar(value=bool(saved.get(o.name, False)) if o.name != "force" else False)
                lbl = FLAG_LABELS.get(o.name) or (o.help[:70] if o.help else "--" + o.name)
                cb = ttk.Checkbutton(ff, text=lbl, variable=v)
                cb.grid(row=i // 2, column=i % 2, sticky="w", padx=(0, 18), pady=1)
                self.vars[o.name] = ("flag", v)
                self._reg(o.name, (cb, "normal"))
                Tooltip(self, cb, self._tip_for(o.name))
        for f in (self.bopt, self.adv_inner):
            f.columnconfigure(2, weight=1)

    def _raw_values(self) -> dict:
        out = {}
        for name, (kind, v) in self.vars.items():
            if kind == "flag":
                out[name] = bool(v.get())
            elif kind == "choice":
                s = v.get()
                out[name] = "" if s.startswith("(") else _unlabel(s)
            else:
                out[name] = v.get().strip()
        return out

    def _effective_raw(self) -> dict:
        raw = self._raw_values()
        if option_disabled_reason("layout", raw):
            raw["layout"] = "flow"
        return raw

    def values(self) -> dict:
        """Wartości do uruchomienia – bez opcji nieadekwatnych do trybu (nie trafiają do CLI)."""
        raw = self._effective_raw()
        out = {}
        for k, v in raw.items():
            if option_disabled_reason(k, raw):
                continue
            out[k] = v
        if "layout" in self.vars and option_disabled_reason("layout", self._raw_values()):
            out.pop("layout", None)
        return out

    def running(self) -> bool:
        return bool(self.job and self.job.is_alive())

    def _refresh_states(self):
        run = self.running()
        raw = self._effective_raw() if self.vars else {}
        for name, items in self.opt_widgets.items():
            dis = run or bool(option_disabled_reason(name, raw))
            for w, normal in items:
                try:
                    w.configure(state="disabled" if dis else normal)
                except Exception:
                    pass
        for w, normal in self.freeze:
            try:
                w.configure(state="disabled" if run else normal)
            except Exception:
                pass
        self.drop.configure(cursor="watch" if run else "hand2")
        can_start = (not run) and (not self.closing) and bool(self.script_dir) and not self.loading_opts
        self.start_btn.configure(state="normal" if can_start else "disabled")
        self.stop_btn.configure(state="normal" if run and not self.job.stopping and not self.closing else "disabled")

    def _validate_live(self):
        if not self.vars:
            self.valid_lbl.configure(text="")
            return
        errs = validate_values(self.values(), self.total_pages)
        self.valid_lbl.configure(text=("⚠ " + errs[0]) if errs else "")

    def pick_script_dir(self):
        if self.running():
            return
        d = self.fd.askdirectory(title="Wskaż folder skryptu pdfmaker (z plikiem convert.sh)",
                                 initialdir=str(self.script_dir or HERE.parent))
        if not d:
            return
        if not is_script_dir(d):
            self.mb.showerror("pdfmaker", "W tym folderze nie ma pliku convert.sh.")
            return
        self.cfg["script_dir"] = str(Path(d).resolve())
        save_config(self.cfg)
        self.load_script_dir(Path(d).resolve())

    # ================================================================ plik wejściowy / wyjściowy
    def _drop_text(self):
        if self.input:
            self.drop.configure(text=f"{self.input.name}\n{self.input.parent}")
        else:
            self.drop.configure(text=("Przeciągnij tutaj plik PDF albo kliknij, aby wybrać" if self.dnd
                                      else "Kliknij, aby wybrać plik PDF\n(przeciąganie niedostępne – brak tkinterdnd2)"))

    def on_drop(self, event):
        self.drop.configure(bg=self.pal["drop"])
        if self.running():
            self.mb.showinfo("pdfmaker", "Trwa zadanie – plik można zmienić po jego zakończeniu.")
            return event.action
        files = self.root.tk.splitlist(event.data)
        for f in files:
            if f.startswith("file://"):
                from urllib.parse import unquote, urlparse
                f = unquote(urlparse(f).path)
            if f.lower().endswith(".pdf") and Path(f).is_file():
                self.set_input(Path(f))
                return event.action
        self.mb.showwarning("pdfmaker", "Upuść plik PDF.")
        return event.action

    def pick_input(self):
        if self.running():
            return
        f = self.fd.askopenfilename(title="Wybierz skan PDF", initialdir=self.cfg.get("last_dir", str(Path.home())),
                                    filetypes=[("Pliki PDF", "*.pdf *.PDF"), ("Wszystkie pliki", "*")])
        if f:
            self.set_input(Path(f))

    def set_input(self, p: Path):
        if self.running():
            return
        self.input = p.resolve()
        self.cfg["last_dir"] = str(self.input.parent)
        self.total_pages = None
        if not self.output or self.output_auto:
            self.output = self.input.with_name(self.input.stem + "_latex.pdf")
            self.output_auto = True
        self._drop_text()
        self.info_lbl.configure(text="liczę strony…")
        self._update_out()
        self._pages_token += 1
        token, path = self._pages_token, self.input

        def work():
            n = pdf_page_count(path)
            self.events.put(("pagecount", token, n))
        threading.Thread(target=work, daemon=True).start()

    def _on_pagecount(self, token, n):
        if token != self._pages_token:
            return
        self.total_pages = n
        self._update_info()
        self._validate_live()

    def _update_info(self):
        if not self.input:
            return
        tp = self.total_pages
        txt = f"{tp} stron" if tp else "liczba stron nieznana"
        spec = self.vars.get("pages", (None, None))[1]
        if tp and spec is not None and spec.get().strip():
            s = spec.get()
            if valid_pages_spec(s):
                txt += f", wybrano {len(parse_pages(s, tp))}"
            else:
                txt += ", błędny zakres!"
        self.info_lbl.configure(text=txt)

    def _update_out(self):
        if self.output:
            wd = work_dir_for(self.output)
            errs = check_paths(self.input, self.output, self.script_dir) if self.input else []
            self.out_lbl.configure(text=f"{self.output}   (folder roboczy: {wd.name}/)",
                                   style="Err.TLabel" if errs else "TLabel")
            if errs:
                self.valid_lbl.configure(text="⚠ " + errs[0])
            else:
                self._validate_live()

    def pick_output(self):
        if self.running():
            return
        init_dir = str(self.output.parent if self.output else self.cfg.get("last_dir", Path.home()))
        init = self.output.name if self.output else "wynik.pdf"
        f = self.fd.asksaveasfilename(title="Zapisz wynik jako...", initialdir=init_dir, initialfile=init,
                                      defaultextension=".pdf", filetypes=[("PDF", "*.pdf")])
        if f:
            out = Path(f).resolve()
            if out.suffix.lower() != ".pdf":
                out = out.with_suffix(".pdf")
            errs = check_paths(self.input, out, self.script_dir) if self.input else []
            if errs:
                self.mb.showerror("pdfmaker", "\n\n".join(errs))
                return
            self.output = out
            self.output_auto = False
            self._update_out()

    # ================================================================ uruchamianie
    def start(self):
        if self.running() or self.closing:
            return
        if not self.script_dir:
            self.mb.showerror("pdfmaker", "Najpierw wskaż folder skryptu pdfmaker.")
            return
        if self.loading_opts:
            self.mb.showinfo("pdfmaker", "Chwilę – trwa odczyt opcji skryptu.")
            return
        if not self.input or not self.input.exists():
            self.mb.showerror("pdfmaker", "Wybierz lub przeciągnij plik PDF.")
            return
        vals = self.values()
        if not self.output:
            self.output = self.input.with_name(self.input.stem + "_latex.pdf")
        inp, out = self.input.resolve(), self.output.resolve()
        errs = validate_values(vals, self.total_pages) + check_paths(inp, out, self.script_dir)
        if errs:
            self.mb.showerror("pdfmaker", "Nie można uruchomić:\n\n" + "\n\n".join(errs))
            return
        existing = [p for p in (out, searchable_for(out) if (vals.get("mode") == "text") else None) if p and p.exists()]
        if existing:
            if not self.mb.askyesno("pdfmaker – nadpisać?",
                                    "Te pliki już istnieją i zostaną zastąpione PO UDANEJ konwersji:\n\n" +
                                    "\n".join(str(p) for p in existing) +
                                    "\n\nPrzy błędzie lub anulowaniu pozostaną bez zmian.\nKontynuować?",
                                    icon="warning"):
                return
        self.cfg["options"] = {k: v for k, v in self._raw_values().items() if k != "force"}
        save_config(self.cfg)
        self._clear_log()
        self._reset_results()
        self._last_progress = None
        self.pbar.stop()
        self._indet = False
        self.pbar.configure(mode="determinate", value=0)
        self.job = Job(self.script_dir, inp, out, vals, self.opts, self.events, total_pages=self.total_pages)
        self.t_start = time.time()
        self.job_lbl.configure(text=f"{inp.name}  →  {out}")
        self.phase_lbl.configure(text="Faza: " + PHASE_LABELS["start"])
        self.status.configure(text="Uruchamiam…")
        self.idle_lbl.configure(text="")
        self.job.start()
        self._refresh_states()

    def stop(self):
        if self.running():
            self.status.configure(text="Zatrzymywanie… (czekam na zakończenie procesów OCR/LaTeX)")
            self.job.stop()
            self._refresh_states()

    def _set_text(self, lbl, text):
        """Etykieta wyników: pokazana tylko, gdy ma treść (bez pustych linii w oknie)."""
        lbl.configure(text=text)
        if text:
            lbl.pack(anchor="w", fill="x", after=self.state_lbl if lbl is self.reasons_lbl else self.reasons_lbl
                     if self.reasons_lbl.winfo_manager() else self.state_lbl)
        else:
            lbl.pack_forget()

    def _reset_results(self):
        self.state_lbl.configure(text="Zadanie w toku…", style="Muted.TLabel")
        self._set_text(self.reasons_lbl, "")
        self._set_text(self.summary, "")
        for b in self.res_btns.values():
            b.configure(state="disabled")

    def _set_pbar(self, done, total):
        if total:
            if self._indet:
                self.pbar.stop()
                self.pbar.configure(mode="determinate")
                self._indet = False
            self.pbar.configure(value=100.0 * done / max(1, total))
        elif not self._indet:
            self.pbar.configure(mode="indeterminate", value=0)
            self.pbar.start(60)
            self._indet = True

    def _show_progress(self, d: dict):
        txt = "Faza: " + d["label"]
        if d.get("detail"):
            txt += f" – {d['detail']}"
        self.phase_lbl.configure(text=txt)
        self._set_pbar(d.get("done") or 0, d.get("total"))

    def _tick_status(self):
        if not self.running():
            return
        el = fmt_dur(time.time() - self.t_start)
        d = self._last_progress or {}
        parts = []
        if self.job.stopping:
            parts.append("Zatrzymywanie… (czekam na zakończenie procesów)")
        if d.get("total"):
            unit = "części" if d.get("phase") == "compile" else "strony"
            parts.append(f"{unit}: {d['done']}/{d['total']}")
        parts.append(f"czas: {el}")
        if d.get("eta"):
            parts.append(f"do końca OCR ok. {fmt_dur(d['eta'])} "
                         f"({'szacunek bardzo niepewny' if d.get('eta_uncertain') else 'szacunek'})")
        self.status.configure(text="   ".join(parts))
        idle = time.time() - self.job.last_line_t
        if idle > IDLE_WARN_SEC:
            self.idle_lbl.configure(text=f"Brak nowych komunikatów od {fmt_dur(idle)} – skrypt może nadal "
                                         "pracować (np. kompilacja, ładowanie modeli). Nie przerywam automatycznie.")
        else:
            self.idle_lbl.configure(text="")

    def poll(self):
        logs: list[str] = []
        n = 0
        try:
            while n < MAX_EVENTS_PER_POLL:
                ev = self.events.get_nowait()
                n += 1
                kind = ev[0]
                if kind == "log":
                    logs.append(ev[1])
                    continue
                if logs:
                    self._log("".join(logs))
                    logs = []
                if kind == "progress":
                    self._last_progress = ev[1]
                    self._show_progress(ev[1])
                elif kind == "done":
                    self._finished(*ev[1:])
                elif kind == "opts":
                    self._on_opts(ev[1], ev[2])
                elif kind == "pagecount":
                    self._on_pagecount(ev[1], ev[2])
                elif kind == "logfile":
                    pass
        except queue.Empty:
            pass
        if logs:
            self._log("".join(logs))
        try:
            self._tick_status()
            self.root.after(POLL_MS, self.poll)
        except Exception:
            pass   # okno zamknięte

    def _finished(self, state, rc, result, summary, reasons, logfile):
        job = self.job
        el = fmt_dur(time.time() - self.t_start)
        if state in ("ok", "warn"):
            self._set_pbar(1, 1)
        else:
            self.pbar.stop()
            self._indet = False
            self.pbar.configure(mode="determinate")
        label = STATE_LABELS.get(state, state)
        style = {"ok": "Ok.TLabel", "warn": "WarnB.TLabel", "error": "ErrB.TLabel", "cancelled": "WarnB.TLabel"}[state]
        self.state_lbl.configure(text=f"{label} (czas: {el})", style=style)
        self.phase_lbl.configure(text=f"Zakończono: {label}")
        self._set_text(self.reasons_lbl, "\n".join("• " + r for r in reasons or []))
        self._set_text(self.summary, ("Podsumowanie (report.json): " + summary_text(summary)) if summary else "")
        if result:
            self.status.configure(text=f"Zapisano: {result}")
        elif state == "cancelled":
            self.status.configure(text="Zatrzymano. Ponowny Start z tym samym plikiem wynikowym wznowi pracę.")
        else:
            self.status.configure(text=f"Wynik NIE został zapisany (kod {rc}). Zobacz powody powyżej i dziennik.")
        self.idle_lbl.configure(text="")
        if job:
            wd = job.workdir
            avail = {"pdf": bool(result and Path(result).exists()),
                     "dir": job.output.parent.exists(),
                     "work": wd.exists(),
                     "report": (wd / "report.json").exists(),
                     "compare": (wd / "compare").is_dir() and any((wd / "compare").iterdir()),
                     "log": bool(logfile and Path(logfile).exists())}
            for k, b in self.res_btns.items():
                b.configure(state="normal" if avail.get(k) else "disabled")
        self._refresh_states()
        if not self.closing:
            name = job.output.name if job else ""
            notify(f"pdfmaker: {label}", f"{name} – {el}" + (f"\n{reasons[0]}" if reasons else ""),
                   urgent=state == "error")
            try:
                self.root.bell()
            except Exception:
                pass
        else:
            self._destroy()

    # ================================================================ wyniki (dotyczą aktywnego/ostatniego zadania)
    def open_pdf(self):
        if self.job and self.job.output.exists():
            open_path(self.job.output)

    def open_dir(self):
        if self.job:
            open_path(self.job.output.parent if self.job.output.parent.exists() else self.job.workdir)

    def open_workdir(self):
        if self.job and self.job.workdir.exists():
            open_path(self.job.workdir)

    def open_report(self):
        if self.job and (self.job.workdir / "report.json").exists():
            open_path(self.job.workdir / "report.json")

    def open_compare(self):
        if self.job and (self.job.workdir / "compare").is_dir():
            open_path(self.job.workdir / "compare")

    def open_log(self):
        if self.job and self.job.logfile.exists():
            open_path(self.job.logfile)
        else:
            self.mb.showinfo("pdfmaker", "Brak pliku dziennika – zadanie jeszcze nie było uruchomione.")

    def copy_log(self):
        txt = None
        if self.job and self.job.logfile.exists():
            try:
                txt = self.job.logfile.read_text(encoding="utf-8", errors="replace")
            except OSError:
                txt = None
        if txt is None:
            txt = self.log.get("1.0", "end-1c")
        self.root.clipboard_clear()
        self.root.clipboard_append(txt)
        self.status.configure(text=f"Skopiowano dziennik do schowka ({len(txt.splitlines())} linii).")

    # ================================================================ dziennik
    def _log_yscroll(self, vsb):
        def setter(first, last):
            vsb.set(first, last)
            if float(last) >= 0.999 and self._paused_shown:
                self._show_paused(False)
        return setter

    def _show_paused(self, on: bool):
        if on and not self._paused_shown:
            self.bottom_btn.pack(side="left", padx=(12, 4))
            self.paused_lbl.pack(side="left")
        elif not on and self._paused_shown:
            self.bottom_btn.pack_forget()
            self.paused_lbl.pack_forget()
        self._paused_shown = on

    def _log_to_end(self):
        self.log.see("end")
        self._show_paused(False)

    def _log(self, s: str):
        t = self.log
        at_bottom = t.yview()[1] >= 0.999
        t.configure(state="normal")
        t.insert("end", s)
        lines = int(t.index("end-1c").split(".")[0])
        if lines > MAX_UI_LOG_LINES:
            t.delete("1.0", f"{lines - MAX_UI_LOG_LINES + 1}.0")
        t.configure(state="disabled")
        if at_bottom:
            t.see("end")
        else:
            self._show_paused(True)

    def _clear_log(self):
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")
        self._show_paused(False)

    # ================================================================ zamykanie
    def on_close(self):
        if self.closing:
            if self.mb.askyesno("pdfmaker", "Nadal czekam na zakończenie procesów.\n"
                                            "Zamknąć okno natychmiast? (procesy dostały już sygnał zatrzymania)"):
                self._destroy()
            return
        if self.running():
            if not self.mb.askyesno("pdfmaker", "Konwersja trwa. Zatrzymać ją i zamknąć okno?\n"
                                                "(postęp zostanie zachowany – można wznowić)"):
                return
            self.closing = True
            self.job.stop()          # asynchronicznie – okno zamknie się po zdarzeniu 'done'
            self.status.configure(text="Zatrzymywanie… okno zamknie się po zakończeniu procesów.")
            self._refresh_states()
            self.root.after(120_000, self._force_close)
            return
        self._destroy()

    def _force_close(self):
        if self.running():
            self._destroy()

    def _destroy(self):
        try:
            self.cfg["geometry"] = self.root.geometry()
            self.cfg["theme"] = self.theme_mode
            self.cfg["font_scale"] = self.font_scale
            try:
                self.cfg["sash"] = int(self.pw.sashpos(0))
            except Exception:
                pass
            save_config(self.cfg)
        finally:
            try:
                self.root.destroy()
            except Exception:
                pass


if __name__ == "__main__":
    main_gui(sys.argv)
