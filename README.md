# ScrApp.MB

Aplikacja okienkowa (Python + Tkinter), która pobiera strony z podanej listy, analizuje każdą w Claude (Anthropic) według naszego promptu i pisze raport końcowy. Każde wyszukanie zapisuje się w osobnym katalogu `wyszukania/<nazwa>/`.

## Instalacja

```
pip install -r requirements.txt
playwright install chromium
```

Utwórz plik `klucz_api.txt` obok `scrapp.py` i wklej do niego sam klucz Anthropic. Plik jest w `.gitignore` i nie trafia do repozytorium.

## Uruchomienie

Kliknij dwukrotnie `ScrApp.MB.bat` albo wpisz `python scrapp.py`.

Szczegóły obsługi są w pliku `instrukcja_ScrApp.MB.txt`.
