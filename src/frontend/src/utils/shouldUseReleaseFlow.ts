import type { Book, SearchMode } from '../types';
import { isMetadataBook } from '../types';

/**
 * Decide whether a search result must be actioned via the release flow
 * (BookGetButton → /api/releases → ReleaseModal) instead of the direct
 * download action (BookDownloadButton → buildReleaseDataFromDirectBook).
 *
 * Two independent reasons force the release flow:
 *
 * 1. `searchMode === 'universal'` — the historical rule. App.tsx feeds the
 *    SearchModeContext with `resolveEffectiveSearchMode(persistedMode,
 *    contentType)`, so an audiobook search is always universal here.
 * 2. `isMetadataBook(book)` — per-book last line of defence. A metadata book
 *    (`provider`/`provider_id` set, `provider !== source`, e.g. an Audible hit
 *    with `id = "audible:B00NWCPRBU"`) has no release identity. The direct
 *    payload would carry `source_id = book.id` and no `source_url`, which the
 *    AudiobookBay handler cannot resolve ("Missing AudiobookBay details URL").
 *    Results can outlive the search mode that produced them (header content-type
 *    toggle keeps the old `books` array), so the mode alone is not sufficient.
 *
 * Source-backed direct results are safe: `transformSourceBackedDataToBook` sets
 * `provider === source`, so `isMetadataBook` is false and the eBook direct-mode
 * path is unchanged.
 *
 * Pure function; mirrors the predicate DetailsModal already uses.
 */
export function shouldUseReleaseFlow(searchMode: SearchMode, book: Book): boolean {
  return searchMode === 'universal' || isMetadataBook(book);
}
