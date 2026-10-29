# pdfmaker GUI – okienko do skryptu pdfmaker

Osobny, prosty program okienkowy, który **tylko uruchamia** skrypt `pdfmaker/convert.sh`
(skan PDF → tekst + wzory LaTeX → PDF). Sam nie robi OCR – całą pracę wykonuje skrypt.

## Układ folderów

```
coś/
├── pdfmaker/        ← skrypt (convert.sh, convert.py, venv, llama …)
└── pdfmaker_gui/    ← ten program (pdfmaker_gui.py, gui.sh, …)
```

GUI samo znajduje skrypt w `../pdfmaker`. Jeśli leży gdzie indziej, kliknij
**„Wskaż folder skryptu…”** i wybierz folder z `convert.sh` – wybór zostanie zapamiętany.

## Instalacja (Fedora 44)

```bash
cd pdfmaker && ./install_fedora.sh            # najpierw sam skrypt (jeśli jeszcze nie)
cd ../pdfmaker_gui && ./install_fedora_gui.sh # Tkinter, tkinterdnd2, skrót w menu
```

`install_fedora_gui.sh` instaluje `python3-tkinter tk poppler-utils xdg-utils`, tworzy
`.venv` z pakietem `tkinterdnd2` (przeciąganie plików) i dodaje skrót **pdfmaker** do menu
aplikacji GNOME (`~/.local/share/applications/pdfmaker.desktop`, z pełną ścieżką do `gui.sh`).

## Uruchamianie

```bash
./gui.sh                 # albo z menu aplikacji: „pdfmaker”
./gui.sh ksiazka.pdf     # od razu z plikiem
```

## Jak używać

1. **Przeciągnij plik PDF** na niebieskie pole (albo kliknij je / „Wybierz plik…”).
   Obok pojawi się liczba stron.
2. **Opcje:**
   - *Zakres stron* – np. `10-20` albo `1,3,5-7`; puste = cała książka.
   - *Tryb* – **Matematyka / LaTeX** (wzory, Marker; ok. 1–6 min/stronę) albo
     **Sam tekst** (Tesseract, szybko, bez wzorów).
   - *Układ strony* – **Wierny układ strony** (`--layout faithful`: każda strona jak w skanie;
     wymaga trybu Matematyka) albo domyślny ciągły tekst.
   - *Silnik LaTeX*, *Każda strona od nowej strony*, *Bez obrazów*, *Wymuś ponowny OCR* itd.
   Lista opcji jest czytana ze skryptu (`convert.py --help`) przy starcie, więc nowe
   opcje skryptu pojawiają się w oknie same.
3. **Zapisz jako…** – wybierz, gdzie zapisać wynikowy PDF (domyślnie `<nazwa>_latex.pdf`
   obok skanu). Obok powstaje folder roboczy `<wynik>_praca/` (strony, `book.tex`,
   `book.md`, `report.json`). W trybie *Sam tekst* dodatkowo `<wynik>_przeszukiwalny.pdf`.
4. **Start** – pasek postępu (strony gotowe / wszystkie), szacowany czas do końca i dziennik.
5. **Stop** – czysto zatrzymuje skrypt razem z OCR i `llama-server`. Ponowny **Start**
   z tym samym plikiem wynikowym **wznawia** pracę (gotowe strony nie są OCR-owane ponownie).
6. Po zakończeniu: **Otwórz PDF**, **Otwórz folder** i podsumowanie z `report.json`
   (ile stron OK / częściowo / jako surowy tekst).

Ostatni folder i opcje są zapamiętywane w `~/.config/pdfmaker_gui.json`.

## Uwagi (Fedora / GNOME / Wayland)

- Okno Tk działa przez **XWayland** (domyślnie włączony w GNOME). Przeciąganie z Nautilusa
  do okna zwykle działa; jeśli w Twojej sesji nie zadziała, użyj „Wybierz plik…”
  albo przeciągnij PDF na ikonę „pdfmaker” / uruchom `./gui.sh plik.pdf`.
- Bez `tkinterdnd2` program działa dalej – tylko bez przeciągania (zostaje „Wybierz plik…”).
- Okna wyboru plików to okna Tk (nie systemowe okna GNOME) – wyglądają skromniej, ale działają.
- Zamknięcie okna w trakcie pracy pyta o zatrzymanie; postęp zostaje zachowany.
