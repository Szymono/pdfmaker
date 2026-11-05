# Jak przerobić zeskanowaną książkę (np. Skoczylasa) na tekstowy PDF z LaTeX-em

Skrypt `convert.sh` robi wszystko sam, lokalnie i bez płatnych API. Ty tylko go uruchamiasz i czekasz.

## 0. Instalacja

### Wersja A: ten komputer (Ubuntu/Debian)
Wszystko jest już zainstalowane, polecenia instalacji są w `README.md`.

### Wersja B: Fedora 44 (x86_64, sam procesor, bez GPU)
1. Rozpakuj paczkę ze skryptem, np. do `~/pdfmaker`, i wejdź do niej:
   ```bash
   mkdir -p ~/pdfmaker && cd ~/pdfmaker && unzip ~/Pobrane/pdfmaker_skrypt.zip
   ```
2. Uruchom instalator (zapyta o hasło do `sudo`):
   ```bash
   chmod +x install_fedora.sh && ./install_fedora.sh
   ```
   Instalator robi kolejno:
   - instaluje przez `dnf` TeX Live (XeLaTeX, LuaLaTeX, polski, `unicode-math`, Latin Modern), `pandoc-cli`,
     `tesseract` z językiem polskim, `qpdf`, `poppler-utils`, `pikepdf`, `uv` i czcionki DejaVu,
   - tworzy środowisko `venv` z Pythonem 3.12 (Fedora 44 ma nowszego Pythona, z którym Marker może nie działać,
     więc `uv` pobiera osobno 3.12) i instaluje `torch` (wersja CPU) oraz `marker-pdf==2.0.0`,
   - pobiera `llama-server` z [llama.cpp b11738](https://github.com/ggml-org/llama.cpp/releases/tag/b11738)
     do `llama/llama-b11738/`, czyli tam, gdzie szuka go `convert.sh`,
   - na koniec sprawdza, czy wszystkie programy są dostępne (`OK` / `BRAK`).
3. Potrzebne: ok. 6 GB na dysku (TeX Live ~3 GB, torch ~1 GB, modele OCR ~1.6 GB) i minimum 8 GB RAM.
4. Przy pierwszym przebiegu `convert.sh` pobierze modele OCR (~1.6 GB) do `~/.cache/huggingface` i `~/.cache/datalab`.
   Później działa offline (`export HF_HUB_OFFLINE=1`).
5. Dalej wszystko jak niżej, tylko ścieżki zamień na swoje (np. `~/pdfmaker` zamiast `/workspace/pdfmaker`,
   `~/ksiazki` zamiast `/workspace/ksiazki`). Czas: ok. 1 min na stronę na 8 rdzeniach, wolniej na słabszym laptopie.
6. Jeśli `llama-server` nie startuje, sprawdź brakujące biblioteki: `ldd llama/llama-b11738/llama-server | grep "not found"`
   i doinstaluj je przez `sudo dnf install`. Alternatywnie zainstaluj wersję z Fedory: `sudo dnf install llama-cpp`
   i uruchamianie z `LLAMA_CPP_BINARY=$(which llama-server) ./convert.sh ...`.

## 1. Przygotowanie
1. Skopiuj PDF książki na komputer ze skryptem, np. `/workspace/ksiazki/skoczylas.pdf`.
2. Wejdź do katalogu skryptu: `cd /workspace/pdfmaker`.
3. (Opcjonalnie) sprawdź, czy PDF to skan: `pdffonts skoczylas.pdf`. Pusta lista czcionek oznacza same obrazy, czyli to jest przypadek dla tego skryptu.

## 2. Próba na kilku stronach
Najpierw przepuść 2–3 strony, żeby zobaczyć jakość (każda strona trwa ok. 1 min):
```bash
./convert.sh /workspace/ksiazki/skoczylas.pdf --pages 93-95 --out /workspace/ksiazki/proba --newpage
./preview.sh /workspace/ksiazki/proba/book.pdf 1 110 podglad.png
```
Porównaj `podglad.png` z oryginałem (`./preview.sh skoczylas.pdf 93`).

## 3. Cała książka (w tle, bez pilnowania)
```bash
nohup ./convert.sh /workspace/ksiazki/skoczylas.pdf --out /workspace/ksiazki/wynik --newpage > /workspace/ksiazki/wynik.log 2>&1 &
```
- 187 stron to ok. 3–4 godziny na 8 rdzeniach CPU. Zostaw na noc.
- Postęp: `tail -f /workspace/ksiazki/wynik.log`.
- Jeśli przebieg się przerwie, uruchom to samo polecenie jeszcze raz. Gotowe strony są pomijane, a skrypt kontynuuje od miejsca przerwania.

## 4. Wynik
W katalogu `wynik/`:
- `book.pdf` – gotowa książka (tekst + wzory LaTeX + rysunki wycięte ze skanu),
- `pages/page_NNNN.md` – tekst każdej strony do ręcznych poprawek,
- `report.json` – status każdej strony (`ok`, `partial`, `verbatim`, `text`).

## 5. Poprawki
1. Popraw błąd w `pages/page_NNNN.md` (np. zła litera, wzór).
2. Przebuduj PDF bez ponownego OCR:
   ```bash
   ./convert.sh /workspace/ksiazki/skoczylas.pdf --out /workspace/ksiazki/wynik --newpage --skip-ocr
   ```
3. Jeśli strona wyszła bardzo źle, zrób jej OCR od nowa: `--pages 65 --force`, a potem całość przez `--skip-ocr`.

## 6. Zgodność stron z oryginałem (strona PDF = strona książki)
Żeby treść danej strony wyniku odpowiadała w miarę możliwości tej samej stronie oryginalnego PDF-a:
- **Zawsze dodawaj `--newpage`.** Wtedy każda strona skanu zaczyna się na nowej stronie wyniku i nic nie przechodzi
  z sąsiedniej strony. Bez tej opcji tekst płynie ciągiem (w Skoczylasie wychodzi ok. 93 strony zamiast 187).
- **Przetwarzaj całą książkę od strony 1** (bez `--pages`), żeby numeracja się nie przesunęła. Opcji `--pages` używaj
  tylko do prób i poprawek, a końcowy PDF zawsze składaj poleceniem z `--skip-ocr` bez `--pages`.
- Na początku każdej strony wyniku jest szary znacznik `--- [s. N] ---` z numerem strony źródłowej, więc łatwo ją porównać z oryginałem.
- **Ograniczenie:** strona wyniku jest A4, a krój i układ różnią się od skanu, więc gęsta strona może się nie zmieścić
  i przejść na drugą kartkę. W Skoczylasie z `--newpage` wychodzi 215 stron dla 187 stron źródła, czyli ok. 28 stron
  zajmuje po dwie kartki. Początek każdej strony zawsze jest zgodny z oryginałem. Dokładna zgodność 1:1 z numerem
  strony PDF nie jest gwarantowana.
- Ramki, nagłówki i dokładny układ ze skanu nie są w tym trybie odwzorowywane, odwzorowywana jest treść i jej kolejność.
  Jeśli zależy Ci na dokładnej zgodności 1:1 z układem stron, użyj `--layout faithful` (punkt 7).

## 7. Strona jak w oryginale: `--layout faithful` (cyfrowa książka 1:1 ze zdjęć/skanu)
Jeśli chcesz, żeby strona wyniku **wyglądała jak strona książki** (ten sam rozmiar kartki i marginesy, akapity
w tej samej kolejności i w tych samych miejscach, szare tła pod nagłówkami i pod „Przykład …”, kółka ● / ○ przy
przykładach i zadaniach, ramki, pogrubione „Rozwiązanie”, „a)”, „b)”, wyśrodkowane wzory, rysunki w swoim miejscu,
żywa pagina i numer strony), dodaj `--layout faithful`:
```bash
./convert.sh /workspace/ksiazki/skoczylas.pdf --pages 93 --out /workspace/ksiazki/uklad --layout faithful --compare
```
- Tekst i wzory są prawdziwe (da się je zaznaczać, kopiować, wyszukiwać) i pochodzą z OCR, a położenie, wielkość
  i wygląd bloków skrypt mierzy sam na skanie. Łamanie wierszy w akapicie może być trochę inne niż w oryginale.
- `--compare` tworzy w `uklad/compare/` obrazki do porównania: `page_0093_side.png` (lewa: oryginał, prawa: wynik),
  `page_0093_overlay.png` (czerwone = tylko oryginał, niebieskie = tylko wynik, czarne = zgadza się)
  i `page_0093.pdf` (sama ta strona). Liczby podobieństwa są w `report.json` (`similarity`) i w logu
  (`ssim`, `ink_f1`, `rows` – im bliżej 1, tym lepiej).
- Strony przerobione wcześniej zwykłym trybem są OCR-owane jeszcze raz (potrzebny jest układ bloków `page_NNNN.json`);
  folder z wynikiem zwykłego trybu najlepiej zostawić w spokoju i użyć nowego `--out`.
- Cała książka w tym trybie (w tle):
  ```bash
  nohup ./convert.sh /workspace/ksiazki/skoczylas.pdf --out /workspace/ksiazki/uklad --layout faithful > uklad.log 2>&1 &
  ```
  Każda strona źródła to dokładnie jedna strona wyniku (`--newpage` nie jest potrzebne). Czas: ~1 min OCR + ~5 s na stronę.
- Długie książki (`--layout faithful`): składanie idzie w porcjach ~120 stron (limit slotów czcionek XeLaTeX ~9000) i jest scalane przez `qpdf`. Po poprawkach w `pages/page_NNNN.tex` wystarczy `./convert.sh … --skip-ocr --layout faithful` (bez ponownego OCR).
- Po ręcznej poprawce tekstu w `pages/page_NNNN.json` (pole `"md"` bloku) przebuduj przez `--skip-ocr --layout faithful`.
- Jeśli czcionki wydają się za grube/za cienkie: `--fakebold 0` (cienkie, „czyste” Latin Modern) albo np. `--fakebold 1`.
- Okładka i inne strony zdjęciowe są wstawiane jako obraz całej strony. Strony złożone prawie wyłącznie ze wzorów
  (odpowiedzi) wychodzą słabiej – warto je obejrzeć w `compare/page_NNNN_side.png`.
- Na Fedorze do tego trybu potrzebny jest dodatkowo `opencv-python-headless` w venv (instalator `install_fedora.sh` już to robi).

## Inne przydatne polecenia
```bash
./extract_page.sh skoczylas.pdf 93 strona93.pdf     # wytnij jedną stronę oryginału
./convert.sh skoczylas.pdf --out wynik_txt --mode text   # szybko, sam tekst bez wzorów (tesseract)
python3 usun_obrazy.py book.pdf book_bez_obrazow.pdf     # usuń rysunki z gotowego PDF-a
```
