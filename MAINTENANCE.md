# Shelfmark Fork — Maintenance Playbook

**Repo:** github.com/Fabian-Ariu/shelfmark
**Upstream:** github.com/calibrain/shelfmark
**Production deploy:** `ghcr.io/fabian-ariu/shelfmark:v1.3.0-fork-0.1.0` on Mac Mini cwa-downloader (port 8085 via gluetun)

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

git checkout feature/content-type-driven-search-mode
git rebase main
# If conflicts in SearchBar.tsx / App.tsx: integrate carefully, run npm gates
cd src/frontend && npm install && npm run lint && npm run typecheck && npm run test:unit && cd ../..
git push origin feature/content-type-driven-search-mode --force-with-lease
```

### After upstream-sync — release bump

Only needed if upstream changes touch files we patched (SearchBar.tsx, App.tsx, SearchSection.tsx, the two utils):

```bash
# After rebase + green CI gates:
git tag v1.3.0-fork-0.1.X   # bump X
git push origin v1.3.0-fork-0.1.X

# Wait for GHCR build (https://github.com/Fabian-Ariu/shelfmark/actions)
# Then update production compose:
sed -i.bak \
  's|fabian-ariu/shelfmark:v1.3.0-fork-0.1.[0-9]*|fabian-ariu/shelfmark:v1.3.0-fork-0.1.X|' \
  ~/docker-migration/stacks/media-erweiterung/docker-compose.yml
~/scripts/deploy.sh media-erweiterung up -d cwa-downloader
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

If someone pushes to feature branch without bumping the tag, production stays on `v1.3.0-fork-0.1.0`. Check:

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
