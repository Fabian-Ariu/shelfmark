import { describe, expect, it } from 'vitest';

import {
  appendUniqueBooks,
  canLoadMoreDirect,
  performDirectLoadMore,
} from '../hooks/useSearch.helpers';
import type { DirectSearchResult } from '../services/api';
import { buildDirectSearchQuery } from '../services/directSearchHelpers';
import type { Book } from '../types';

const book = (id: string): Book => ({ id, title: `Title ${id}`, author: 'Author' });

describe('buildDirectSearchQuery', () => {
  it('leaves page 1 untouched so the first search keeps its current URL', () => {
    expect(buildDirectSearchQuery('query=dune&format=epub', 1)).toBe('query=dune&format=epub');
  });

  it('appends the page for every further page', () => {
    expect(buildDirectSearchQuery('query=dune&format=epub', 3)).toBe(
      'query=dune&format=epub&page=3',
    );
  });

  it('keeps the full filter query, not just the search term', () => {
    const query = 'query=dune&lang=de&lang=en&format=epub&sort=newest';
    expect(buildDirectSearchQuery(query, 2)).toBe(`${query}&page=2`);
  });

  it('falls back to page 1 for nonsense page numbers', () => {
    expect(buildDirectSearchQuery('query=dune', 0)).toBe('query=dune');
    expect(buildDirectSearchQuery('query=dune', -4)).toBe('query=dune');
    expect(buildDirectSearchQuery('query=dune', Number.NaN)).toBe('query=dune');
  });
});

describe('canLoadMoreDirect', () => {
  const base = { hasMore: true, isLoadingMore: false, rawQuery: 'query=dune' };

  it('allows the next page after a search that reported more', () => {
    expect(canLoadMoreDirect(base)).toBe(true);
  });

  it('blocks a second click while a page is still loading', () => {
    // Each page is one ~45s protection-challenge solve; a double click would buy a
    // second one for nothing.
    expect(canLoadMoreDirect({ ...base, isLoadingMore: true })).toBe(false);
  });

  it('blocks once the backend reported the last page', () => {
    expect(canLoadMoreDirect({ ...base, hasMore: false })).toBe(false);
  });

  it('blocks when no direct search is on screen', () => {
    expect(canLoadMoreDirect({ ...base, rawQuery: undefined })).toBe(false);
    expect(canLoadMoreDirect({ ...base, rawQuery: '' })).toBe(false);
  });
});

describe('appendUniqueBooks', () => {
  it('appends the next page below the results already on screen', () => {
    const result = appendUniqueBooks([book('a'), book('b')], [book('c'), book('d')]);
    expect(result.map((entry) => entry.id)).toEqual(['a', 'b', 'c', 'd']);
  });

  it('drops entries the previous page already showed', () => {
    // AA can re-rank between two page fetches, so page 2 may repeat page 1 entries.
    const result = appendUniqueBooks([book('a'), book('b')], [book('b'), book('c')]);
    expect(result.map((entry) => entry.id)).toEqual(['a', 'b', 'c']);
  });

  it('drops duplicates inside the incoming page as well', () => {
    const result = appendUniqueBooks([book('a')], [book('c'), book('c')]);
    expect(result.map((entry) => entry.id)).toEqual(['a', 'c']);
  });

  it('keeps the previous list identical when the page adds nothing', () => {
    const previous = [book('a'), book('b')];
    expect(appendUniqueBooks(previous, [book('a')])).toBe(previous);
    expect(appendUniqueBooks(previous, [])).toBe(previous);
  });
});

describe('performDirectLoadMore', () => {
  const page2 = { books: [book('c'), book('d')], hasMore: true, page: 2 };

  const harness = (result: DirectSearchResult | Error, stale = false) => {
    const toasts: Array<[string, string]> = [];
    const errors: unknown[] = [];
    let list: Book[] = [book('a'), book('b')];
    let hasMore: boolean | undefined;
    let currentPage: number | undefined;
    const requested: Array<[string, number]> = [];

    const run = () =>
      performDirectLoadMore({
        rawQuery: 'query=sapiens&lang=de',
        nextPage: 2,
        fetchPage: (query, page) => {
          requested.push([query, page]);
          return result instanceof Error ? Promise.reject(result) : Promise.resolve(result);
        },
        isStale: () => stale,
        setBooks: (updater) => {
          list = updater(list);
        },
        setHasMore: (value) => {
          hasMore = value;
        },
        setCurrentPage: (value) => {
          currentPage = value;
        },
        showToast: (message, type) => toasts.push([message, type]),
        onError: (error) => errors.push(error),
      });

    return {
      run,
      requested,
      toasts,
      errors,
      get list() {
        return list;
      },
      get hasMore() {
        return hasMore;
      },
      get currentPage() {
        return currentPage;
      },
    };
  };

  it('asks for the next page with the full query of the current search', async () => {
    const h = harness(page2);
    await h.run();

    expect(h.requested).toEqual([['query=sapiens&lang=de', 2]]);
  });

  it('appends the page instead of replacing the list', async () => {
    const h = harness(page2);
    await h.run();

    expect(h.list.map((entry) => entry.id)).toEqual(['a', 'b', 'c', 'd']);
    expect(h.hasMore).toBe(true);
    expect(h.currentPage).toBe(2);
  });

  it('follows the page the server actually served, not the one requested', async () => {
    // The backend clamps at the browse page limit; paging on past the clamp would
    // refetch the same page on every further click.
    const h = harness({ books: [book('c')], hasMore: false, page: 20 });
    await h.run();

    expect(h.currentPage).toBe(20);
  });

  it('drops a page that belongs to a search the user has already left', async () => {
    // The click-to-answer window is 45-60s; without this the cleared result list
    // refills itself out of nowhere, holding page 2 without page 1.
    const h = harness(page2, true);
    await h.run();

    expect(h.list.map((entry) => entry.id)).toEqual(['a', 'b']);
    expect(h.hasMore).toBeUndefined();
    expect(h.currentPage).toBeUndefined();
    expect(h.toasts).toEqual([]);
  });

  it('answers an empty last page instead of just removing the button', async () => {
    const h = harness({ books: [], hasMore: false, page: 2 });
    await h.run();

    expect(h.toasts).toEqual([['No further results', 'info']]);
    expect(h.hasMore).toBe(false);
  });

  it('keeps paging offered when an empty page is not the last one', async () => {
    const h = harness({ books: [], hasMore: true, page: 2 });
    await h.run();

    expect(h.toasts).toEqual([['No results on this page', 'info']]);
    expect(h.hasMore).toBe(true);
    expect(h.currentPage).toBe(2);
  });

  it('reports a failed page through the error handler', async () => {
    const failure = new Error('Unable to reach download source');
    const h = harness(failure);
    await h.run();

    expect(h.errors).toEqual([failure]);
    expect(h.list.map((entry) => entry.id)).toEqual(['a', 'b']);
  });
});
