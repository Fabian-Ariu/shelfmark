import type { ContentType, SearchMode } from '../types';

/**
 * Resolve the effective search mode at search-dispatch time.
 *
 * Background: SEARCH_MODE=direct routes searches via `/api/releases?source=direct_download`
 * (Anna's Archive full-text), which bypasses the metadata-provider layer entirely.
 * That is intended for ebook search but produces wrong results for audiobooks —
 * AA has no reliable server-side audio-format filter (see Block-A empirics), so
 * an audiobook query in Direct mode returns epub/PDF hits.
 *
 * `contentTypeToSearchMode()` already handles the toggle-click path
 * (SearchBar.tsx propagates the new mode via onSearchModeChange). This function
 * is the defensive last-line override at the search-dispatch layer: even if
 * userSearchMode somehow ended up as `direct` while the toggle says `audiobook`
 * (race conditions, stale persisted state, deep-linked URLs, etc.), force
 * universal mode so the metadata-provider layer (combined_audiobook → Audible
 * + Hardcover) actually runs.
 *
 * Pure function; only the caller writes back state.
 */
export function resolveEffectiveSearchMode(
  requestedMode: SearchMode,
  contentType: ContentType,
): SearchMode {
  if (contentType === 'audiobook') return 'universal';
  return requestedMode;
}
