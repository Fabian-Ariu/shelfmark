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
   `book.source`. `effectiveSearchMode` in App.tsx ist **nicht**
   content-type-aware und bleibt `direct` → `SearchModeContext` = `direct`.
3. Header-Toggle zurück auf *Ebook*: `effectiveContentType='ebook'`,
   `SearchModeContext='direct'`, aber `books` sind noch die Audiobook-
   Metadata-Books.

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
`direct_download.py` zur Laufzeit:

```bash
git show <neuer-tag>:shelfmark/release_sources/direct_download.py > /tmp/dd_new.py
diff -u /tmp/dd_new.py ~/docker-migration/stacks/media-erweiterung/direct_download_patched.py
# Erwartung: NUR die drei Patch-Familien (import os / CF-Bypass / Pagination-Loop)
```

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
