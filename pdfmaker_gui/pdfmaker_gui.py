#!/usr/bin/env python3
"""pdfmaker GUI – okienkowa nakładka na skrypt pdfmaker (convert.sh).

Osobny program: nie zawiera logiki OCR, tylko uruchamia ../pdfmaker/convert.sh
(lub folder wskazany w ustawieniach) z wybranymi opcjami.  Dostępne opcje
odczytuje przy starcie z `convert.py --help`, więc nowe flagi skryptu
(np. --layout) pojawiają się w oknie automatycznie.

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
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

HERE = Path(__file__).resolve().parent
CONFIG = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "pdfmaker_gui.json"

# --------------------------------------------------------------------------- #
#  Backend (bez Tk – można testować bez okna)
# --------------------------------------------------------------------------- #

def load_config() -> dict:
    try:
        return json.loads(CONFIG.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_config(cfg: dict) -> None:
    try:
        CONFIG.parent.mkdir(parents=True, exist_ok=True)
        CONFIG.write_text(json.dumps(cfg, indent=1, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


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


def read_script_options(script_dir: Path) -> tuple[list[Opt], str]:
    """Zwraca (opcje, surowy tekst --help).  Najpierw convert.py, potem convert.sh."""
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
    try:
        r = subprocess.run(["pdfinfo", str(pdf)], capture_output=True, text=True, timeout=30)
        m = re.search(r"^Pages:\s+(\d+)", r.stdout, re.M)
        if m:
            return int(m.group(1))
    except Exception:
        pass
    try:  # awaryjnie pikepdf / pypdf, jeśli są
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
    return bool(re.fullmatch(r"\s*(\d*-?\d*)(\s*,\s*\d*-?\d*)*\s*", spec or "")) and \
        not re.search(r"(^|,)\s*-?\s*(,|$)", spec.strip()) if spec.strip() else True


def work_dir_for(output: Path) -> Path:
    """Folder roboczy obok pliku wynikowego: <wynik>_praca/ (wznawianie działa przy tej samej ścieżce)."""
    return output.with_name(output.stem + "_praca")


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


def read_summary(workdir: Path, pages: list[int] | None = None) -> dict | None:
    """_summary z report.json + zliczenie statusów 'tex' (dla stron z tego zadania, jeśli podane)."""
    rep = read_report(workdir)
    if not rep:
        return None
    s = dict(rep.get("_summary") or {})
    counts: dict = {}
    for k, v in rep.items():
        if k.isdigit() and isinstance(v, dict) and (not pages or int(k) in pages):
            st = str(v.get("tex", "brak")).split()[0].split("(")[0]
            counts[st] = counts.get(st, 0) + 1
    s["_tex_counts"] = counts
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


_PAGE_DONE_RE = re.compile(r"\bpage (\d+): (?:OK|FAILED|tesseract OK|tesseract FAILED)")


class Job(threading.Thread):
    """Uruchamia convert.sh w osobnej grupie procesów; zdarzenia wysyła do kolejki:
       ("log", str) ("progress", done, total, eta_s|None, faza) ("done", rc, wynik|None, summary|None)
    """

    def __init__(self, script_dir: Path, input_pdf: Path, output: Path, values: dict,
                 opts: list[Opt], events: queue.Queue):
        super().__init__(daemon=True)
        self.script_dir, self.input_pdf, self.output = Path(script_dir), Path(input_pdf), Path(output)
        self.values, self.opts, self.q = values, opts, events
        self.workdir = work_dir_for(self.output)
        self.proc: subprocess.Popen | None = None
        self.stopping = False
        total = pdf_page_count(self.input_pdf) or 0
        self.pages = parse_pages(values.get("pages", ""), total) if total else []
        self.done_pages: set[int] = set()
        self.phase = "Start..."

    # -- postęp na podstawie plików pages/page_NNNN.(md|txt) + linii logu --
    def _scan_files(self, since: float | None) -> set[int]:
        pdir = self.workdir / "pages"
        found = set()
        faithful = self.values.get("layout") in ("faithful", True)
        exts = (".json",) if faithful else (".md", ".txt")
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

    def command(self) -> list[str]:
        return build_command(self.script_dir, self.input_pdf, self.workdir, self.values, self.opts)

    def run(self):
        q = self.q
        cmd = self.command()
        self.workdir.mkdir(parents=True, exist_ok=True)
        q.put(("log", "$ " + " ".join(_shq(c) for c in cmd) + "\n"))
        force = bool(self.values.get("force"))
        t0 = time.time()
        cached = set() if force else self._scan_files(None)
        if self.values.get("skip-ocr"):
            cached = set(self.pages)
        self.done_pages |= cached
        n_cached = len(cached)
        env = dict(os.environ, PYTHONUNBUFFERED="1")
        try:
            self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                         stdin=subprocess.DEVNULL, text=True, bufsize=1,
                                         cwd=str(self.script_dir), env=env, start_new_session=True,
                                         errors="replace")
        except Exception as e:
            q.put(("log", f"Nie udało się uruchomić skryptu: {e}\n"))
            q.put(("done", -1, None, None))
            return
        self.phase = "OCR..." if not self.values.get("skip-ocr") else "Składanie LaTeX..."
        last_scan = 0.0
        stop_reader = threading.Event()
        lines: queue.Queue = queue.Queue()

        def reader():
            for line in self.proc.stdout:
                lines.put(line)
            stop_reader.set()
        threading.Thread(target=reader, daemon=True).start()

        def report():
            total = len(self.pages)
            done = len(self.done_pages)
            new = done - n_cached
            eta = None
            if new > 0 and done < total:
                eta = (time.time() - t0) / new * (total - done)
            q.put(("progress", done, total, eta, self.phase))

        while True:
            try:
                line = lines.get(timeout=0.5)
            except queue.Empty:
                line = None
            if line is not None:
                q.put(("log", line))
                m = _PAGE_DONE_RE.search(line)
                if m:
                    self.done_pages.add(int(m.group(1)))
                    report()
                if "Marker OCR on" in line:
                    self.phase = "OCR (Marker)..."
                elif "Compiling" in line:
                    self.phase = "Kompilacja LaTeX..."
                    self.done_pages |= set(self.pages)
                    report()
                elif "tex status" in line or "LaTeX failed" in line:
                    self.phase = "Składanie LaTeX (strony)..."
            if time.time() - last_scan > 2:
                last_scan = time.time()
                self.done_pages |= self._scan_files(None if not force else t0)
                report()
            if stop_reader.is_set() and lines.empty():
                break
        rc = self.proc.wait()
        result = None
        book = self.workdir / "book.pdf"
        if book.exists() and book.stat().st_mtime >= t0 - 1 and not self.stopping:
            self.output.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(book, self.output)
            result = self.output
            sp = self.workdir / "searchable.pdf"
            if sp.exists() and sp.stat().st_mtime >= t0 - 1:
                extra = self.output.with_name(self.output.stem + "_przeszukiwalny.pdf")
                shutil.copy2(sp, extra)
                q.put(("log", f"Skopiowano też przeszukiwalny skan: {extra}\n"))
            q.put(("log", f"Zapisano: {self.output}\n"))
        self.phase = "Zatrzymano" if self.stopping else ("Gotowe" if result else "Błąd")
        q.put(("done", rc, result, read_summary(self.workdir, self.pages)))

    def stop(self):
        """Czysto zatrzymuje całą grupę procesów (convert.py, marker_worker, llama-server)."""
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
        self.q.put(("log", "Zatrzymano (procesy zakończone).\n"))


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


# --------------------------------------------------------------------------- #
#  GUI (Tkinter + opcjonalnie tkinterdnd2)
# --------------------------------------------------------------------------- #

MODE_LABELS = {
    "math": "Matematyka / LaTeX (wzory, Marker)",
    "text": "Sam tekst (Tesseract, szybko)",
    "faithful": "Wierny układ strony",
    "layout": "Wierny układ strony",
}
LAYOUT_LABELS = {
    "faithful": "Wierny układ strony",
    "flow": "Ciągły tekst (domyślny)",
    "reflow": "Ciągły tekst (domyślny)",
    "default": "Domyślny",
    "auto": "Automatyczny",
}
FLAG_LABELS = {
    "newpage": "Każda strona źródła od nowej strony",
    "no-images": "Bez obrazów (nie wycinaj ilustracji)",
    "force": "Wymuś ponowny OCR (nie wznawiaj)",
    "skip-ocr": "Tylko przebuduj PDF z gotowego tekstu (bez OCR)",
    "layout": "Wierny układ strony",
    "compare": "Porównanie ze skanem (tylko wierny układ)",
}
VALUE_LABELS = {"dpi": "DPI (tryb tekstowy)", "fakebold": "Pogrubienie czcionki (wierny układ)"}
FIXED = {"help", "out", "pages", "mode", "engine", "layout"}


def _labelled(choice: str, table: dict) -> str:
    return f"{table[choice]}  [{choice}]" if choice in table else choice


def _unlabel(text: str) -> str:
    m = re.search(r"\[([^\]]+)\]\s*$", text or "")
    return m.group(1) if m else (text or "")


def main_gui(argv: list[str]) -> None:
    import tkinter as tk
    from tkinter import ttk, filedialog, messagebox, scrolledtext

    try:
        from tkinterdnd2 import TkinterDnD, DND_FILES  # type: ignore
        root = TkinterDnD.Tk()
        dnd = True
    except Exception:
        root = tk.Tk()
        dnd = False
    app = App(root, dnd, tk, ttk, filedialog, messagebox, scrolledtext)
    if len(argv) > 1 and argv[1].lower().endswith(".pdf"):
        app.set_input(Path(argv[1]))
    root.mainloop()


class App:
    def __init__(self, root, dnd, tk, ttk, filedialog, messagebox, scrolledtext):
        self.root, self.dnd, self.tk, self.ttk = root, dnd, tk, ttk
        self.fd, self.mb = filedialog, messagebox
        self.cfg = load_config()
        self.events: queue.Queue = queue.Queue()
        self.job: Job | None = None
        self.result: Path | None = None
        self.opts: list[Opt] = []
        self.input: Path | None = None
        self.output: Path | None = None
        self.vars: dict = {}
        self.t_start = 0.0

        root.title("pdfmaker – skan PDF -> LaTeX/PDF")
        root.geometry(self.cfg.get("geometry", "900x840"))
        root.minsize(720, 600)
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except Exception:
            pass
        style.configure("Postep.Horizontal.TProgressbar", background="#3c7dd9", thickness=18)
        import tkinter.font as tkfont
        big = tkfont.nametofont("TkDefaultFont").copy()
        big.configure(size=max(12, abs(int(big.cget("size"))) + 2))
        self._bigfont = big

        main = ttk.Frame(root, padding=10)
        main.pack(fill="both", expand=True)

        # --- folder skryptu
        sf = ttk.Frame(main)
        sf.pack(fill="x")
        ttk.Label(sf, text="Skrypt pdfmaker:").pack(side="left")
        self.script_lbl = ttk.Label(sf, text="", foreground="#555")
        self.script_lbl.pack(side="left", padx=6, fill="x", expand=True)
        ttk.Button(sf, text="Wskaż folder skryptu...", command=self.pick_script_dir).pack(side="right")

        # --- strefa upuszczania
        self.drop = tk.Label(main, text="", relief="groove", bd=2, height=4, bg="#eef3fb",
                             font=big, cursor="hand2", wraplength=780, justify="center")
        self.drop.pack(fill="x", pady=(8, 4))
        self.drop.bind("<Button-1>", lambda e: self.pick_input())
        fr = ttk.Frame(main)
        fr.pack(fill="x")
        ttk.Button(fr, text="Wybierz plik...", command=self.pick_input).pack(side="left")
        self.info_lbl = ttk.Label(fr, text="")
        self.info_lbl.pack(side="left", padx=8)
        self._drop_text()
        if dnd:
            from tkinterdnd2 import DND_FILES  # type: ignore
            for w in (self.drop, root):
                w.drop_target_register(DND_FILES)
                w.dnd_bind("<<Drop>>", self.on_drop)
                w.dnd_bind("<<DropEnter>>", lambda e: (self.drop.configure(bg="#d4e6ff"), e.action)[1])
                w.dnd_bind("<<DropLeave>>", lambda e: (self.drop.configure(bg="#eef3fb"), e.action)[1])

        # --- opcje (budowane dynamicznie)
        self.opt_frame = ttk.LabelFrame(main, text="Opcje", padding=8)
        self.opt_frame.pack(fill="x", pady=8)

        # --- wynik
        of = ttk.Frame(main)
        of.pack(fill="x")
        ttk.Button(of, text="Zapisz jako...", command=self.pick_output).pack(side="left")
        self.out_lbl = ttk.Label(of, text="(wynik: obok pliku źródłowego)", foreground="#555")
        self.out_lbl.pack(side="left", padx=8, fill="x", expand=True)

        # --- przyciski
        bf = ttk.Frame(main)
        bf.pack(fill="x", pady=8)
        self.start_btn = ttk.Button(bf, text="Start", command=self.start)
        self.start_btn.pack(side="left")
        self.stop_btn = ttk.Button(bf, text="Stop", command=self.stop, state="disabled")
        self.stop_btn.pack(side="left", padx=6)
        self.open_pdf_btn = ttk.Button(bf, text="Otwórz PDF", command=self.open_pdf, state="disabled")
        self.open_pdf_btn.pack(side="right")
        self.open_dir_btn = ttk.Button(bf, text="Otwórz folder", command=self.open_dir, state="disabled")
        self.open_dir_btn.pack(side="right", padx=6)

        # --- postęp
        self.pbar = ttk.Progressbar(main, mode="determinate", maximum=100, style="Postep.Horizontal.TProgressbar")
        self.pbar.pack(fill="x")
        self.status = ttk.Label(main, text="Gotowy.")
        self.status.pack(fill="x", pady=(4, 0))
        self.summary = ttk.Label(main, text="", foreground="#225522", wraplength=820)
        self.summary.pack(fill="x")

        # --- log
        lf = ttk.LabelFrame(main, text="Dziennik", padding=4)
        lf.pack(fill="both", expand=True, pady=(6, 0))
        self.log = scrolledtext.ScrolledText(lf, height=12, font=("TkFixedFont", 9), state="disabled",
                                             wrap="none")
        self.log.pack(fill="both", expand=True)

        root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.load_script_dir(find_script_dir(self.cfg))
        root.after(100, self.poll)

    # ---------- folder skryptu / opcje
    def load_script_dir(self, d: Path | None):
        self.script_dir = d
        for w in self.opt_frame.winfo_children():
            w.destroy()
        self.vars = {}
        if not d:
            self.script_lbl.configure(text="NIE ZNALEZIONO – wskaż folder z convert.sh", foreground="#b00")
            self.opts = []
            self.start_btn.configure(state="disabled")
            return
        self.script_lbl.configure(text=str(d), foreground="#555")
        self.status.configure(text="Odczytuję opcje skryptu...")
        self.root.update_idletasks()
        self.opts, _ = read_script_options(d)
        if not self.opts:  # minimalny zestaw, gdy --help zawiedzie
            self.opts = [Opt("pages", "value"), Opt("mode", "choice", ["math", "text"]),
                         Opt("engine", "choice", ["xelatex", "lualatex", "pdflatex"]),
                         Opt("newpage", "flag"), Opt("no-images", "flag"), Opt("force", "flag")]
            self._log("Uwaga: nie udało się odczytać --help skryptu, używam podstawowych opcji.\n")
        self.build_options()
        self.start_btn.configure(state="normal")
        self.status.configure(text="Gotowy.")

    def build_options(self):
        ttk, tk = self.ttk, self.tk
        saved = self.cfg.get("options", {})
        f = self.opt_frame
        byname = {o.name: o for o in self.opts}
        row = 0

        def combo_row(label, o, table, default):
            nonlocal row
            ttk.Label(f, text=label).grid(row=row, column=0, sticky="w", pady=2)
            vals = [_labelled(c, table) for c in o.choices]
            if default is None:
                vals = ["(domyślnie)"] + vals
            v = tk.StringVar()
            sv = saved.get(o.name)
            pick = next((x for x in vals if _unlabel(x) == sv), None) or \
                next((x for x in vals if _unlabel(x) == default), vals[0])
            v.set(pick)
            cb = ttk.Combobox(f, textvariable=v, values=vals, state="readonly", width=44)
            cb.grid(row=row, column=1, sticky="w", pady=2)
            self.vars[o.name] = ("choice", v)
            row += 1

        # zakres stron
        if "pages" in byname:
            ttk.Label(f, text="Zakres stron:").grid(row=row, column=0, sticky="w", pady=2)
            pv = tk.StringVar(value="")
            e = ttk.Entry(f, textvariable=pv, width=20)
            e.grid(row=row, column=1, sticky="w", pady=2)
            ttk.Label(f, text="np. 10-20 lub 1,3,5-7 (puste = wszystkie)", foreground="#666").grid(
                row=row, column=2, sticky="w", padx=6)
            pv.trace_add("write", lambda *a: self._update_info())
            self.vars["pages"] = ("value", pv)
            row += 1
        if "mode" in byname and byname["mode"].kind == "choice":
            combo_row("Tryb:", byname["mode"], MODE_LABELS, byname["mode"].choices[0])
        if "layout" in byname:
            o = byname["layout"]
            if o.kind == "choice":
                combo_row("Układ strony:", o, LAYOUT_LABELS, None)
            elif o.kind == "flag":
                v = tk.BooleanVar(value=bool(saved.get("layout", False)))
                ttk.Checkbutton(f, text=FLAG_LABELS["layout"], variable=v).grid(
                    row=row, column=1, sticky="w")
                self.vars["layout"] = ("flag", v)
                row += 1
            else:
                ttk.Label(f, text="Układ strony:").grid(row=row, column=0, sticky="w")
                v = tk.StringVar(value=saved.get("layout", ""))
                ttk.Entry(f, textvariable=v, width=20).grid(row=row, column=1, sticky="w")
                self.vars["layout"] = ("value", v)
                row += 1
        if "engine" in byname and byname["engine"].kind == "choice":
            combo_row("Silnik LaTeX:", byname["engine"], {}, byname["engine"].choices[0])

        # pozostałe opcje z wartością / wyborem (np. --dpi) i flagi
        for o in self.opts:
            if o.name in FIXED or o.kind == "flag":
                continue
            if o.kind == "choice":
                combo_row(VALUE_LABELS.get(o.name, "--" + o.name) + ":", o, {}, None)
            else:
                ttk.Label(f, text=VALUE_LABELS.get(o.name, "--" + o.name) + ":").grid(
                    row=row, column=0, sticky="w", pady=2)
                v = tk.StringVar(value=str(saved.get(o.name, "")))
                ttk.Entry(f, textvariable=v, width=10).grid(row=row, column=1, sticky="w", pady=2)
                ttk.Label(f, text="puste = domyślnie", foreground="#666").grid(row=row, column=2, sticky="w", padx=6)
                self.vars[o.name] = ("value", v)
                row += 1
        flags = [o for o in self.opts if o.kind == "flag" and o.name not in FIXED]
        if flags:
            ff = ttk.Frame(f)
            ff.grid(row=row, column=0, columnspan=3, sticky="w", pady=(6, 0))
            for i, o in enumerate(flags):
                v = tk.BooleanVar(value=bool(saved.get(o.name, False)) if o.name != "force" else False)
                lbl = FLAG_LABELS.get(o.name) or (o.help[:60] if o.help else "--" + o.name)
                ttk.Checkbutton(ff, text=lbl, variable=v).grid(row=i // 2, column=i % 2, sticky="w", padx=(0, 18))
                self.vars[o.name] = ("flag", v)
        f.columnconfigure(2, weight=1)

    def values(self) -> dict:
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

    def pick_script_dir(self):
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

    # ---------- plik wejściowy / wyjściowy
    def _drop_text(self):
        if self.input:
            self.drop.configure(text=f"{self.input.name}\n{self.input.parent}")
        else:
            self.drop.configure(text=("Przeciągnij tutaj plik PDF\nalbo kliknij, aby wybrać" if self.dnd
                                      else "Kliknij, aby wybrać plik PDF\n(przeciąganie niedostępne – brak tkinterdnd2)"))

    def on_drop(self, event):
        self.drop.configure(bg="#eef3fb")
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
        f = self.fd.askopenfilename(title="Wybierz skan PDF", initialdir=self.cfg.get("last_dir", str(Path.home())),
                                    filetypes=[("Pliki PDF", "*.pdf *.PDF"), ("Wszystkie pliki", "*")])
        if f:
            self.set_input(Path(f))

    def set_input(self, p: Path):
        self.input = p.resolve()
        self.cfg["last_dir"] = str(self.input.parent)
        self.total_pages = pdf_page_count(self.input)
        if not self.output or self.output_auto:
            self.output = self.input.with_name(self.input.stem + "_latex.pdf")
            self.output_auto = True
        self._drop_text()
        self._update_info()
        self._update_out()
        self.result = None
        self.open_pdf_btn.configure(state="disabled")
        self.open_dir_btn.configure(state="disabled")
        self.summary.configure(text="")

    output_auto = True
    total_pages = None

    def _update_info(self):
        if not self.input:
            return
        tp = self.total_pages
        txt = f"{tp} stron" if tp else "liczba stron nieznana"
        spec = self.vars.get("pages", (None, None))[1]
        if tp and spec is not None and spec.get().strip():
            try:
                txt += f", wybrano {len(parse_pages(spec.get(), tp))}"
            except Exception:
                txt += ", błędny zakres!"
        self.info_lbl.configure(text=txt)

    def _update_out(self):
        if self.output:
            wd = work_dir_for(self.output)
            self.out_lbl.configure(text=f"{self.output}   (folder roboczy: {wd.name}/)", foreground="#000")

    def pick_output(self):
        init_dir = str(self.output.parent if self.output else self.cfg.get("last_dir", Path.home()))
        init = self.output.name if self.output else "wynik.pdf"
        f = self.fd.asksaveasfilename(title="Zapisz wynik jako...", initialdir=init_dir, initialfile=init,
                                      defaultextension=".pdf", filetypes=[("PDF", "*.pdf")])
        if f:
            self.output = Path(f).resolve()
            if self.output.suffix.lower() != ".pdf":
                self.output = self.output.with_suffix(".pdf")
            self.output_auto = False
            self._update_out()

    # ---------- uruchamianie
    def start(self):
        if self.job and self.job.is_alive():
            return
        if not self.script_dir:
            self.mb.showerror("pdfmaker", "Najpierw wskaż folder skryptu pdfmaker.")
            return
        if not self.input or not self.input.exists():
            self.mb.showerror("pdfmaker", "Wybierz lub przeciągnij plik PDF.")
            return
        vals = self.values()
        if self.total_pages and vals.get("pages"):
            try:
                if not parse_pages(vals["pages"], self.total_pages):
                    raise ValueError
            except Exception:
                self.mb.showerror("pdfmaker", "Błędny zakres stron (np. 10-20 albo 1,3,5-7).")
                return
        if vals.get("layout") == "faithful" and vals.get("mode") not in (None, "", "math"):
            self.mb.showerror("pdfmaker", "Wierny układ strony wymaga trybu 'Matematyka / LaTeX'.\n"
                                          "Zmień tryb albo wybierz inny układ.")
            return
        if not self.output:
            self.output = self.input.with_name(self.input.stem + "_latex.pdf")
        self.cfg["options"] = {k: v for k, v in vals.items() if k not in ("force",)}
        save_config(self.cfg)
        self._clear_log()
        self.summary.configure(text="")
        self.result = None
        self.open_pdf_btn.configure(state="disabled")
        self.open_dir_btn.configure(state="disabled")
        self.pbar.configure(value=0, mode="determinate")
        self.job = Job(self.script_dir, self.input, self.output, vals, self.opts, self.events)
        if not self.job.pages:
            self.pbar.configure(mode="indeterminate")
            self.pbar.start(80)
        self.t_start = time.time()
        self.job.start()
        self.start_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")
        self.status.configure(text="Uruchamiam...")

    def stop(self):
        if self.job and self.job.is_alive():
            self.status.configure(text="Zatrzymuję (czekam na zakończenie procesów)...")
            self.stop_btn.configure(state="disabled")
            self.job.stop()

    def poll(self):
        try:
            while True:
                ev = self.events.get_nowait()
                kind = ev[0]
                if kind == "log":
                    self._log(ev[1])
                elif kind == "progress":
                    _, done, total, eta, phase = ev
                    el = fmt_dur(time.time() - self.t_start)
                    if total:
                        pct = 100.0 * done / total
                        if phase.startswith("Kompilacja"):
                            pct = 97
                        self.pbar.configure(value=pct)
                        txt = f"{phase}  strony: {done}/{total}  ({pct:.0f}%)  czas: {el}"
                        if eta:
                            txt += f"  pozostało ok. {fmt_dur(eta)}"
                    else:
                        txt = f"{phase}  czas: {el}"
                    self.status.configure(text=txt)
                elif kind == "done":
                    self._finished(*ev[1:])
        except queue.Empty:
            pass
        if self.job and self.job.is_alive() and self.job.stopping:
            self.status.configure(text="Zatrzymuję (czekam na zakończenie procesów)...")
        self.root.after(150, self.poll)

    def _finished(self, rc, result, summary):
        self.pbar.stop()
        self.pbar.configure(mode="determinate")
        self.start_btn.configure(state="normal")
        self.stop_btn.configure(state="disabled")
        el = fmt_dur(time.time() - self.t_start)
        stopped = self.job.stopping if self.job else False
        self.open_dir_btn.configure(state="normal")
        if result:
            self.result = result
            self.pbar.configure(value=100)
            self.open_pdf_btn.configure(state="normal")
            self.status.configure(text=f"Gotowe w {el}.  Zapisano: {result}")
        elif stopped:
            self.status.configure(text=f"Zatrzymano po {el}. Ponowny Start z tym samym plikiem wynikowym wznowi pracę.")
        else:
            self.status.configure(text=f"Błąd (kod {rc}) po {el} – zobacz dziennik.")
        self.summary.configure(text=("Podsumowanie: " + summary_text(summary)) if summary else "")
        try:
            self.root.bell()
        except Exception:
            pass

    def open_pdf(self):
        if self.result and self.result.exists():
            open_path(self.result)

    def open_dir(self):
        if self.output:
            open_path(self.output.parent if self.output.parent.exists() else work_dir_for(self.output))

    # ---------- dziennik
    def _log(self, s: str):
        self.log.configure(state="normal")
        self.log.insert("end", s)
        if int(self.log.index("end-1c").split(".")[0]) > 5000:
            self.log.delete("1.0", "1000.0")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _clear_log(self):
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")

    def on_close(self):
        if self.job and self.job.is_alive():
            if not self.mb.askyesno("pdfmaker", "Konwersja trwa. Zatrzymać ją i zamknąć okno?\n"
                                                "(postęp zostanie zachowany – można wznowić)"):
                return
            self.job.stop()
            self.job.join(timeout=30)
        self.cfg["geometry"] = self.root.geometry()
        save_config(self.cfg)
        self.root.destroy()


if __name__ == "__main__":
    main_gui(sys.argv)
