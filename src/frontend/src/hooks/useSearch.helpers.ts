import type { DirectSearchResult } from '../services/api';
import type { Book } from '../types';

/**
 * Direct-mode paging helpers.
 *
 * Direct mode talks to Anna's Archive, where every result page costs one DDoS-Guard
 * browser solve (40-60s). Pages are therefore fetched one at a time, on demand, and
 * appended to the list already on screen. The decisions around that are pure
 * functions so they can be tested without a DOM.
 */

/** State loadMore needs to decide whether another direct-mode page may be fetched. */
export interface DirectLoadMoreState {
  hasMore: boolean;
  isLoadingMore: boolean;
  rawQuery?: string;
}

/**
 * Whether a direct-mode "load more" may start.
 *
 * Guards the double click (a second click while a ~50s solve is in flight), the end
 * of the list, and a missing query (nothing searched yet, or a reset in between).
 */
export const canLoadMoreDirect = (state: DirectLoadMoreState): boolean =>
  state.hasMore && !state.isLoadingMore && Boolean(state.rawQuery);

/**
 * Append newly fetched books, dropping ids already on screen.
 *
 * Server-side dedup only covers one page: between two pages lies a solve plus the
 * user's think time, and AA can re-rank in that window, so page 2 may repeat an entry
 * from page 1. Duplicate ids would also produce duplicate React keys.
 */
export const appendUniqueBooks = (previous: Book[], incoming: Book[]): Book[] => {
  const seen = new Set(previous.map((book) => book.id));
  const additions = incoming.filter((book) => {
    if (seen.has(book.id)) return false;
    seen.add(book.id);
    return true;
  });
  return additions.length > 0 ? [...previous, ...additions] : previous;
};

/** Everything performDirectLoadMore needs; injected so it is testable without a DOM. */
export interface DirectLoadMoreDeps {
  rawQuery: string;
  nextPage: number;
  fetchPage: (query: string, page: number) => Promise<DirectSearchResult>;
  /** True once the search this page belongs to has been replaced or cleared. */
  isStale: () => boolean;
  setBooks: (updater: (previous: Book[]) => Book[]) => void;
  setHasMore: (value: boolean) => void;
  setCurrentPage: (value: number) => void;
  showToast: (message: string, type: 'success' | 'error' | 'info') => void;
  onError: (error: unknown, fallbackMessage: string) => void;
}

/**
 * Fetch one further Anna's Archive page and fold it into the list on screen.
 *
 * Extracted from useSearch so the sequence can be tested directly: which page is
 * requested, that results are appended rather than replacing the list, that a
 * superseded page is dropped, and that a 45-60s wait always ends in a visible answer.
 */
export const performDirectLoadMore = async (deps: DirectLoadMoreDeps): Promise<void> => {
  const {
    rawQuery,
    nextPage,
    fetchPage,
    isStale,
    setBooks,
    setHasMore,
    setCurrentPage,
    showToast,
    onError,
  } = deps;

  try {
    const result = await fetchPage(rawQuery, nextPage);
    // One page is one protection-challenge solve (45-60s). The search bar and the
    // logo stay usable in that window, so the answer may belong to a search that no
    // longer exists -- appending it would refill a list the user just cleared.
    if (isStale()) return;

    // Trust the page the server actually served: it clamps the request at the browse
    // page limit, and paging on past that clamp would refetch the same page forever.
    setCurrentPage(result.page > 0 ? result.page : nextPage);
    setHasMore(result.hasMore);

    if (result.books.length > 0) {
      // Append instead of replace, and drop ids already on screen: AA can re-rank
      // between two pages, and duplicate ids break React keys.
      setBooks((previous) => appendUniqueBooks(previous, result.books));
      return;
    }

    // A minute of spinner followed by a button that silently disappears is
    // indistinguishable from a broken app -- say what happened.
    showToast(result.hasMore ? 'No results on this page' : 'No further results', 'info');
  } catch (error) {
    onError(error, 'Failed to load more results');
  }
};
