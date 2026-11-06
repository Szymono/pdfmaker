# pdfmaker GUI – okienko do skryptu pdfmaker

Osobny program okienkowy (Tk/ttk), który **tylko uruchamia** skrypt `pdfmaker/convert.sh`
(skan PDF → tekst + wzory LaTeX → PDF). Sam nie robi OCR – całą pracę wykonuje skrypt.
Skrypt działa też bez GUI (z terminala); GUI to cienka nakładka.

## Układ folderów

```
coś/
├── pdfmaker/        ← skrypt (convert.sh, convert.py, venv OCR, llama …)
└── pdfmaker_gui/    ← ten program (pdfmaker_gui.py, gui.sh, .venv GUI …)
```

GUI samo znajduje skrypt w `../pdfmaker`. Jeśli leży gdzie indziej, kliknij
**„Folder skryptu…”** i wybierz folder z `convert.sh` – wybór zostanie zapamiętany.

## Instalacja (Fedora 44)

```bash
cd pdfmaker && ./install_fedora.sh            # najpierw sam skrypt (jeśli jeszcze nie)
cd ../pdfmaker_gui && ./install_fedora_gui.sh # Tkinter, tkinterdnd2, ttkbootstrap, skrót w menu
```

`install_fedora_gui.sh` instaluje `python3-tkinter tk poppler-utils xdg-utils libnotify`,
tworzy **osobne** środowisko `pdfmaker_gui/.venv` (nie rusza venv OCR `pdfmaker/venv`) z pakietami:

- `tkinterdnd2` – przeciąganie plików (opcjonalne),
- `ttkbootstrap` – nowocześniejszy wygląd i motywy jasny/ciemny (opcjonalne).

Oba pakiety są **opcjonalne**: bez `ttkbootstrap` GUI startuje na zwykłym ttk (z własnym
motywem jasnym/ciemnym), bez `tkinterdnd2` – bez przeciągania. Ręcznie:
`pdfmaker_gui/.venv/bin/pip install ttkbootstrap`. Wymuszenie zwykłego ttk (np. do testów):
`PDFMAKER_GUI_NO_TTKBOOTSTRAP=1 ./gui.sh`.

Skrót **pdfmaker** trafia do menu aplikacji GNOME (`~/.local/share/applications/pdfmaker.desktop`).

## Uruchamianie

```bash
./gui.sh                 # albo z menu aplikacji: „pdfmaker”
./gui.sh ksiazka.pdf     # od razu z plikiem
```

## Układ okna

Góra (przewijana): ustawienia. Dół: aktywne zadanie, wyniki, dziennik. Podział można
przesuwać myszą (zapamiętywany).

- **Podstawowe** – plik źródłowy (przeciągnij na pole albo „Wybierz plik…”), zakres stron,
  tryb, układ strony, plik wynikowy („Zapisz jako…”).
- **Zaawansowane** (zwijane) – silnik LaTeX, DPI Tesseracta, pogrubienie (wierny układ),
  wymuszenie OCR, tylko przebudowa PDF, bez obrazów, porównanie ze skanem, każda strona od nowej.
- **Aktywne zadanie** – Start / Stop, bieżąca **faza**, pasek postępu fazy, czas, szacunek.
- **Wyniki** – stan końcowy, powody ostrzeżeń/błędu, podsumowanie z `report.json` i przyciski:
  Otwórz PDF, Folder wyniku, Folder roboczy, Raport (`report.json`), Porównanie (`compare/`),
  Pełny dziennik.
- **Dziennik** – podgląd na żywo.

Każda opcja ma **dymek z opisem** po polsku (najedź myszą). Opcje nieadekwatne do trybu są
**wyszarzone** i nie trafiają do skryptu (zgodnie z `convert.py`):

| Opcja | Kiedy aktywna |
|---|---|
| Układ „Wierny układ strony” | tylko tryb *Matematyka / LaTeX* (`--layout faithful` wymaga `--mode math`) |
| Porównanie ze skanem, Pogrubienie | tylko wierny układ |
| Każda strona od nowej strony | tylko ciągły tekst (w wiernym układzie każda strona i tak jest osobna) |
| Bez obrazów | tylko tryb *Matematyka* (Tesseract nie wycina ilustracji) |
| DPI Tesseracta | zawsze (tryb *Sam tekst* oraz awaryjny OCR, gdy Marker zawiedzie) |

Lista opcji jest czytana ze skryptu (`convert.py --help`) **w tle**, więc okno nie zamarza,
a nowe opcje skryptu pojawiają się same.

## Jak używać

1. **Plik PDF** – przeciągnij albo wybierz. Liczba stron liczy się w tle.
2. **Opcje** – zakres (`10-20`, `1,3,5-7`, `-5`, `400-`; puste = całość; błędny zakres
   jest od razu podświetlany), tryb, układ itd. Liczby (DPI, pogrubienie) są sprawdzane.
3. **Zapisz jako…** – domyślnie `<nazwa>_latex.pdf` obok skanu. Obok powstaje folder roboczy
   `<wynik>_praca/` (strony, `book.tex`, `book.md`, `report.json`, pełny dziennik GUI).
   W trybie *Sam tekst* dodatkowo `<wynik>_przeszukiwalny.pdf`.
4. **Start**. GUI najpierw sprawdza bezpieczeństwo ścieżek (niżej) i pyta o nadpisanie
   istniejącego pliku wynikowego. Parametry są **zamrażane**: w czasie pracy wybór pliku,
   wyniku i opcje są zablokowane, a przyciski wyników dotyczą zadania, które działa
   (albo ostatniego).
5. **Stop** – czysto zatrzymuje skrypt razem z OCR i `llama-server`. Ponowny **Start**
   z tym samym plikiem wynikowym **wznawia** pracę (gotowe strony nie są OCR-owane ponownie).
6. Po zakończeniu: powiadomienie systemowe (`notify-send`, jeśli jest) + sygnał dźwiękowy.

### Zabezpieczenia ścieżek

Start jest blokowany, gdy:

- plik wynikowy to ten sam plik co źródło (porównanie po `Path.resolve()`/`samefile`, także przez dowiązania),
- `<wynik>_przeszukiwalny.pdf` nadpisałby źródło,
- źródło leży w folderze roboczym wyniku, albo wynik w swoim folderze roboczym,
- folder roboczy istnieje jako plik albo nachodzi na folder skryptu (`venv`, `lib`).

Istniejący plik wynikowy jest podmieniany **atomowo i tylko po sukcesie** – przy błędzie
lub anulowaniu zostaje bez zmian.

### Stany końcowe

| Stan | Kiedy |
|---|---|
| **Ukończono** | kod wyjścia 0 **i** w tym przebiegu powstał niepusty `book.pdf` z poprawną liczbą stron, a `report.json` nie zgłasza problemów |
| **Ukończono z ostrzeżeniami** | jw., ale np. strony *częściowo*/*verbatim*/*brak OCR*, błędy OCR, brakujące obrazy, `ERROR` w dzienniku, niezgodna liczba stron (wierny układ), brak świeżego `report.json` |
| **Błąd** | kod ≠ 0, brak świeżego `book.pdf` (stary plik z poprzedniego uruchomienia **nie** liczy się jako wynik), pusty PDF, wyjątek. Świeży artefakt przy kodzie ≠ 0 zostaje w folderze roboczym, ale nie jest kopiowany jako wynik |
| **Anulowano** | zatrzymane przez użytkownika |

### Postęp

Pasek pokazuje postęp **bieżącej fazy**, nie procent całego przebiegu. Fazy odczytywane
z dziennika: *Przygotowanie → OCR (Marker/Tesseract) → Skład stron → Kompilacja PDF
(części i/n) → Scalanie → Porównanie ze skanem → Raport*. Gdy fazy nie da się zmierzyć,
pasek „pulsuje”. Szacowany czas dotyczy **tylko OCR** i jest oznaczony jako szacunek
(„bardzo niepewny” przy pierwszych stronach). Przy długim braku nowych komunikatów pojawia się
informacja – zadanie **nie** jest przerywane automatycznie.

### Dziennik

- Pełny dziennik zadania zapisuje się do `<wynik>_praca/pdfmaker_gui_RRRRMMDD_GGMMSS.log`
  (także traceback, jeśli coś pójdzie nie tak w GUI). Okno trzyma tylko ostatnie ~4000 linii.
- Przewinięcie w górę **wstrzymuje autoprzewijanie** („↓ Na dół” wraca do końca).
- „Zawijaj wiersze” albo poziomy pasek przewijania; „Kopiuj pełny dziennik” / „Otwórz plik dziennika”.

## Wygląd

- **Ciemny motyw** – przełącznik u góry (zapamiętywany).
- **A− / A+** (albo Ctrl −, Ctrl +, Ctrl 0) – skalowanie czcionek (zapamiętywane).

Ostatni folder, opcje, motyw, skala czcionek, rozmiar okna i podział paneli są zapamiętywane
w `~/.config/pdfmaker_gui.json` (zapis atomowy).

## Uwagi (Fedora / GNOME / Wayland)

- Okno Tk działa przez **XWayland** (domyślnie włączony w GNOME). Przeciąganie z Nautilusa
  do okna zwykle działa; jeśli w Twojej sesji nie zadziała, użyj „Wybierz plik…”
  albo przeciągnij PDF na ikonę „pdfmaker” / uruchom `./gui.sh plik.pdf`.
- Okna wyboru plików to okna Tk (nie systemowe okna GNOME) – wyglądają skromniej, ale działają.
- Zamknięcie okna w trakcie pracy pyta o zatrzymanie; okno **nie zamarza** – pokazuje
  „Zatrzymywanie…” i zamyka się po zakończeniu procesów (postęp zostaje zachowany).
  Drugie kliknięcie zamknięcia pozwala zamknąć od razu.
