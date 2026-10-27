"""ScrApp.MB - pobiera wskazane strony, analizuje je w Claude według promptu
i pisze raport końcowy. Każde wyszukanie ma własny katalog w "wyszukania"."""

import json
import os
import queue
import re
import threading
import tkinter as tk
import urllib.parse
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from tkinter.scrolledtext import ScrolledText

import anthropic
from playwright.sync_api import sync_playwright

# ---------------------------------------------------------
# USTAWIENIA
# ---------------------------------------------------------
MODEL = "claude-opus-5-5"

KATALOG_APLIKACJI = Path(__file__).resolve().parent
KATALOG_WYSZUKAN = KATALOG_APLIKACJI / "wyszukania"
PLIK_OSTATNICH = KATALOG_APLIKACJI / "ostatnie_ustawienia.json"
# Klucz Anthropic (np. "sk-ant-api03-...") w osobnym pliku, który nie trafia do repozytorium
PLIK_KLUCZA = KATALOG_APLIKACJI / "klucz_api.txt"

DOMENY_POMIJANE = {
    "accounts.google.com",
    "login.microsoftonline.com",
    "login.live.com",
    "appleid.apple.com",
}
ROZSZERZENIA_POMIJANE = (".pdf", ".jpg", ".jpeg", ".png", ".gif", ".svg", ".webp",
                         ".zip", ".mp4", ".mp3", ".doc", ".docx", ".xls", ".xlsx")
DRUGI_POZIOM = {"com", "net", "org", "edu", "gov", "co", "ac", "biz", "info"}
# Teksty stron weryfikacji ruchu (Cloudflare itp.) - takiej treści nie wysyłamy do AI
OZNAKI_BLOKADY = ("just a moment", "verify you are human", "checking your browser",
                  "access denied", "attention required", "enable javascript and cookies")
MIN_ZNAKOW = 200

DOMYSLNE = {
    "nazwa": "",
    "cel": "Szukamy platform, które z materiałów źródłowych automatycznie tworzą kursy "
           "(prezentacje, audio, wideo, awatary, tutor AI).",
    "strony": "",
    "prompt_cechy": """Opisz witrynę DOKŁADNIE według wzoru w Markdown:

**Trafność względem celu:** [tak / częściowo / nie] - jedno zdanie uzasadnienia

## Executive Summary
* **Główny cel i mechanika:** [zwięzły opis działania]
* **Segment rynku:** [np. B2B, uczniowie, twórcy kursów]
* **Ograniczenia i rynki:** [np. rynek PL, język angielski]

## Cechy
| Model płatności i cena | Dane wejściowe | Nakład pracy użytkownika |
|---|---|---|
| ... | ... | ... |

## Szczegóły
* **Wyróżnik (USP):**
* **Bariery/Braki:**""",
    "prompt_raport": """Na podstawie profili przygotuj raport końcowy w Markdown:
1. Krótkie podsumowanie (5-7 zdań).
2. Tabela zbiorcza wszystkich witryn: nazwa, trafność, segment, cena, wyróżnik.
3. Grupy podobnych rozwiązań i czym się różnią.
4. Luki na rynku i wnioski dla nas.
Pomiń witryny z trafnością "nie" w punktach 3-4, ale wymień je w tabeli.""",
    "wysilek": "medium",
    "podstrony": 5,
    "znakow": 8000,
    "slowa_linkow": "pricing, cennik, price, plans, features, funkcje, product, produkt, about, o-nas, solutions",
    "chrome": False,
    "pomijaj_gotowe": True,
}

API_KEY = PLIK_KLUCZA.read_text(encoding="utf-8").strip() if PLIK_KLUCZA.exists() else ""
if not API_KEY.startswith("sk-ant-"):
    tk.Tk().withdraw()
    messagebox.showerror("ScrApp.MB", "Brak klucza API.\nUtwórz plik klucz_api.txt obok scrapp.py\ni wklej do niego sam klucz.")
    raise SystemExit(1)

client = anthropic.Anthropic(api_key=API_KEY)


class Zatrzymano(Exception):
    pass


class Pominieto(Exception):
    pass


class Zablokowano(Exception):
    pass


def wyglada_na_blokade(tekst: str) -> bool:
    krotki = tekst.strip().lower()
    return len(krotki) < MIN_ZNAKOW or (len(krotki) < 2000 and any(o in krotki for o in OZNAKI_BLOKADY))


def domena_glowna(host: str) -> str:
    """app.firma.pl i www.firma.pl -> firma.pl"""
    czesci = host.lower().split(".")
    if len(czesci) >= 3 and czesci[-2] in DRUGI_POZIOM and len(czesci[-1]) == 2:
        return ".".join(czesci[-3:])
    return ".".join(czesci[-2:])


def bezpieczna_nazwa(tekst: str) -> str:
    return re.sub(r"[^\w.-]+", "_", tekst.strip(), flags=re.UNICODE).strip("_")[:80]


def lista_adresow(tekst: str) -> list[str]:
    """Jeden adres w linii; brak http:// jest uzupełniany, duplikaty usuwane."""
    adresy = []
    for linia in tekst.splitlines():
        linia = linia.strip().split()[0] if linia.strip() else ""
        if not linia or linia.startswith("#"):
            continue
        if not re.match(r"https?://", linia):
            linia = "https://" + linia
        if linia not in adresy:
            adresy.append(linia)
    return adresy


def zapytaj_claude(system: str, tresc: str, wysilek: str, max_tokens: int, myslenie: bool = False,
                   na_zywo=None, przerwij=None) -> str:
    """na_zywo(tekst) dostaje kolejne fragmenty odpowiedzi, przerwij() może rzucić wyjątek i przerwać strumień."""
    parametry = {}
    if myslenie:
        parametry["thinking"] = {"type": "adaptive"}
    with client.beta.messages.stream(
        model=MODEL,
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": tresc}],
        output_config={"effort": wysilek},
        # gdy filtr bezpieczeństwa odrzuci zapytanie, API samo ponawia je na modelu zapasowym
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
        **parametry,
    ) as strumien:
        for fragment in strumien.text_stream:
            if przerwij:
                przerwij()
            if na_zywo:
                na_zywo(fragment)
        odpowiedz = strumien.get_final_message()

    if odpowiedz.stop_reason == "refusal":
        raise RuntimeError("model odmówił analizy (refusal)")
    tekst = "".join(b.text for b in odpowiedz.content if b.type == "text")
    if odpowiedz.stop_reason == "max_tokens":
        tekst += "\n\n(odpowiedź ucięta - przekroczono limit długości)"
    return tekst or "(pusta odpowiedź modelu)"


# ---------------------------------------------------------
# PRACA W TLE: pobieranie, analiza, raport
# ---------------------------------------------------------
class Zadanie:
    """Sterowanie z interfejsu (threading.Event): stop - zakończ po bieżącej stronie,
    pomin - zrezygnuj z bieżącego kroku, teraz - zakończ natychmiast."""

    def __init__(self, ustawienia: dict, katalog: Path, log, postep, zywo,
                 stop: threading.Event, pomin: threading.Event, teraz: threading.Event):
        self.u = ustawienia
        self.katalog = katalog
        self.log = log
        self.postep = postep
        self.zywo = zywo  # zywo(tytul, tekst): tytul != None zaczyna nowy podgląd na żywo
        self.stop = stop
        self.pomin = pomin
        self.teraz = teraz

    def sprawdz_przerwanie(self):
        if self.teraz.is_set():
            raise Zatrzymano()
        if self.pomin.is_set():
            self.pomin.clear()
            raise Pominieto()

    def sprawdz_stop(self):
        if self.teraz.is_set() or self.stop.is_set():
            raise Zatrzymano()

    # --- pobieranie ---
    def pobierz_tekst(self, strona, url: str) -> str:
        strona.goto(url, wait_until="domcontentloaded", timeout=20000)
        try:
            strona.wait_for_load_state("networkidle", timeout=5000)
        except Exception:
            pass  # strony z ciągłym ruchem sieciowym - bierzemy to, co jest
        return strona.evaluate("document.body ? document.body.innerText : ''")

    def wybierz_podstrony(self, strona, domena: str, start: str) -> list[str]:
        linki = strona.eval_on_selector_all(
            "a[href]", "els => els.map(e => [e.href, (e.innerText || '').trim()])")
        slowa = [s.strip().lower() for s in self.u["slowa_linkow"].split(",") if s.strip()]
        kandydaci = {}
        for href, opis in linki:
            href = href.split("#")[0].rstrip("/")
            adres = urllib.parse.urlparse(href)
            host = adres.hostname or ""
            if adres.scheme not in ("http", "https") or not host or host in DOMENY_POMIJANE:
                continue
            if domena_glowna(host) != domena or href == start.rstrip("/"):
                continue
            if adres.path.lower().endswith(ROZSZERZENIA_POMIJANE):
                continue
            tekst = (href + " " + opis).lower()
            punkty = sum(1 for s in slowa if s in tekst)
            kandydaci[href] = max(punkty, kandydaci.get(href, 0))
        # najpierw linki pasujące do słów kluczowych, potem krótsze adresy (bliżej strony głównej)
        ranking = sorted(kandydaci, key=lambda h: (-kandydaci[h], len(h)))
        return ranking[: int(self.u["podstrony"])]

    def pobierz_witryne(self, kontekst, url: str) -> str:
        domena = domena_glowna(urllib.parse.urlparse(url).hostname or "")
        self.zywo(f"Pobieranie: {domena}", "")
        strona = kontekst.new_page()
        bufor, widziane = "", set()
        maks = int(self.u["znakow"])
        try:
            tekst = self.pobierz_tekst(strona, url)
            self.sprawdz_przerwanie()
            if wyglada_na_blokade(tekst):
                raise Zablokowano(tekst.strip()[:80] or "pusta strona")
            fragment = f"\n--- URL: {url} ---\n{tekst[:maks]}\n"
            bufor += fragment
            self.zywo(None, fragment)
            widziane.add(hash(tekst))
            self.log(f"   pobrano: {url} ({len(tekst)} znaków)")
            podstrony = self.wybierz_podstrony(strona, domena, url) if int(self.u["podstrony"]) > 0 else []
            for adres in podstrony:
                self.sprawdz_przerwanie()
                try:
                    tekst = self.pobierz_tekst(strona, adres)
                except Exception as blad:
                    self.log(f"   [pominięto] {adres}: {type(blad).__name__}")
                    continue
                self.sprawdz_przerwanie()
                if hash(tekst) in widziane or wyglada_na_blokade(tekst):
                    continue
                widziane.add(hash(tekst))
                fragment = f"\n--- URL: {adres} ---\n{tekst[:maks]}\n"
                bufor += fragment
                self.zywo(None, fragment)
                self.log(f"   pobrano: {adres} ({len(tekst)} znaków)")
        finally:
            strona.close()
        return bufor

    # --- analiza ---
    def system_analizy(self, domena: str) -> str:
        return (
            "Jesteś analitykiem badającym strony internetowe.\n"
            f"CEL BADANIA (czego szukamy): {self.u['cel']}\n\n"
            f"Otrzymasz teksty pobrane z witryny {domena}. Opieraj się wyłącznie na nich; "
            "gdy jakiejś informacji brak, napisz 'brak danych'. Ignoruj marketingowy żargon.\n\n"
            f"WYTYCZNE ANALIZY:\n{self.u['prompt_cechy']}"
        )

    def uruchom(self, tylko_raport: bool):
        try:
            if not tylko_raport:
                self.analizuj_strony()
            self.pomin.clear()
            try:
                self.raport_koncowy()
            except Pominieto:
                self.log("\n[POMINIĘTO] Raport końcowy nie został napisany.")
            self.log("\n[GOTOWE] Wyniki w katalogu: " + str(self.katalog))
        except Zatrzymano:
            self.log("\n[ZATRZYMANO] Zrobione kroki są zapisane; ponowny Start wznowi pracę.")
        except Exception as blad:
            self.log(f"\n[BŁĄD] {type(blad).__name__}: {blad}")
        self.zywo("brak aktywnego kroku", "")

    def analizuj_strony(self):
        adresy = lista_adresow(self.u["strony"])
        if not adresy:
            raise RuntimeError("lista stron jest pusta")
        (self.katalog / "surowe").mkdir(exist_ok=True)
        (self.katalog / "profile").mkdir(exist_ok=True)

        with sync_playwright() as p:
            if self.u["chrome"]:
                self.log("Łączenie z Chrome na porcie 9222 (uruchom go przez uruchom.bat z katalogu wyżej)...")
                przegladarka = p.chromium.connect_over_cdp("http://localhost:9222")
                kontekst = przegladarka.contexts[0]
            else:
                przegladarka = p.chromium.launch(headless=True)
                kontekst = przegladarka.new_context(locale="pl-PL")
            try:
                for nr, url in enumerate(adresy, 1):
                    self.pomin.clear()  # rezygnacja dotyczy tylko kroku, w którym ją kliknięto
                    self.sprawdz_stop()
                    self.postep(nr - 1, len(adresy))
                    self.log(f"\n[{nr}/{len(adresy)}] {url}")
                    try:
                        self.przetworz_witryne(kontekst, url)
                    except Pominieto:
                        self.log("   [POMINIĘTO] na żądanie - przechodzę do następnej strony")
                self.postep(len(adresy), len(adresy))
            finally:
                if not self.u["chrome"]:
                    przegladarka.close()

    def przetworz_witryne(self, kontekst, url: str):
        domena = domena_glowna(urllib.parse.urlparse(url).hostname or "")
        nazwa = bezpieczna_nazwa(domena)
        plik_profilu = self.katalog / "profile" / f"{nazwa}.md"
        plik_surowy = self.katalog / "surowe" / f"{nazwa}.txt"

        if self.u["pomijaj_gotowe"] and plik_profilu.exists():
            self.log("   profil już istnieje - pomijam")
            return

        if self.u["pomijaj_gotowe"] and plik_surowy.exists():
            bufor = plik_surowy.read_text(encoding="utf-8")
            self.log("   używam wcześniej pobranych tekstów")
        else:
            try:
                bufor = self.pobierz_witryne(kontekst, url)
            except (Zatrzymano, Pominieto):
                raise
            except Zablokowano as blad:
                self.log(f"   [ZABLOKOWANA] strona nie wpuściła automatu lub jest pusta: \"{blad}\"")
                self.log("   Wskazówka: włącz w Ustawieniach 'Używaj mojego Chrome' i przejdź weryfikację ręcznie.")
                return
            except Exception as blad:
                self.log(f"   [BŁĄD POBIERANIA] {type(blad).__name__}: {str(blad)[:200]}")
                return
            plik_surowy.write_text(bufor, encoding="utf-8")
            self.log(f"   zapisano surowe/{nazwa}.txt")

        if not bufor.strip():
            self.log("   brak treści - pomijam")
            return

        self.sprawdz_przerwanie()
        self.log("   analiza w Claude...")
        self.zywo(f"Analiza: {domena}", "")
        try:
            profil = zapytaj_claude(self.system_analizy(domena),
                                    f"Teksty z witryny {domena}:\n{bufor}",
                                    self.u["wysilek"], 16000,
                                    na_zywo=lambda t: self.zywo(None, t),
                                    przerwij=self.sprawdz_przerwanie)
        except (Zatrzymano, Pominieto):
            raise
        except Exception as blad:
            self.log(f"   [BŁĄD API] {blad} - teksty zostały w surowe/{nazwa}.txt")
            return
        plik_profilu.write_text(f"# {domena}\n\nŹródło: {url}\n\n{profil}\n", encoding="utf-8")
        self.log(f"   zapisano profile/{nazwa}.md")

    def raport_koncowy(self):
        katalog_profili = self.katalog / "profile"
        profile = sorted(katalog_profili.glob("*.md")) if katalog_profili.exists() else []
        if not profile:
            raise RuntimeError("brak profili do raportu - najpierw przeanalizuj strony")
        if not self.u["prompt_raport"].strip():
            self.log("\nPrompt raportu końcowego jest pusty - pomijam raport.")
            return

        wszystkie = "\n\n---\n\n".join(p.read_text(encoding="utf-8") for p in profile)
        (self.katalog / "profile_wszystkie.md").write_text(wszystkie, encoding="utf-8")

        self.sprawdz_stop()
        self.log(f"\n[RAPORT] Piszę raport końcowy z {len(profile)} profili (to może potrwać kilka minut)...")
        self.zywo("Raport końcowy (model najpierw analizuje, tekst pojawi się po chwili)", "")
        system = (
            "Jesteś analitykiem przygotowującym raport końcowy z badania stron internetowych.\n"
            f"CEL BADANIA (czego szukamy): {self.u['cel']}\n\n"
            "Opieraj się wyłącznie na dostarczonych profilach.\n\n"
            f"WYTYCZNE RAPORTU:\n{self.u['prompt_raport']}"
        )
        raport = zapytaj_claude(system, f"Profile witryn:\n\n{wszystkie}",
                                self.u["wysilek"], 32000, myslenie=True,
                                na_zywo=lambda t: self.zywo(None, t),
                                przerwij=self.sprawdz_przerwanie)
        naglowek = f"# Raport końcowy: {self.u['nazwa']}\n\nData: {datetime.now():%Y-%m-%d %H:%M}, profili: {len(profile)}\n\n"
        (self.katalog / "raport_koncowy.md").write_text(naglowek + raport, encoding="utf-8")
        self.log("[ZAPISANO] raport_koncowy.md")


# ---------------------------------------------------------
# INTERFEJS
# ---------------------------------------------------------
ZYWO = "__zywo__"                 # element podglądu "na żywo" w drzewie plików
GRUPY_PODGLADU = {"raport": ("Raport końcowy", "raport_koncowy.md"),
                  "profile": ("Profile", "profile/*.md"),
                  "surowe": ("Surowe teksty", "surowe/*.txt")}


class Aplikacja(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("ScrApp.MB")
        self.geometry("1150x800")
        self.minsize(900, 600)
        self.kolejka = queue.Queue()
        self.stop = threading.Event()
        self.pomin = threading.Event()
        self.teraz = threading.Event()
        self.watek = None
        self.katalog = None
        self.plik_logu = None
        # podgląd
        self.zywo_tytul = "brak aktywnego kroku"
        self.zywo_tekst = ""
        self.pokazany = None          # ZYWO albo ścieżka pokazanego pliku
        self.pokazany_mtime = None
        self.podpis_plikow = None

        self.zbuduj()
        self.wypelnij(self.wczytaj_json(PLIK_OSTATNICH) or DOMYSLNE)
        self.protocol("WM_DELETE_WINDOW", self.zamknij)
        self.after(200, self.odbierz)
        self.after(500, self.odswiez_podglad)

    # --- budowa okna ---
    def pole_tekstowe(self, rodzic, wysokosc):
        pole = ScrolledText(rodzic, height=wysokosc, wrap="word", font=("Consolas", 10), undo=True)
        pole.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        return pole

    def zbuduj(self):
        gora = ttk.Frame(self, padding=8)
        gora.pack(fill="x")
        ttk.Label(gora, text="Nazwa wyszukania:").pack(side="left")
        self.nazwa = tk.StringVar()
        ttk.Entry(gora, textvariable=self.nazwa, width=40).pack(side="left", padx=6)
        ttk.Button(gora, text="Nowe", command=self.nowe).pack(side="left", padx=2)
        ttk.Button(gora, text="Wczytaj...", command=self.wczytaj).pack(side="left", padx=2)
        ttk.Button(gora, text="Otwórz katalog", command=self.otworz_katalog).pack(side="left", padx=2)

        self.zakladki = zakladki = ttk.Notebook(self)
        zakladki.pack(fill="both", expand=True, padx=8)

        z1 = ttk.Frame(zakladki)
        zakladki.add(z1, text="1. Czego szukamy i gdzie")
        ttk.Label(z1, text="Cel: czego szukamy na stronach").pack(anchor="w", padx=8, pady=(8, 2))
        self.cel = self.pole_tekstowe(z1, 4)
        ttk.Label(z1, text="Lista stron (jeden adres w linii, # = komentarz)").pack(anchor="w", padx=8, pady=(4, 2))
        self.strony = self.pole_tekstowe(z1, 12)

        z2 = ttk.Frame(zakladki)
        zakladki.add(z2, text="2. Prompt: cechy do znalezienia")
        ttk.Label(z2, text="Jakie cechy i w jakiej formie opisać dla każdej strony").pack(anchor="w", padx=8, pady=(8, 2))
        self.prompt_cechy = self.pole_tekstowe(z2, 20)

        z3 = ttk.Frame(zakladki)
        zakladki.add(z3, text="3. Prompt: raport końcowy")
        ttk.Label(z3, text="Jak napisać raport końcowy ze wszystkich profili").pack(anchor="w", padx=8, pady=(8, 2))
        self.prompt_raport = self.pole_tekstowe(z3, 20)

        z4 = ttk.Frame(zakladki, padding=12)
        zakladki.add(z4, text="Ustawienia")
        self.wysilek = tk.StringVar()
        self.podstrony = tk.IntVar()
        self.znakow = tk.IntVar()
        self.slowa_linkow = tk.StringVar()
        self.chrome = tk.BooleanVar()
        self.pomijaj_gotowe = tk.BooleanVar()
        wiersze = [
            ("Wysiłek modelu", ttk.Combobox(z4, textvariable=self.wysilek, values=["low", "medium", "high"],
                                            state="readonly", width=10)),
            ("Podstron na witrynę (0 = tylko podany adres)", ttk.Spinbox(z4, from_=0, to=30, textvariable=self.podstrony, width=8)),
            ("Maks. znaków z jednej podstrony", ttk.Spinbox(z4, from_=1000, to=50000, increment=1000,
                                                            textvariable=self.znakow, width=8)),
            ("Słowa w linkach do wyboru podstron", ttk.Entry(z4, textvariable=self.slowa_linkow, width=70)),
        ]
        for nr, (opis, pole) in enumerate(wiersze):
            ttk.Label(z4, text=opis).grid(row=nr, column=0, sticky="w", pady=4)
            pole.grid(row=nr, column=1, sticky="w", padx=8, pady=4)
        ttk.Checkbutton(z4, text="Używaj mojego Chrome z portu 9222 (strony wymagające zalogowania)",
                        variable=self.chrome).grid(row=4, column=0, columnspan=2, sticky="w", pady=4)
        ttk.Checkbutton(z4, text="Wznawiaj: pomijaj strony, które mają już profil w tym wyszukaniu",
                        variable=self.pomijaj_gotowe).grid(row=5, column=0, columnspan=2, sticky="w", pady=4)
        ttk.Label(z4, text=f"Model: {MODEL}").grid(row=6, column=0, sticky="w", pady=(16, 4))
        ttk.Button(z4, text="Przywróć domyślne prompty",
                   command=self.domyslne_prompty).grid(row=7, column=0, sticky="w", pady=4)

        self.z_podglad = self.zbuduj_podglad(zakladki)
        zakladki.add(self.z_podglad, text="Podgląd")

        dol = ttk.Frame(self, padding=8)
        dol.pack(fill="x")
        self.przycisk_start = ttk.Button(dol, text="▶ Start: analiza stron + raport", command=lambda: self.start(False))
        self.przycisk_start.pack(side="left")
        self.przycisk_raport = ttk.Button(dol, text="Tylko raport końcowy", command=lambda: self.start(True))
        self.przycisk_raport.pack(side="left", padx=6)
        self.przycisk_pomin = ttk.Button(dol, text="⏭ Zrezygnuj z bieżącego kroku", command=self.zrezygnuj, state="disabled")
        self.przycisk_pomin.pack(side="left", padx=(12, 0))
        self.przycisk_stop = ttk.Button(dol, text="Zatrzymaj po tej stronie", command=self.zatrzymaj, state="disabled")
        self.przycisk_stop.pack(side="left", padx=6)
        self.przycisk_teraz = ttk.Button(dol, text="■ Zakończ teraz", command=self.zakoncz_teraz, state="disabled")
        self.przycisk_teraz.pack(side="left")
        self.pasek = ttk.Progressbar(dol, mode="determinate")
        self.pasek.pack(side="left", fill="x", expand=True, padx=10)
        self.status = ttk.Label(dol, text="")
        self.status.pack(side="left")

        self.logi = ScrolledText(self, height=10, wrap="word", font=("Consolas", 9), state="disabled")
        self.logi.pack(fill="both", padx=8, pady=(0, 8))

    def zbuduj_podglad(self, rodzic):
        ramka = ttk.Frame(rodzic)
        panele = ttk.PanedWindow(ramka, orient="horizontal")
        panele.pack(fill="both", expand=True, padx=8, pady=8)

        lewa = ttk.Frame(panele)
        self.sledz = tk.BooleanVar(value=True)
        ttk.Checkbutton(lewa, text="Śledź bieżący krok na żywo", variable=self.sledz,
                        command=self.przelaczono_sledzenie).pack(anchor="w", pady=(0, 4))
        self.drzewo = ttk.Treeview(lewa, show="tree", selectmode="browse")
        przewijak = ttk.Scrollbar(lewa, orient="vertical", command=self.drzewo.yview)
        self.drzewo.configure(yscrollcommand=przewijak.set)
        przewijak.pack(side="right", fill="y")
        self.drzewo.pack(fill="both", expand=True)
        self.drzewo.bind("<<TreeviewSelect>>", self.wybrano_w_drzewie)
        panele.add(lewa, weight=1)

        prawa = ttk.Frame(panele)
        self.podglad_tytul = ttk.Label(prawa, text="", font=("Segoe UI", 10, "bold"))
        self.podglad_tytul.pack(anchor="w", pady=(0, 4))
        self.podglad = ScrolledText(prawa, wrap="word", font=("Consolas", 10), state="disabled")
        self.podglad.pack(fill="both", expand=True)
        panele.add(prawa, weight=3)
        return ramka

    # --- dane formularza ---
    def zbierz(self) -> dict:
        return {
            "nazwa": self.nazwa.get().strip(),
            "cel": self.cel.get("1.0", "end").strip(),
            "strony": self.strony.get("1.0", "end").strip(),
            "prompt_cechy": self.prompt_cechy.get("1.0", "end").strip(),
            "prompt_raport": self.prompt_raport.get("1.0", "end").strip(),
            "wysilek": self.wysilek.get(),
            "podstrony": self.podstrony.get(),
            "znakow": self.znakow.get(),
            "slowa_linkow": self.slowa_linkow.get(),
            "chrome": self.chrome.get(),
            "pomijaj_gotowe": self.pomijaj_gotowe.get(),
        }

    def wypelnij(self, dane: dict):
        dane = {**DOMYSLNE, **dane}
        self.nazwa.set(dane["nazwa"])
        for pole, klucz in ((self.cel, "cel"), (self.strony, "strony"),
                            (self.prompt_cechy, "prompt_cechy"), (self.prompt_raport, "prompt_raport")):
            pole.delete("1.0", "end")
            pole.insert("1.0", dane[klucz])
        self.wysilek.set(dane["wysilek"])
        self.podstrony.set(dane["podstrony"])
        self.znakow.set(dane["znakow"])
        self.slowa_linkow.set(dane["slowa_linkow"])
        self.chrome.set(dane["chrome"])
        self.pomijaj_gotowe.set(dane["pomijaj_gotowe"])

    @staticmethod
    def wczytaj_json(plik: Path):
        try:
            return json.loads(plik.read_text(encoding="utf-8"))
        except Exception:
            return None

    def katalog_wyszukania(self) -> Path | None:
        nazwa = bezpieczna_nazwa(self.nazwa.get())
        return KATALOG_WYSZUKAN / nazwa if nazwa else None

    # --- przyciski ---
    def nowe(self):
        dane = self.zbierz()
        dane.update(nazwa="", strony="")
        self.wypelnij(dane)  # cel i prompty zostają jako punkt wyjścia

    def domyslne_prompty(self):
        dane = self.zbierz()
        dane.update(prompt_cechy=DOMYSLNE["prompt_cechy"], prompt_raport=DOMYSLNE["prompt_raport"])
        self.wypelnij(dane)

    def wczytaj(self):
        KATALOG_WYSZUKAN.mkdir(exist_ok=True)
        katalog = filedialog.askdirectory(initialdir=KATALOG_WYSZUKAN, title="Wybierz katalog wyszukania")
        if not katalog:
            return
        dane = self.wczytaj_json(Path(katalog) / "ustawienia.json")
        if not dane:
            messagebox.showwarning("ScrApp.MB", "W tym katalogu nie ma pliku ustawienia.json.")
            return
        self.wypelnij(dane)
        self.dopisz_log(f"Wczytano wyszukanie: {katalog}")

    def otworz_katalog(self):
        katalog = self.katalog_wyszukania()
        if not katalog or not katalog.exists():
            katalog = KATALOG_WYSZUKAN
            katalog.mkdir(exist_ok=True)
        os.startfile(katalog)

    def start(self, tylko_raport: bool):
        dane = self.zbierz()
        if not dane["nazwa"]:
            messagebox.showwarning("ScrApp.MB", "Wpisz nazwę wyszukania - od niej zależy nazwa katalogu.")
            return
        if not tylko_raport and not lista_adresow(dane["strony"]):
            messagebox.showwarning("ScrApp.MB", "Wklej co najmniej jeden adres strony.")
            return
        if not dane["prompt_cechy"]:
            messagebox.showwarning("ScrApp.MB", "Prompt cech nie może być pusty.")
            return

        self.katalog = self.katalog_wyszukania()
        self.katalog.mkdir(parents=True, exist_ok=True)
        ustawienia = self.wczytaj_json(self.katalog / "ustawienia.json") or {}
        dane["utworzono"] = ustawienia.get("utworzono", f"{datetime.now():%Y-%m-%d %H:%M}")
        tekst = json.dumps(dane, ensure_ascii=False, indent=2)
        (self.katalog / "ustawienia.json").write_text(tekst, encoding="utf-8")
        PLIK_OSTATNICH.write_text(tekst, encoding="utf-8")
        self.plik_logu = self.katalog / "log.txt"

        for flaga in (self.stop, self.pomin, self.teraz):
            flaga.clear()
        self.ustaw_przyciski(True)
        self.zakladki.select(self.z_podglad)
        self.sledz.set(True)
        self.podpis_plikow = None  # drzewo przebuduje się dla katalogu tego wyszukania
        self.pokaz_zywo()
        self.dopisz_log(f"\n===== {datetime.now():%Y-%m-%d %H:%M} | {dane['nazwa']} =====")
        zadanie = Zadanie(dane, self.katalog,
                          log=lambda t: self.kolejka.put(("log", t)),
                          postep=lambda a, b: self.kolejka.put(("postep", (a, b))),
                          zywo=lambda tytul, t: self.kolejka.put(("zywo", (tytul, t))),
                          stop=self.stop, pomin=self.pomin, teraz=self.teraz)
        self.watek = threading.Thread(target=self.praca, args=(zadanie, tylko_raport), daemon=True)
        self.watek.start()

    def praca(self, zadanie: Zadanie, tylko_raport: bool):
        zadanie.uruchom(tylko_raport)
        self.kolejka.put(("koniec", None))

    def zrezygnuj(self):
        self.pomin.set()
        self.dopisz_log("Rezygnuję z bieżącego kroku...")

    def zatrzymaj(self):
        self.stop.set()
        self.dopisz_log("Zatrzymam pracę po bieżącej stronie...")

    def zakoncz_teraz(self):
        self.teraz.set()
        self.dopisz_log("Kończę natychmiast (wczytywana właśnie strona może potrzebować do ~20 s)...")

    def zamknij(self):
        if self.watek and self.watek.is_alive():
            if not messagebox.askyesno("ScrApp.MB", "Praca trwa. Przerwać i zamknąć?"):
                return
            self.teraz.set()
        try:
            PLIK_OSTATNICH.write_text(json.dumps(self.zbierz(), ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pass
        self.destroy()

    # --- podgląd plików i bieżącego kroku ---
    def ustaw_podglad(self, tytul: str, tekst: str, pozycja: float = 0.0):
        self.podglad_tytul.config(text=tytul)
        self.podglad.config(state="normal")
        self.podglad.delete("1.0", "end")
        self.podglad.insert("1.0", tekst)
        self.podglad.config(state="disabled")
        self.podglad.yview_moveto(pozycja)

    def pokaz_zywo(self):
        self.pokazany = ZYWO
        self.ustaw_podglad(f"Na żywo: {self.zywo_tytul}", self.zywo_tekst)
        self.podglad.see("end")
        if self.drzewo.exists(ZYWO):
            self.drzewo.selection_set(ZYWO)

    def pokaz_plik(self, plik: Path):
        pozycja = self.podglad.yview()[0] if self.pokazany == str(plik) else 0.0
        try:
            self.pokazany_mtime = plik.stat().st_mtime
            tekst = plik.read_text(encoding="utf-8")
        except Exception as blad:
            tekst = f"(nie można odczytać pliku: {blad})"
        self.pokazany = str(plik)
        self.ustaw_podglad(f"{plik.parent.name}/{plik.name}", tekst, pozycja)

    def wybrano_w_drzewie(self, _zdarzenie=None):
        zaznaczone = self.drzewo.selection()
        if not zaznaczone or zaznaczone[0] == self.pokazany or zaznaczone[0] in GRUPY_PODGLADU:
            return
        if zaznaczone[0] == ZYWO:
            self.sledz.set(True)
            self.pokaz_zywo()
        else:
            self.sledz.set(False)  # oglądamy plik - nie przeskakujemy na bieżący krok
            self.pokaz_plik(Path(zaznaczone[0]))

    def przelaczono_sledzenie(self):
        if self.sledz.get():
            self.pokaz_zywo()

    def odswiez_podglad(self):
        try:
            katalog = self.katalog if self.watek and self.watek.is_alive() else self.katalog_wyszukania()
            pliki = []
            if katalog and katalog.exists():
                for grupa, (_, wzor) in GRUPY_PODGLADU.items():
                    pliki += [(grupa, p) for p in sorted(katalog.glob(wzor))]
            podpis = tuple((g, str(p), p.stat().st_mtime) for g, p in pliki)
            if podpis != self.podpis_plikow:
                self.podpis_plikow = podpis
                self.zbuduj_drzewo(pliki)
            if self.pokazany and self.pokazany != ZYWO:
                plik = Path(self.pokazany)
                if plik.exists() and plik.stat().st_mtime != self.pokazany_mtime:
                    self.pokaz_plik(plik)  # plik zmienił się na dysku
        except Exception:
            pass  # plik w trakcie zapisu - spróbujemy za chwilę
        self.after(1500, self.odswiez_podglad)

    def zbuduj_drzewo(self, pliki):
        pierwsze = not self.drzewo.get_children()
        otwarte = {g for g in GRUPY_PODGLADU if self.drzewo.exists(g) and self.drzewo.item(g, "open")}
        self.drzewo.delete(*self.drzewo.get_children())
        self.drzewo.insert("", "end", iid=ZYWO, text=f"● Na żywo: {self.zywo_tytul}")
        for grupa, (opis, _) in GRUPY_PODGLADU.items():
            elementy = [p for g, p in pliki if g == grupa]
            self.drzewo.insert("", "end", iid=grupa, text=f"{opis} ({len(elementy)})",
                               open=pierwsze or grupa in otwarte)
            for plik in elementy:
                self.drzewo.insert(grupa, "end", iid=str(plik), text=plik.stem)
        if self.pokazany and self.drzewo.exists(self.pokazany):
            self.drzewo.selection_set(self.pokazany)

    def odebrano_zywo(self, tytul, tekst: str):
        if tytul is not None:
            self.zywo_tytul, self.zywo_tekst = tytul, ""
            if self.drzewo.exists(ZYWO):
                self.drzewo.item(ZYWO, text=f"● Na żywo: {tytul}")
            if self.sledz.get() or self.pokazany == ZYWO:
                self.pokaz_zywo()
        if tekst:
            self.zywo_tekst += tekst
            if self.pokazany == ZYWO:
                self.podglad.config(state="normal")
                self.podglad.insert("end", tekst)
                self.podglad.config(state="disabled")
                self.podglad.see("end")

    # --- komunikacja z wątkiem pracy ---
    def ustaw_przyciski(self, praca: bool):
        for przycisk in (self.przycisk_start, self.przycisk_raport):
            przycisk.config(state="disabled" if praca else "normal")
        for przycisk in (self.przycisk_pomin, self.przycisk_stop, self.przycisk_teraz):
            przycisk.config(state="normal" if praca else "disabled")

    def dopisz_log(self, tekst: str):
        self.logi.config(state="normal")
        self.logi.insert("end", tekst + "\n")
        self.logi.see("end")
        self.logi.config(state="disabled")
        if self.plik_logu:
            with open(self.plik_logu, "a", encoding="utf-8") as f:
                f.write(tekst + "\n")

    def odbierz(self):
        try:
            while True:
                rodzaj, wartosc = self.kolejka.get_nowait()
                if rodzaj == "log":
                    self.dopisz_log(wartosc)
                elif rodzaj == "zywo":
                    self.odebrano_zywo(*wartosc)
                elif rodzaj == "postep":
                    zrobione, wszystkie = wartosc
                    self.pasek.config(maximum=max(wszystkie, 1), value=zrobione)
                    self.status.config(text=f"{zrobione}/{wszystkie}")
                elif rodzaj == "koniec":
                    self.ustaw_przyciski(False)
        except queue.Empty:
            pass
        self.after(200, self.odbierz)


if __name__ == "__main__":
    Aplikacja().mainloop()
