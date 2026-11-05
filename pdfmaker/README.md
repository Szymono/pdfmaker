# pdfmaker – skan PDF (same obrazy) → Markdown + LaTeX → tekstowy PDF

Działa w 100% lokalnie i offline (po pierwszym pobraniu modeli). Bez płatnych API i bez zewnętrznych LLM-ów.

## Jak to działa
1. `qpdf` wycina wybrane strony (bez renderowania całego dokumentu).
2. **Tryb `math` (domyślny)** – [Marker](https://github.com/VikParuchuri/marker) 2.0 + [Surya OCR](https://github.com/VikParuchuri/surya)
   (lokalny model OCR `datalab-to/surya-ocr-2` w GGUF, uruchamiany przez `llama-server` z
   [llama.cpp](https://github.com/ggml-org/llama.cpp), CPU). Daje Markdown z polskim tekstem i wzorami `$...$` / `$$...$$`.
   Modele ładowane są raz na cały przebieg; każda strona osobno (błąd jednej strony nie przerywa reszty –
   taka strona idzie do tesseracta). Rysunki wycinane są ze skanu i zapisywane w `pages/img/` (wyłącz: `--no-images`).
3. **Tryb `text`** – `tesseract -l pol` (szybki, bez wzorów). Dodatkowo powstaje `searchable.pdf`
   (oryginalny skan + niewidoczna warstwa tekstu, do wyszukiwania/kopiowania).
4. `pandoc` zamienia każdą stronę na fragment LaTeX; każdy fragment jest testowo kompilowany.
   Jeśli strona się nie kompiluje → próba akapit po akapicie (zepsute akapity jako surowy tekst, oznaczone
   na czerwono) → w ostateczności cała strona jako verbatim. Potem `book.tex` → `book.pdf` (xelatex, Latin Modern,
   `babel` polish, `unicode-math`; zdefiniowane polskie operatory `\tg \ctg \arctg \sh \ch …`).

## Użycie
```bash
cd /workspace/pdfmaker
./convert.sh ksiazka.pdf --pages 10-20 --out wynik/          # wzory jako LaTeX (wolne, ~1–3 min/str. na CPU)
./convert.sh ksiazka.pdf --pages 10-20 --out wynik_txt --mode text   # sam tekst (~1–10 s/str.)
./convert.sh ksiazka.pdf --out wynik --skip-ocr               # tylko przebuduj tex/pdf po ręcznej edycji pages/*.md
./convert.sh ksiazka.pdf --out wynik --engine pdflatex        # inny silnik (xelatex|lualatex|pdflatex)
./convert.sh ksiazka.pdf --out wynik --newpage                # każda strona źródła od nowej strony PDF
./convert.sh ksiazka.pdf --out wynik --force                  # wymuś ponowny OCR
./convert.sh ksiazka.pdf --pages 16,17 --out wynik --force    # ponowny OCR tylko wybranych stron (reszta zostaje)
./convert.sh ksiazka.pdf --out wynik --no-images              # bez wycinania rysunków (stare zachowanie)
./extract_page.sh ksiazka.pdf 93 strona93.pdf                 # jedna strona (lub zakres 10-20)
./preview.sh wynik/book.pdf 1 110 podglad.png                 # PNG jednej strony
./convert.sh ksiazka.pdf --pages 93 --out wynik_1do1 --layout faithful --compare   # układ strony 1:1 jak w skanie
```
`--pages` przyjmuje `5`, `10-20`, `1,3,7-9`. Ponowne uruchomienie z tym samym `--out` **wznawia** pracę
(strony z istniejącym `pages/page_NNNN.md` są pomijane) – można przerwać Ctrl-C i kontynuować.
Dla całej książki najlepiej w tle: `nohup ./convert.sh ksiazka.pdf --out wynik > wynik.log 2>&1 &`.

## Tryb `--layout faithful` – strona wyniku wygląda jak strona skanu
Domyślnie (`--layout flow`) tekst płynie ciągiem na stronach A4 (stare zachowanie, bez zmian).
Z `--layout faithful` **1 strona skanu = 1 strona wyniku** o tym samym rozmiarze, a każdy blok jest w tym samym miejscu:
- Marker zapisuje układ strony `pages/page_NNNN.json` (typ bloku, prostokąt w punktach PDF, Markdown z OCR danego bloku;
  także nagłówek/numer strony). Strony przerobione wcześniej bez `.json` są automatycznie OCR-owane jeszcze raz.
- `lib/layout.py` analizuje sam skan (OpenCV, 600 dpi), wszystko automatycznie:
  szare (rastrowe) tła pod nagłówkami i „Przykład …” → prostokąt z gradientem o zmierzonej jasności,
  ramki i długie linie → `tikz`, znaczniki na marginesie (● pełne kółko, ○ okrąg) → rysowane w tym samym miejscu,
  wysokość x / odstęp linii / grubość kreski każdego bloku → rozmiar czcionki, interlinia, pogrubienie
  (np. „Rozwiązanie”, „a)”, „b)” na początku akapitu, tekst w ramce), krój bezszeryfowy nagłówków wybierany
  przez porównanie szerokości (LM Sans Bold / Demi Cond / TeX Gyre Heros), tekst Latin Modern Roman 10, wzory Latin Modern Math,
  rysunki i tabele wycinane ze skanu (`pages/img/pNNNN_L*.png`) i wstawiane w oryginalnym miejscu,
  tusz nie objęty żadnym blokiem OCR (np. niewykryty rysunek) też jest wycinany jako obraz.
- Każdy blok jest stawiany bezwzględnie (`picture`, jednostka 1bp, linia bazowa pierwszego wiersza jak w skanie),
  szerokość = szerokość bloku w skanie. Akapit: pętla TeX-a dobiera rozmiar (±) tak, żeby liczba wierszy była jak w skanie
  i żeby blok nigdy nie wszedł na następny; wiersz pojedynczy: skalowany do szerokości ze skanu; wzór wyświetlany:
  wyśrodkowany, skalowany do szerokości, odstępy między wierszami `aligned`/`cases` dopasowane do wysokości.
  Łamanie wierszy wewnątrz akapitu może się różnić od oryginału.
- Tekst pochodzi wyłącznie z OCR (nic nie jest przepisywane ręcznie); blok, który się nie kompiluje, jest pokazany jako
  surowy tekst OCR (ciemnoczerwony) w swoim miejscu.
- `--compare` zapisuje w `compare/`: `page_NNNN.pdf` (pojedyncza strona), `page_NNNN_side.png` (oryginał | wynik),
  `page_NNNN_overlay.png` (oryginał czerwony, wynik niebieski, zgodne = czarne) oraz miary podobieństwa w `report.json`
  (`similarity`: `ssim_blur` – SSIM rozmytych obrazów, `ink_iou`/`ink_f1` – zgodność tuszu z tolerancją ~1.5 pt,
  `rows_corr`/`cols_corr` – korelacja profili poziomych/pionowych, czyli położenia wierszy i akapitów).
- `--fakebold auto|0|X` – grubość kreski: `auto` lekko pogrubia czcionki w stronę skanu (druk + skan „puchnie”),
  `0` = czyste, cienkie Latin Modern.
- Wymaga xelatex (lub lualatex) i pakietu `opencv-python-headless` w venv (`scikit-image` opcjonalnie).
- Czas: OCR jak zwykle (~60–75 s/str.), układ + kompilacja ok. 5 s/str.

## Wyniki (w katalogu `--out`)
- `pages/page_NNNN.md` – OCR strony (Markdown + LaTeX) – do ręcznej poprawki,
- `pages/img/pNNNN_Figure_K.png|jpg` – rysunki wycięte przez Marker ze skanu (192 dpi; PNG w skali szarości,
  JPG dla kolorowych), w `.md` jako `![](img/...)` (ścieżka względem `pages/`), w LaTeX przez `\graphicspath`
  (`pages/`), skalowane do max `\linewidth` × `0.8\textheight`, wyśrodkowane. Odwołanie do nieistniejącego pliku
  jest zastępowane widocznym napisem „brak pliku obrazu” (nie psuje kompilacji; liczone w `report.json`),
- `pages/page_NNNN.tex` – fragment LaTeX, `book.tex` / `book.pdf` / `book.md` – całość,
- (`--layout faithful`) `pages/page_NNNN.json` – bloki strony z OCR (typ, położenie, Markdown), `pages/img/pNNNN_L*.png` –
  rysunki wycięte do układu 1:1, `compare/` – porównania ze skanem (z `--compare`), w `report.json` dla strony:
  `layout` (zmierzony rozmiar czcionki, interlinia, liczba szarych teł/ramek/znaczników, wybrany krój nagłówków…)
  i `similarity`,
- `report.json` – czasy i status każdej strony (`tex: ok | partial(...) | verbatim | text | missing`, liczba obrazów);
  przy przebiegu na części stron dane pozostałych stron są zachowywane; `_summary` – podsumowanie wszystkich stron,
- `work/` – wycięte strony, logi (`marker_stderr.log`), `searchable.pdf` w trybie text.

## Instalacja (już wykonana na tym komputerze)
```bash
sudo apt install texlive-xetex texlive-luatex texlive-latex-extra texlive-lang-polish \
  texlive-fonts-recommended texlive-pictures fonts-texgyre lmodern pandoc tesseract-ocr tesseract-ocr-pol qpdf img2pdf poppler-utils
uv venv --python 3.12 venv && . venv/bin/activate
uv pip install torch torchvision --index-url https://download.pytorch.org/whl/cpu
uv pip install marker-pdf          # 2.0.0 (surya-ocr 0.22)
uv pip install opencv-python-headless scikit-image   # tylko dla --layout faithful / --compare
# llama-server (CPU) z https://github.com/ggml-org/llama.cpp/releases -> llama/llama-b11738/
```
Modele (~1.6 GB) pobierają się automatycznie przy pierwszym uruchomieniu do `~/.cache/huggingface`
i `~/.cache/datalab`; potem można pracować offline (`export HF_HUB_OFFLINE=1`).

## Ograniczenia
- CPU (8 rdzeni, brak GPU): ~60–70 s na prostą stronę, strony gęste od wzorów dłużej (patrz report.json).
  Cała książka (187 str.) to kilka godzin – zostawić na noc.
- RAM: ~4 GB dla potoku.
- OCR nie jest bezbłędny: zdarzają się pomylone diakrytyki wielkich liter (Ź/Ż), `[x]` (entier) jako `\lfloor x\rfloor`,
  gubione drobne elementy (indeksy, primy), układ wielowierszowych przekształceń (`aligned`) może być spłaszczony.
  Ilustracje/wykresy są wstawiane jako obrazki wycięte ze skanu (nie są wektoryzowane/OCR-owane); jeśli Marker
  nie wykryje rysunku jako bloku Figure/Picture, rysunek nie pojawi się. Sprawdzaj wynik z oryginałem.
- Uwaga: przebieg z `--pages` buduje `book.tex/pdf` tylko z tych stron; żeby złożyć całą książkę po poprawkach
  uruchom `./convert.sh ksiazka.pdf --out wynik --skip-ocr`.
- W trybie domyślnym ramki, szare tła nagłówków, numeracja i układ strony z oryginału nie są odwzorowywane – PDF to
  „czysty” tekst. Odwzorowuje je `--layout faithful` (patrz wyżej). Tam ograniczenia: tło rastrowe jest zastępowane
  gładkim gradientem; odstępy wewnątrz wzorów i dokładne łamanie wierszy różnią się od oryginału; jeśli OCR połączy
  lub zgubi fragment, blok zostaje w swoim miejscu, ale jego treść jest taka, jak dał OCR; wiersze z kilkoma
  wzorami obok siebie (np. „a) … b) … c) …”) są rozciągane na szerokość, ale kolumny nie są wyrównane jak w tabeli.
  Strony zdjęciowe (np. okładka, >30% ciemnych pikseli) są wstawiane w całości jako obraz skanu (tekst z OCR jest
  wtedy tylko w `book.md`). Na bardzo gęstych stronach z odpowiedziami (same wzory, np. str. 100) OCR łączy bloki
  i zgodność jest wyraźnie niższa (ssim ≈ 0,85, ink_f1 ≈ 0,52) – wzory mogą na siebie nachodzić.
- Long faithful books: XeTeX's ~9000 font-slot limit used to abort around ~200 pages (continuous OCR sizes + per-page FakeBold via `\addfontfeatures`/`\setmathfont`). Fixed by predeclaring a small set of FakeBold families, quantizing sizes to 0.25 pt, and compiling the book in ~120-page chunks merged with `qpdf`. `--skip-ocr` reuses existing `pages/page_NNNN.tex` and only rebuilds `book.tex`/`book.pdf`.
  Program kończy tylko własny `llama-server` (uruchomiony przez swój proces OCR); inne zadania na komputerze nie są ruszane.

## Zmiany (v2, tryb `--layout faithful`)
- Inline HTML z OCR (`<sup>`, `<sub>`, `<b>`, `<i>`, `<br>`, ...) jest zamieniany na prawdziwy LaTeX (`html_to_md` w convert.py).
- Listy `a) ... b) ...` nie są składane jako `itemize` (bez dodatkowych punktorów, bez zmniejszania czcionki): każdy wiersz
  skanu jest osobnym blokiem w swojej linii bazowej, a pozycje z jednego wiersza są komórkami w pozycjach x ze skanu.
- Waga: tekst zasadniczy regularny (FakeBold tylko ponad grubość regularnego LM), nagłówki bezszeryfowe pogrubione
  (wykrywane z grubości kreski vs. szerokość).  Poprawione wykrywanie linii bazowych (per wiersz).
- Bloki, które się nie kompilowały: niezbalansowane `\begin/\end` i nawiasy `{}` w formułach z OCR (`balance_math`),
  poziome linie `---` (rysowane ze skanu), `\resizebox` bez `*` (mylił wysokość z wysokością+głębokością).
- Minimalne skalowanie akapitu 80% (wcześniej 55%).
