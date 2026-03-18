# NGVO - Polish Localization Builder

Ten workflow sklada wynikowy mod MO2 z rozpakowanych zrodel w `./tmp/`.

## Zalozenie

- runtime NGVO zostaje angielski
- wynikowy mod to overlay ladowany na koncu lewej listy MO2
- builder nie pobiera nic z Nexusa
- builder tylko scala juz rozpakowane pliki do jednej struktury moda

## Struktura zrodel

Builder czyta konfiguracje z `ngvo-polish-build.json`.

Domyslnie oczekuje takich katalogow:

```text
tmp/
   NGVO - Polish Localization/
   sources/
      mods/
         skyui-polish-translation/
         racemenu-polish-translation/
         morehud-se-polish-translation/
         ...
      overrides/
         010-manual-fixes/
         020-font-patch/
```

## Jak rozkladac pliki

1. Ustaw w `ngvo-polish-build.json` sciezke `paths.sourceGameRoot` na polska instalacje Skyrim.
2. Ustaw w `ngvo-polish-build.json` sciezke `paths.buildsRoot` na katalog buildow, na przyklad `./tmp/`.
3. Kazde spolszczenie z `ngvo-plus-pl-mods.json` rozpakuj do katalogu:
   `tmp/sources/mods/<entry.id>/`
4. Rzeczy reczne i lokalne poprawki wrzucaj do:
   `tmp/sources/overrides/<nazwa>/`

Builder sam skopiuje pliki bootstrapu na podstawie `ngvo-polish-copy-manifest.json` i `paths.sourceGameRoot` bezposrednio do `NGVO - Polish Localization`.
Jesli rozpakowane archiwum moda ma katalog `Data`, builder automatycznie skopiuje jego zawartosc do korzenia wynikowego moda.

## Budowanie

Uruchom:

```powershell
python tools/build_ngvo_polish_localization.py --clean
```

Jesli skladasz paczke etapami i nie masz jeszcze wszystkich wymaganych spolszczen, builder i tak zlozy bootstrap z manifestu i dopisze braki do raportu:

```powershell
python tools/build_ngvo_polish_localization.py --clean --allow-missing-required
```

Wynik trafi do:

```text
tmp/NGVO - Polish Localization/
```

Raport trafi do:

```text
tmp/build-report.json
```

## Co builder robi

1. Czyta wpisy z `ngvo-plus-pl-mods.json`
2. Kopiuje pliki z `ngvo-polish-copy-manifest.json` bezposrednio do `tmp/NGVO - Polish Localization/`.
3. Szuka rozpakowanych zrodel modow w `tmp/sources/mods/<entry.id>/`
4. Kopiuje pliki do jednego wyniku w kolejnosci:
   - `bootstrap` z manifestu kopiowania
   - mody z manifestu wedlug pola `order`
   - katalogi z `overrides/` w porzadku alfabetycznym
5. Pozniejsze zrodla nadpisuja wczesniejsze
6. Zapisuje raport z nadpisaniami i brakujacymi zrodlami

## Co sprawdzac recznie

1. Czy spolszczenie jest zgodne z wersja moda w NGVO
2. Czy archive nie zawiera smieci typu screenshoty albo osobne readme do instalatora
3. Czy pliki maja warianty `_english`, jesli maja nadpisywac angielski runtime
4. Czy przetlumaczone pluginy nie sa starsze od pluginow bazowych w NGVO

## Uwaga praktyczna

Ten builder nie rozstrzyga konfliktow semantycznych. Jesli dwa zrodla dostarczaja ten sam plik, wygrywa zrodlo kopiowane pozniej. Szczegoly sa w `tmp/build-report.json`.

Jesli wynikowy mod jest pusty, to znaczy, ze builder nie znalazl nic do skopiowania ani w manifeście kopiowania, ani w `tmp/sources/`.