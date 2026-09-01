# Shelfmark Fork — Maintenance Playbook

**Repo:** github.com/Fabian-Ariu/shelfmark
**Upstream:** github.com/calibrain/shelfmark
**Production deploy:** `ghcr.io/fabian-ariu/shelfmark:v1.3.0-fork-0.2.3` on Mac Mini cwa-downloader (port 8085 via gluetun)
**Production branch:** `feature/variante-2-prime-audiobook-discovery` (enthaelt `feature/content-type-driven-search-mode`)
**Block-B Cut-Over:** 2026-08-30 — Audiobook-Discovery via `combined_audiobook` (Audible-Direct DE+US + Hardcover) live

---

## Architektur-Regeln

### Audiobook-Toggle überschreibt SEARCH_MODE bewusst

`SEARCH_MODE=direct` (Production-Setting) routet ebook-Suchen via
`/api/releases?source=direct_download` (Anna's Archive Volltext). Das ist eine
eBook-Optimierung — AA hat keinen verlässlichen serverseitigen Audio-Format-
Filter (Block-A-Empirie, 2026-05-16).

Audiobooks brauchen daher ZWINGEND die Metadata-Provider-Schicht:
`/api/metadata/search?content_type=audiobook` routet zu `combined_audiobook`,
das parallel Audible-Direct (DE+US) und Hardcover (2-Step, reading_format_id=2,
`ger`/`eng`-Sprachcodes) queryt und ASIN-/Werks-Hash-deduplet.

Frontend setzt den effektiven Mode an **zwei** Stellen:

1. **SearchBar-Toggle-Klick:** `SearchBar.tsx → handleContentTypeSelect()`
   ruft `contentTypeToSearchMode(type, false)` und propagiert via
   `onSearchModeChange`.
2. **Search-Dispatch defensive:** `useSearch.ts → handleSearch()` ruft
   `resolveEffectiveSearchMode(requestedMode, contentType)` direkt vor
   `searchMode`-Bestimmung. Greift auch wenn Persistenz/Race/Deep-Link den
   ersten Pfad umgehen würde.

Wenn jemand eine "Direct-Mode für Audiobooks"-Variante baut, muss der
defensive Override in `useSearch.ts` zuerst entfernt werden — und es braucht
einen sinnvollen Audio-Discovery-Pfad ohne Metadata-Provider-Layer (Block-A
zeigte: ABB-only ohne Audible/Hardcover-Metadata findet die DE-Pop-Hits wie
Fitzek/Perry-Rhodan nicht zuverlässig).

Tests sichern beide Stellen:
- `tests/contentTypeToSearchMode.test.ts` (Toggle-Klick-Pfad)
- `tests/resolveEffectiveSearchMode.test.ts` (Dispatch-Override-Pfad)

### Search-Mode: roh persistiert vs. content-type-aware abgeleitet (0.2.5, 2026-08-31)

`App.tsx` hält **zwei** Werte:

- `persistedSearchMode` — die rohe Nutzer-/Config-Präferenz
  (`userSearchMode ?? config.search_mode ?? 'direct'`). **Nur dieser Wert** wird
  im Search-Mode-Dropdown angezeigt (`AdvancedFilters`/`SearchSection`) und über
  `handleSearchModeChange` nach LocalStorage + `SEARCH_MODE` zurückgeschrieben.
  Auch `combinedModeAllowed` hängt daran (Combined erzwingt `contentType='ebook'`,
  ein abgeleiteter Wert würde dort flackern).
- `effectiveSearchMode = resolveEffectiveSearchMode(persistedSearchMode,
  effectiveContentType)` — was die App *tut*. Speist `SearchModeProvider`,
  Suchaufbau, Sort, Load-More, Provider-Auflösung.

Damit folgt die UI derselben Regel wie der Dispatch in `useSearch.ts`. Es gibt
**keinen** Rückschreibpfad von `effectiveSearchMode` nach `SEARCH_MODE`; ein
Audiobook-Modus kann die persistierte eBook-Präferenz nicht überschreiben.

Per-Book-Absicherung: `utils/shouldUseReleaseFlow.ts`
(`searchMode === 'universal' || isMetadataBook(book)`) entscheidet in
`BookActionButton`, `ResultsSection` und `ListView` über Release- vs.
Direct-Aktion **und** über den Button-State. Diese drei müssen dasselbe Prädikat
benutzen, sonst rendert ein Get-Button mit Direct-Button-State.

Backend-Gegenstück (deckt API-Clients ab, die kein Frontend benutzen):
`DownloadHandler.validate_queue_request(release_data, source_url)` — Default
`None`. `AudiobookBayHandler` lehnt dort Releases ohne Detail-URL ab
(`MISSING_DETAIL_URL_ERROR`), `orchestrator.queue_release` ruft den Hook vor dem
Queuen auf. Bewusst **kein** Titel/Autor-Fallback: ein geratener Release wäre in
der Request-Historie nicht von einer bewussten Auswahl zu unterscheiden.

Tests: `tests/shouldUseReleaseFlow.test.ts`,
`tests/audiobookbay/test_handler.py::TestAudiobookBayHandlerQueueValidation`,
`tests/download/test_orchestrator_user_output_mode.py`
(`test_queue_release_rejects_audiobookbay_release_without_detail_url`).

### Release-Source-Resolution ist content-type-aware (Block-B 2026-05-18, Patch 0.2.3)

`getBrowseSource(book)` returnt `book.source || book.provider`. Im Direct-
Mode-eBook-Flow funktioniert das zufällig, weil `book.provider="direct_download"`
auch ein registrierter Release-Source ist. Mit Variante-2-prime ist diese
Identität aufgelöst: Audiobook-Hits haben `book.provider="audible"` (oder
`hardcover`/`combined_audiobook`) — keine echten Release-Sources. Backend
würde `Unknown release source: audible` werfen.

Hinweis: `getBrowseSource` selbst ist inzwischen toter Code (keine
Production-Call-Site mehr, nur noch Tests). Der lebende Pfad ist der
eBook-Zweig von `utils/getReleaseSourceForContentType.ts`.

**Architektur-Regel:** Für Download-Payloads die `release_data.source` setzen,
ist die korrekte Utility `getReleaseSourceForContentType(book, contentType,
defaultAudiobookSource)` — siehe Abschnitt unten für die vollständige
Auflösungsreihenfolge. Bei `audiobook` greift
`config.default_release_source_audiobook`; ist der leer, wirft die Utility
bewusst, statt still auf `book.provider` zurückzufallen.

Regression-Tests in `tests/requestPayload.test.ts` (Audiobook-Integration-
Block) und `tests/getReleaseSourceForContentType.test.ts`.

### Release-Source-Resolution: eBook-Zweig + Render-Sicherheit (Block-B 2026-08-31, Patch 0.2.4)

Der eBook-Zweig returnte `book.source || book.provider`. Für ein
Metadata-Book (`transformMetadataToBook` setzt **kein** `book.source`,
`book.provider` ist `hardcover`/`audible`/`combined_audiobook`) hätte das
`Unknown release source: hardcover` beim Backend ausgelöst.

**Erreichbarkeit — korrigiert gegenüber der alten Backlog-Notiz:** Der Fall ist
*nicht* auf `SEARCH_MODE=universal` beschränkt. In echtem Universal-Mode ist er
sogar unerreichbar, weil `BookActionButton` bei `searchMode === 'universal'`
einen `BookGetButton` (Release-Pfad) statt eines Download-Buttons rendert.
Erreichbar ist er in **Direct-Mode-Installs** über den Header-Content-Type-
Toggle:

1. eBook-Suche in Direct-Mode → Ergebnisse stehen, `isInitialState=false`.
2. Header-Toggle auf *Audiobook*: `useSearch` erzwingt intern `universal`
   (`resolveEffectiveSearchMode`), die Treffer sind Metadata-Books ohne
   `book.source`. **Seit 0.2.5 gilt dieselbe Regel auch für die UI** (siehe
   Abschnitt unten) → `SearchModeContext` = `universal`, die Karte rendert den
   Release-Pfad. Vorher blieb der Context auf `direct` und die Karte bot einen
   Direct-Download an — das war BUG B.
3. Header-Toggle zurück auf *Ebook*: `effectiveContentType='ebook'`,
   `SearchModeContext='direct'`, aber `books` sind noch die Audiobook-
   Metadata-Books. Dieser Restfall wird per-Book von `shouldUseReleaseFlow`
   abgefangen (`isMetadataBook`), nicht mehr nur vom Modus.

Die `SearchBar` ruft dabei zwar `onSearchModeChange` (das in `SearchSection`
`resetSearchResultsState` auslöst), aber die Header-Instanz bekommt diesen Prop
absichtlich nicht: er würde bei jedem Wechsel auf "Ebook"
`contentTypeToSearchMode('ebook', false) === 'direct'` persistieren und damit
Universal-Mode-Installs auf Direct umstellen. Stattdessen kappt
`handleContentTypeChange` in App.tsx die Ergebnisse selbst
(`resetSearchResultsState` bei echtem Content-Type-Wechsel).

**Architektur-Regel 1 (Auflösung):** `getReleaseSourceForContentType` löst für
eBook in dieser Reihenfolge auf und gibt **niemals** einen Metadata-Provider-
Namen zurück:

1. `book.source` — jedes source-backed Suchergebnis nennt seine Release-Source
   selbst (`transformSourceBackedDataToBook` setzt `source` **und** `provider`).
2. `book.provider`, **falls** der Name (getrimmt/lowercased) in der Positiv-
   Liste registrierter Release-Sources steht (`audiobookbay`, `direct_download`,
   `irc`, `newznab`, `prowlarr`). Das ist der Legacy-Direct-Mode-Pfad —
   **production-kritisch, unverändert**. Zurückgegeben wird der normalisierte
   Name, also genau der, der validiert wurde.
3. sonst: Error (`"<provider>" is a metadata provider, not a release source …`
   bzw. `missing source context`).

**Es gibt bewusst KEINEN `DEFAULT_RELEASE_SOURCE`-Fallback für Metadata-Books.**
Die Direct-Payload-Builder setzen `source_id: book.id`, und `book.id` ist bei
einem Metadata-Book `${provider}:${provider_id}` (z.B. `hardcover:12345`). Mit
einer substituierten Source entstünde ein wohlgeformter, aber unerfüllbarer
Task: `direct_download` interpretiert `task.task_id` als Anna's-Archive-MD5
(`shelfmark/release_sources/direct_download.py`). Der Task würde akzeptiert,
gequeued, ggf. vom Admin approved — und erst dann scheitern. Ein sofortiger
Fehler ist strikt besser. Der Download-Pfad für Metadata-Books ist der
Release-Pfad (`/api/releases` + ReleaseModal →
`buildReleaseDataFromMetadataRelease`), der eine konkrete Release-ID auflöst.
Der Patch leistet also ausdrücklich *"kein unauflösbarer Source-Name und kein
unerfüllbarer Task mehr"* — **nicht** "Direct-Download für Metadata-Books".

**Architektur-Regel 2 (Render-Pfad wirft nie):** `getDirectPolicyMode` läuft
über `getDirectActionButtonState` **während des Renderings**, und es gibt in
`src/frontend/src` **keine ErrorBoundary** — ein Throw entlädt den kompletten
React-Baum (weiße Seite, nur Reload hilft). Deshalb zwei Einstiegspunkte:

- `resolveReleaseSourceOrNull(book, contentType, defaultAudiobookSource)` →
  `null` statt Throw. **Nur diese** Variante gehört in Render-/Policy-Pfade;
  `getDirectPolicyMode` fällt bei `null` auf `getDefaultMode(contentType)`
  zurück.
- `getReleaseSourceForContentType(...)` → wirft. Nur in Payload-/Download-
  Pfaden, und dort in `try/catch` mit `showToast` (`handleDownload`,
  `executeBookDownload`) — `BookDownloadButton` schluckt Fehler aus `onDownload`
  stumm, ohne Toast wäre der Button einfach tot.

**Positiv-Liste statt Deny-Liste:** `KNOWN_RELEASE_SOURCES` in
`utils/getReleaseSourceForContentType.ts` listet die *Release-Sources*, nicht
die Metadata-Provider. Eine Deny-Liste der Metadata-Provider würde still
brechen, sobald upstream einen Provider ergänzt (unbekannter Name → als
Release-Source behandelt → derselbe Backend-Fehler). Die Positiv-Liste
degradiert im umgekehrten Fall sicher: ein unbekannter Name führt zu Error bzw.
`null`, wir schicken dem Backend nie einen unauflösbaren Namen. Die Live-Liste
aus `/api/release-sources` wäre robuster, wird aber asynchron geladen — im
Ladefenster wäre die Liste leer und jeder Direct-Mode-Treffer sähe aus wie ein
Metadata-Book. **Bei einer neuen Release-Source upstream:
`KNOWN_RELEASE_SOURCES` mit `_BUILTIN_SOURCE_MODULES` in
`shelfmark/release_sources/__init__.py` abgleichen.**

Zur Validierung von `DEFAULT_RELEASE_SOURCE`: nur der User-Override-Pfad
(`validate_search_preference_value` in `shelfmark/config/users_settings.py`,
aufgerufen aus `_on_save_users` und `admin_settings_routes.py`) prüft den Wert
gegen die Registry. Der globale `search_mode`-Tab hat **keinen** `on_save`-Hook
(`register_on_save` existiert nur für security/notifications/downloads/mirrors/
advanced/users), und Onboarding schreibt direkt via `save_config_file`. Ein
global gesetzter Wert ist also **nicht** garantiert eine gültige Release-Source
— ein weiterer Grund, ihn nicht als Fallback zu missbrauchen.

Call-Sites (App.tsx hat `config` überall im Scope):
- `getDirectPolicyMode(book)` — Render-Pfad, nutzt `resolveReleaseSourceOrNull`
- `executeBookDownload(book)` — `getReleaseSourceForContentType` +
  `buildReleaseDataFromDirectBook` + `buildDirectRequestPayload`, in `try/catch`
- `handleDownload(book)` — `getReleaseSourceForContentType` +
  `buildDirectRequestPayload`, in `try/catch`
- `buildDirectRequestPayload` löst die Source **dreimal** auf (book_data,
  release_data, context). Alle drei müssen identische Argumente bekommen, sonst
  lehnt das Backend mit `policy_source_mismatch` ab.

Regression-Tests: `tests/getReleaseSourceForContentType.test.ts` (describes
`ebook content_type (universal-mode / metadata provider)` und
`resolveReleaseSourceOrNull (render path)`) sowie `tests/requestPayload.test.ts`
(Universal-Mode-eBook-Integrationsblock).

---

## Recurring tasks

### Monthly upstream sync

```bash
cd ~/dev/shelfmark-fork
git fetch upstream
git checkout main
git merge upstream/main
# Resolve conflicts in .github/workflows/build-and-publish-docker-image.yml
# (we maintain a slimmer fork version — see ci(fork) commit)
git push origin main

git checkout feature/variante-2-prime-audiobook-discovery
git rebase main
# If conflicts in SearchBar.tsx / App.tsx / requestPayload.ts / metadata_providers:
# integrate carefully, run BOTH gate sets
cd src/frontend && npm install && npm run lint && npm run typecheck && npm run test:unit && cd ../..
uv run pytest tests/ -q          # Backend-Gate: Audible/Combined-Provider-Tests
git push origin feature/variante-2-prime-audiobook-discovery --force-with-lease
```

Die aeltere Branch `feature/content-type-driven-search-mode` (0.1.x) ist nur noch
Historie/Upstream-PR-Basis — Production laeuft auf der Variante-2-prime-Branch.

### After upstream-sync — release bump

Only needed if upstream changes touch files we patched (SearchBar.tsx, App.tsx, SearchSection.tsx, the two utils):

```bash
# After rebase + green CI gates:
git tag v1.3.0-fork-0.2.X   # bump X
git push origin v1.3.0-fork-0.2.X

# Wait for GHCR build (https://github.com/Fabian-Ariu/shelfmark/actions)
# Then update production compose:
sed -i.bak \
  's|fabian-ariu/shelfmark:v1.3.0-fork-0.2.[0-9]*|fabian-ariu/shelfmark:v1.3.0-fork-0.2.X|' \
  ~/docker-migration/stacks/media-erweiterung/docker-compose.yml
~/scripts/deploy.sh media-erweiterung up -d cwa-downloader
```

Der Bind-Mount `direct_download_patched.py` muss nach jedem Upstream-Sync gegen
die neue Source gediffed werden — sonst revertiert er Upstream-Aenderungen an
`direct_download.py` zur Laufzeit.

**Seit 2026-08-31 sind Fork-Source und Bind-Mount byte-identisch**: die drei
Patch-Familien (import os / Bypass-Leiter / Pagination-Loop) sind in die
Fork-Source uebernommen. Der Diff ist damit die Erwartung:

```bash
diff -u shelfmark/release_sources/direct_download.py \
        ~/docker-migration/stacks/media-erweiterung/direct_download_patched.py
# Erwartung: keine Ausgabe. Nach jeder Aenderung an direct_download.py:
cp shelfmark/release_sources/direct_download.py \
   ~/docker-migration/stacks/media-erweiterung/direct_download_patched.py
```

**Reihenfolge beim Rollout des bedarfsgesteuerten Nachladens (2026-08-31).** Der
Bind-Mount wirkt beim naechsten Container-Restart — also spaetestens beim taeglichen
04:00-Restart —, das Image dagegen erst nach `deploy`. Diese Version von
`direct_download.py` stellt den Browse-Pfad auf **eine Seite pro Request** um. Auf dem
alten Image (`v1.3.0-fork-0.2.4`) kennt `ReleaseSearchPlan` kein `page`-Feld, der
`getattr`-Fallback bleibt auf Seite 1, und `main.py`/Frontend kennen weder `has_more`
noch einen Button: die Browse-Suche liefert dort also **eine** Seite statt bis zu
`AA_MAX_PAGES`, ohne Moeglichkeit umzublaettern. Mit dem Produktionswert
`AA_MAX_PAGES=1` ist das folgenlos (es war ohnehin eine Seite). Wer den Wert vorher
hochsetzt, verlangsamt nur die Release-Suche, ohne dass die Browse-Suche mehr liefert.
Deshalb: **erst Image bauen und deployen, dann an Settings drehen** — und
`AA_MAX_PAGES` fuer dieses Feature gar nicht anfassen, dafuer gibt es
`AA_BROWSE_MAX_PAGES` (siehe unten).

Pagination-relevante Settings. Beide sind in `shelfmark/config/settings.py`
(Tab *Mirrors*, Abschnitt Anna's Archive) als `NumberField` registriert, also in
der Settings-UI sichtbar und per ENV ueberschreibbar wie jedes andere Setting:

| Setting / ENV | Default | Wirkung |
| --- | --- | --- |
| `AA_MAX_PAGES` | 5 | **Nur Release-Suche** (ISBN/Titel+Autor, `search_books()`). Die hat keinen Pager, holt bis zu so viele Seiten am Stueck und zahlt pro Seite einen Solve. Produktion steht bewusst auf `1`. |
| `AA_BROWSE_MAX_PAGES` | 20 | **Nur Browse-Suche** (`?source=direct_download&page=N`). Reine Leitplanke gegen einen Client, der auf Seite 400 springt — geholt wird pro Request genau eine Seite. `1` schaltet den Load-More-Button ab. |
| `AA_SEARCH_BUDGET_SECONDS` | 90 | Zeitbudget der Release-Suche; ist es beim Start einer weiteren Seite ueberschritten, kommen Teilergebnisse zurueck. `0` schaltet es ab. Page 1 ist nie budgetiert (sonst 503 statt Treffer). |

**Warum zwei Keys statt einem:** bis 2026-08-31 klammerte der Browse-Zweig an
`AA_MAX_PAGES`. Mit dem Produktionswert `AA_MAX_PAGES=1` wurde `page` auf 1 geclamped
und `has_more = ... and page < max_pages` war `1 < 1` — der Load-More-Button konnte
nie erscheinen, das Feature war in genau der Konfiguration tot, in der es ausgeliefert
wird. Hochsetzen haette den Button gebracht **und** die Release-Suche wieder auf bis zu
N Solves (gemessen 120-164s) verlangsamt. Die beiden Groessen haben nichts miteinander
zu tun: `AA_MAX_PAGES` steuert Arbeit, die pro Request ungefragt anfaellt,
`AA_BROWSE_MAX_PAGES` nur, wie weit ein User klicken darf. `AA_MAX_PAGES=1` bleibt
damit richtig und der Pager funktioniert trotzdem — die Produktions-ENV muss fuer
dieses Feature **nicht** angefasst werden.

`_int_setting()` in `direct_download.py` liest alle drei ueber `config.get()` und
faellt nur dann auf `os.environ` zurueck, wenn die Registry den Key nicht kennt.
Dieser Fallback ist kein Stilbruch, sondern haelt die Bind-Mount-Datei auf einem
aelteren Image funktionsfaehig: dort kennt die mitgelieferte Registry
`AA_MAX_PAGES` noch nicht und `config.get()` wuerde still den Default liefern.

Bedarfsgesteuertes Nachladen (Browse-Pfad): `GET /api/releases?source=direct_download&...&page=N`
holt genau Seite `N` ueber `search_books_page()` — ein Request, ein Solve. Seit dem
gepoolten Bypass-Helfer (siehe "Interner Bypasser") ist dieser Solve billiger als
frueher, aber er faellt weiterhin an: die Kostenrechnung "eine Seite = ein Solve"
bleibt die richtige Obergrenze fuer die Planung.
Die Antwort traegt zusaetzlich `page` und `has_more` (gleicher Contract wie
`/api/metadata/search`); das Frontend haengt die Treffer an und dedupliziert per
`book.id`. Der Sprach-Fallback (Retry ohne `lang`) laeuft nur auf Seite 1 und setzt
dann `has_more=false`, weil eine Fallback-Seite eine andere Query beantwortet.
`search_books()` mit seiner Mehrseiten-Schleife bleibt fuer die Release-Suche.

Abbruchheuristik: der Loop stoppt, sobald eine Seite deutlich kuerzer ist als die
laengste bisher gesehene Seite — vorher zahlte jede Suche einen ueberfluessigen
Leer-Fetch, also einen zusaetzlichen DDoS-Guard-Solve. Gezaehlt werden **von AA
gelieferte** Ergebniszeilen (`_is_result_row()`, rein strukturell), nicht erfolgreich
geparste Records: `_parse_search_result_row()` liefert regulaer `None` fuer
Werbezeilen und fuer Zeilen ohne Publisher-/Jahr-/Sprach-Span. Die Seitengroesse wird
gemessen, nicht angenommen.

Der Slack ist seit 2026-08-31 proportional (`_page_length_slack()`: 10 % der Referenz,
mindestens 2 Zeilen). Feste 2 Zeilen reichten fuer eine einzelne kaputte `<tr>`, nicht
fuer die drei bis fuenf Werbezeilen, die AA in manche Treffersaetze streut.

`_observed_aa_page_size` — die Grenzen ehrlich: fuer den Einzelseiten-Abruf gibt es
keine call-lokale Referenz, deshalb merkt sich das **Modul** die laengste je gesehene
Seite. Dieser Wert ist prozessweit, monoton und wird nie zurueckgesetzt, gilt also
ueber Queries und User eines Workers hinweg. Die frueher hier stehende Zusicherung
"verschluckt nie Ergebnisse" war falsch: liefert Query A eine 100-Zeilen-Seite und
Query B eine volle Seite mit mehr als 10 % nicht zaehlbaren Zeilen, meldet Seite 1 von
B `has_more=false`. Der Fehler ist nach oben durch AAs Seitengroesse begrenzt (eine
Seite kann sie nicht ueberschreiten), dafuer ist der proportionale Slack bemessen —
null ist er nicht. Ohne jede Messung dient `_AA_MIN_PLAUSIBLE_PAGE_SIZE = 48` als
Referenz (Produktion hat eine Browse-Seite mit 48 Ergebniszeilen geliefert, die
Fixtures modellieren 100). Frueher galt dort `Seite nicht leer => has_more`, was nach
jedem Container-Restart (`gunicorn --workers 1`) die erste Suche mit einem Button auf
eine garantiert leere Seite 2 schickte — 45-60 s Warten fuer nichts.

### Interner Bypasser: gepoolter Helfer + warmer Browser

Hoechstes Regressionsrisiko des Forks. Der Bypass-Pfad ist der **einzige** Weg zu
Anna's Archive; geht er kaputt, funktionieren weder Suche noch Download. Es gibt
keinen zweiten Pfad.

**Aufbau.** Der gunicorn-Worker (`--workers 1`, gevent) startet Chrome nie selbst.
Er startet einen Helfer-Subprozess (`python -m shelfmark.bypass.internal_bypasser`,
`start_new_session=True`, also eigene Prozessgruppe), schickt ihm pro Solve eine
JSON-Zeile auf stdin und pollt auf eine Ergebnisdatei in `/tmp`. Neu gegenueber
frueher sind nur zwei Dinge: der Helfer bedient **mehrere** Requests statt einem,
und er haelt **einen** Chrome ueber mehrere Solves warm. `LOCKED` serialisiert
weiterhin prozessweit — zwei Suchen teilen sich den warmen Browser nie parallel.

**Grenzwerte** (alle in `shelfmark/bypass/internal_bypasser.py`, Kopf der Datei,
alle per ENV ueberschreibbar — Compose-ENV + Container-Restart genuegt, **kein**
Image-Build):

| ENV | Default | Wirkung |
| --- | --- | --- |
| `SHELFMARK_WARM_BROWSER_MAX_USES` | 12 | Seitenabrufe pro Chrome. **`=1` ist der Notschalter**: Browser wird vor dem zweiten Solve verworfen = Verhalten vor der Aenderung. |
| `SHELFMARK_WARM_BROWSER_MAX_AGE_SECONDS` | 300 | Maximales Alter eines warmen Chrome |
| `SHELFMARK_WARM_BROWSER_MAX_RSS_MB` | 300 | Chrome + Renderer darueber → ausmustern. `0` schaltet die Pruefung ab. `mem_limit` ist 512m. |
| `SHELFMARK_BYPASS_HELPER_IDLE_SECONDS` | 60 | Danach reapt der Parent den untaetigen Helfer samt Browser |
| `SHELFMARK_BYPASS_HELPER_MAX_AGE_SECONDS` | 900 | Maximales Alter des Helferprozesses |
| `SHELFMARK_BYPASS_HELPER_MAX_REQUESTS` | 40 | Requests pro Helferprozess |

**Gesundes Logbild** (`docker logs -f cwa-downloader`):

```
Started bypass helper process 1234
Chrome browser ready (Pure CDP)
Bypass successful using _bypass_method_cdp_gui_click
Reusing warm Chrome browser (use 2/12, age 34s, chrome rss 180MB)
Reusing warm Chrome browser (use 3/12, age 51s, chrome rss 196MB)
Retiring warm Chrome browser: max uses reached (12)
Bypass helper 1234 idle for 60s - releasing its browser
Stopping bypass helper 1234 after 7 request(s), 210s alive
```

Genau **ein** "Chrome browser ready" pro Batch, danach aufsteigende `use N/12`.
Die `chrome rss`-Zahl ist die Speicherkurve ueber die Wiederverwendungen — sie ist
die einzige Spur, die ein OOM-Post-Mortem hat.

**Krankes Logbild und was es heisst:**

* `Warm Chrome produced nothing (attempt 1/10) - falling back to a fresh browser`
  — einmal pro Batch ist normal (Sitzung war verbraucht). In Serie heisst es:
  DDoS-Guard bewertet wiederverwendete Sitzungen negativ →
  `SHELFMARK_WARM_BROWSER_MAX_USES=1` setzen und neu starten.
* `Retiring warm Chrome browser: chrome rss 512MB over the 300MB limit` in Serie
  — Chrome leckt. Kein Notfall (die Grenze greift ja), aber MAX_USES senken.
* `Killing bypass helper process group 1234 (browser included)` — der Helfer hing
  oder ist gestorben, ohne seinen Browser abzuraeumen. Vereinzelt ok.
* `Bypass helper group 1234 survived SIGTERM - sending SIGKILL` — dito, haerter.
* `Killing bypass helper 1234 without its process group` — `start_new_session` hat
  nicht gegriffen, der Helfer teilt unsere Gruppe. Sollte nie vorkommen; dann
  werden Chrome/Xvfb **nicht** mitgetoetet, Orphan-Gefahr.
* `Pure CDP browser startup failed` in Serie — der klassische Vergiftungszustand:
  verwaiste `chrome`/`chromium`/`Xvfb`. Pruefen und aufraeumen:
  ```bash
  docker exec cwa-downloader ps -eo pid,ppid,pgid,comm | grep -E 'chrome|Xvfb'
  docker restart cwa-downloader   # sicherster Weg
  ```

**Warum der Helfer eine eigene Prozessgruppe hat.** Chrome (SeleniumBase,
`asyncio.create_subprocess_exec`) und Xvfb (pyvirtualdisplay, `subprocess.Popen`)
erben die Gruppe des Helfers. `shutdown()` schickt deshalb **immer** `SIGTERM`,
dann `SIGKILL` an die Gruppe — auch dann, wenn der Python-Kindprozess bereits tot
ist. Das ist der Fall, der frueher Orphans hinterliess: ein OOM-Kill trifft genau
einen Prozess, das `finally` des Kindes laeuft nie, Chrome und Xvfb leben weiter.
Die pgid wird direkt nach `Popen` gemerkt, weil `os.getpgid(pid)` nach dem Reap des
Leaders fehlschlaegt, die Gruppe selbst aber weiterlebt, solange Mitglieder da sind.

**Was das Methoden-Gedaechtnis ist.** Der Parent merkt sich pro Host+Challenge, welche
Methode zuletzt getragen hat, und seedet damit jedes Kind. Das Kind verwirft einen
Hint nach zwei Fehlschlaegen; sein Snapshot **ersetzt** deshalb den des Parents
(`replace_bypass_method_hints`), er wird nicht dazugemergt. Sonst waere ein Hint
unsterblich, weil `dict.update()` nie einen Schluessel entfernt.

**Nicht anfassen ohne Messung:** der `solve_captcha()`-Shortcut greift nur bei
`challenge_type == "ddos_guard"`. Auf echtem Cloudflare (welib.org, z-lib.fm) liefert
SeleniumBase ebenfalls `False`, wenn keiner seiner ~15 Turnstile-Selektoren matcht —
dort ist die 3-5s-Wartezeit danach der eigentliche Wirkmechanismus (die CF-JS laeuft
in dieser Zeit fertig). Ebenso bleibt der eskalierende Backoff zwischen
Methodenversuchen (`min(uniform(2,4)*try_count, 12)`) bewusst unangetastet: jede
Methode klickt das Challenge-Widget, jeder Klick ist eine Verifikationsanfrage.

**Bytecode-Precompile.** Das Dockerfile baut mit `uv sync --compile-bytecode` und
`compileall /app/shelfmark` in Stage `base`; `FROM base AS shelfmark` erbt die
`__pycache__`-Verzeichnisse. `PYTHONDONTWRITEBYTECODE=1` verhindert nur das
*Schreiben* zur Laufzeit, nicht das Lesen. Nach jedem Build pruefen:

```bash
docker exec cwa-downloader find /app/.venv/lib -name '*.pyc' | head
docker exec cwa-downloader find /app/shelfmark -name '*.pyc' | head
```

Beides muss Treffer liefern. Leer heisst: der Precompile-Schritt lief nicht, jeder
Helferstart kompiliert seinen Importgraph wieder selbst (~2,5s pro Solve).

**Pflicht vor dem Produktiv-Tag** (kein Unit-Test deckt das ab — die Suite laeuft
komplett gegen einen FakeDriver, es hat dort noch nie ein echter Chrome gestartet):

1. Laufendes Image sichern, sonst gibt es kein Rollback-Ziel:
   ```bash
   docker tag <aktuelles-image> shelfmark-fork:rollback-0.2.5
   ```
2. Wegwerf-Container fahren: `scripts/bypasser_permission_lab.sh`, darin drei
   AA-Seiten nacheinander holen. Erwartet: genau **ein** `Chrome browser ready`,
   danach `Reusing warm Chrome browser (use 2/12 ...)` und `use 3/12`, und am Ende
   keine verwaisten `chrome`/`Xvfb` in `ps`.
3. Precompile pruefen (Kommandos oben).
4. Erst dann taggen und `deploy media-erweiterung up -d`.

### VPN-Egress: der externe Bypasser waere ein Leck

`cwa-downloader` haengt in `network_mode: service:gluetun`, jeder Request des
Containers verlaesst die Maschine also ueber Mullvad. Der **interne** Bypasser
startet Chrome in einem Helfer-Subprozess im selben Namespace (siehe oben) und ist
damit ebenfalls gedeckt — auch der ueber mehrere Solves warm gehaltene Browser, er
lebt im selben Namespace wie der Helfer.

Der **externe** Bypasser (FlareSolverr) ist ein eigener Container: aktiviert man
`USING_EXTERNAL_BYPASSER`, wuerde der gesamte AA-Traffic ueber dessen Netzwerk
laufen — und damit ueber die Heimleitung, nicht ueber Mullvad. Seit die
AA-Aufrufe in `direct_download.py` (`search_books`, `get_book_info`,
`/dyn/md5/summary`) mit `allow_bypasser_fallback=True` laufen, betrifft das auch
den Suchpfad, der frueher nie beim Bypasser landete.

Regel: `USING_EXTERNAL_BYPASSER` bleibt in diesem Stack aus. Wird es je gebraucht,
muss der FlareSolverr-Container vorher selbst in den gluetun-Namespace.

## Incident response

### Upstream security hotfix

GitHub will notify via security alert. Run monthly sync above immediately, push new tag, deploy.

### Production image broken

```bash
# Roll back to pre-fork image:
cp ~/Documents/backups-2026-05-15/media-erweiterung_docker-compose.yml.bak.pre-fork-cutover \
   ~/docker-migration/stacks/media-erweiterung/docker-compose.yml
~/scripts/deploy.sh media-erweiterung up -d cwa-downloader
# Confirm calibrain image is running and healthy
docker ps --filter name=cwa-downloader --format "{{.Image}} {{.Status}}"
```

### Fork-branch out of sync with production image

If someone pushes to the feature branch without bumping the tag, production stays on the pinned tag. Check:

```bash
docker exec cwa-downloader curl -s http://localhost:8084/api/config | python3 -c "import json,sys; d=json.load(sys.stdin); print(d['build_version'], d['release_version'])"
```

The `build_version` after `-` is the git SHA. If it's not the latest feature-branch HEAD, the tag wasn't bumped.

## Drift detection

Quarterly: skim upstream README + CHANGELOG for breaking changes we'd want to absorb. The fork is small (1 feature commit + 1 ci commit) so most upstream releases are easy to absorb via rebase.

If upstream evolves the toggle/search-mode handling in a way that conflicts with our patches: re-assess whether to keep the fork or migrate the production to a stock upstream image with adjusted settings. Right now the upstream maintenance-mode status (declared "stable, not under active maintenance") makes conflicts unlikely.

## Upstream PR submission

Discussion draft sits at `~/Documents/shelfmark-upstream-discussion-2026-05-15.md`. After maintainer feedback:

```bash
cd ~/dev/shelfmark-fork
git checkout feature/content-type-driven-search-mode
# Address feedback if any
git push origin feature/content-type-driven-search-mode

# Open PR via gh or web UI
# Title: feat(search): always-visible content-type toggle drives per-user search mode
# Body: paste relevant sections from the discussion draft
```

If accepted upstream: switch production back to calibrain image once the feature lands in a stable release, retire fork.
