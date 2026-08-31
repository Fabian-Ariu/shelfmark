import { useState, useCallback, useRef } from 'react';
import { useNavigate } from 'react-router-dom';

import { DEFAULT_SUPPORTED_FORMATS } from '../data/languages';
import { searchBooks, searchMetadata, AuthenticationError } from '../services/api';
import type { Book, AppConfig, AdvancedFilterState, ContentType, SearchMode } from '../types';
import { LANGUAGE_OPTION_DEFAULT } from '../utils/languageFilters';
import { resolveEffectiveSearchMode } from '../utils/resolveEffectiveSearchMode';
import { appendUniqueBooks, canLoadMoreDirect, performDirectLoadMore } from './useSearch.helpers';

const DEFAULT_FORMAT_SELECTION = DEFAULT_SUPPORTED_FORMATS;

interface UseSearchOptions {
  showToast: (message: string, type: 'info' | 'success' | 'error') => void;
  setIsAuthenticated: (value: boolean) => void;
  authRequired: boolean;
  onSearchReset?: () => void;
  contentType?: ContentType;
}

// Search field values for universal mode (provider-specific fields)
type SearchFieldValues = Record<string, string | number | boolean>;

interface UseSearchReturn {
  books: Book[];
  setBooks: React.Dispatch<React.SetStateAction<Book[]>>;
  isSearching: boolean;
  lastSearchQuery: string;
  searchInput: string;
  setSearchInput: (value: string) => void;
  showAdvanced: boolean;
  setShowAdvanced: (value: boolean) => void;
  advancedFilters: AdvancedFilterState;
  setAdvancedFilters: React.Dispatch<React.SetStateAction<AdvancedFilterState>>;
  updateAdvancedFilters: (updates: Partial<AdvancedFilterState>) => void;
  handleSearch: (params: {
    query: string;
    config: AppConfig | null;
    fieldValues?: Record<string, string | number | boolean>;
    contentTypeOverride?: ContentType;
    searchMode?: SearchMode;
    providerOverride?: string;
  }) => Promise<void>;
  handleResetSearch: (config: AppConfig | null) => void;
  /** Drop the current search incl. a page still in flight; see the implementation. */
  cancelPendingLoadMore: () => void;
  resetSortFilter: () => void;
  // Universal mode search field values
  searchFieldValues: SearchFieldValues;
  updateSearchFieldValue: (key: string, value: string | number | boolean, label?: string) => void;
  searchFieldLabels: Record<string, string>;
  // Pagination (universal and direct mode)
  hasMore: boolean;
  isLoadingMore: boolean;
  loadMore: (config: AppConfig | null, searchMode?: SearchMode) => Promise<void>;
  totalFound: number;
  // Source URL and title for the current result set (e.g. Hardcover list page)
  resultsSourceUrl: string | undefined;
  resultsSourceTitle: string | undefined;
}

export function useSearch(options: UseSearchOptions): UseSearchReturn {
  const {
    showToast,
    setIsAuthenticated,
    authRequired,
    onSearchReset,
    contentType = 'ebook',
  } = options;
  const navigate = useNavigate();

  const [books, setBooks] = useState<Book[]>([]);
  const [isSearching, setIsSearching] = useState(false);
  const [lastSearchQuery, setLastSearchQuery] = useState('');
  const [searchInput, setSearchInput] = useState('');
  const [showAdvanced, setShowAdvanced] = useState(false);
  const [advancedFilters, setAdvancedFilters] = useState({
    isbn: '',
    author: '',
    title: '',
    lang: [LANGUAGE_OPTION_DEFAULT],
    sort: '',
    content: '',
    formats: DEFAULT_FORMAT_SELECTION,
  });

  // Universal mode: provider-specific search field values
  const [searchFieldValues, setSearchFieldValues] = useState<SearchFieldValues>({});
  const [searchFieldLabels, setSearchFieldLabels] = useState<Record<string, string>>({});

  // Pagination state (universal and direct mode)
  const [currentPage, setCurrentPage] = useState(1);
  const [hasMore, setHasMore] = useState(false);
  const [isLoadingMore, setIsLoadingMore] = useState(false);
  const [totalFound, setTotalFound] = useState(0);
  const [resultsSourceUrl, setResultsSourceUrl] = useState<string | undefined>();
  const [resultsSourceTitle, setResultsSourceTitle] = useState<string | undefined>();

  // Store last search params for loadMore
  const lastSearchParamsRef = useRef<{
    query: string;
    sort: string;
    fieldValues: SearchFieldValues;
    providerOverride?: string;
    contentType: ContentType;
    // Direct mode: the full built query string (query + isbn/author/title/lang/
    // content/format/sort). `query` alone would drop every filter on the next page.
    rawQuery?: string;
  } | null>(null);

  // Bumped by every new search. A direct-mode page can be in flight for 45-60s and
  // the search bar stays usable meanwhile, so loadMore checks this after its await
  // and drops results that belong to a search the user has already replaced.
  const searchGenerationRef = useRef(0);

  /**
   * Throw away the search currently on screen, including one still in flight.
   *
   * Bumping the generation is the load-bearing part: a direct-mode page can be 45-60s
   * in flight, and without the bump its result would still be appended after the list
   * was cleared -- the emptied results would reappear on their own, holding page 2
   * without page 1. Every caller that clears `books` must call this (App.tsx clears
   * them on logo click, search-mode switch, content-type switch and logout).
   */
  const cancelPendingLoadMore = useCallback(() => {
    searchGenerationRef.current += 1;
    lastSearchParamsRef.current = null;
    setIsLoadingMore(false);
    setHasMore(false);
    setCurrentPage(1);
    setTotalFound(0);
  }, []);

  const updateAdvancedFilters = useCallback((updates: Partial<AdvancedFilterState>) => {
    setAdvancedFilters((prev) => ({ ...prev, ...updates }));
  }, []);

  const updateSearchFieldValue = useCallback(
    (key: string, value: string | number | boolean, label?: string) => {
      setSearchFieldValues((prev) => ({ ...prev, [key]: value }));
      setSearchFieldLabels((prev) => {
        const next = { ...prev };
        if (label !== undefined) {
          if (label) {
            next[key] = label;
          } else {
            delete next[key];
          }
          return next;
        }

        delete next[key];
        return next;
      });
    },
    [],
  );

  const resetSortFilter = useCallback(() => {
    setAdvancedFilters((prev) => ({ ...prev, sort: '' }));
  }, []);

  // Helper to handle authentication and other errors consistently
  const handleSearchError = useCallback(
    (error: unknown, context: string) => {
      if (error instanceof AuthenticationError) {
        setIsAuthenticated(false);
        if (authRequired) {
          void navigate('/login', { replace: true });
        }
        return;
      }

      console.error(`${context}:`, error);
      const message = error instanceof Error ? error.message : context;
      showToast(message, 'error');
    },
    [setIsAuthenticated, authRequired, navigate, showToast],
  );

  const handleSearch = useCallback(
    async ({
      query,
      config,
      fieldValues,
      contentTypeOverride,
      searchMode: searchModeOverride,
      providerOverride,
    }: {
      query: string;
      config: AppConfig | null;
      fieldValues?: Record<string, string | number | boolean>;
      contentTypeOverride?: ContentType;
      searchMode?: SearchMode;
      providerOverride?: string;
    }) => {
      const effectiveContentType = contentTypeOverride ?? contentType;
      searchGenerationRef.current += 1;
      const requestedSearchMode = (searchModeOverride ?? config?.search_mode) || 'universal';
      // Architektur-Regel: Audiobook-Toggle hat KEINEN Direct-Mode-Pfad.
      // Direct mode routes via /api/releases?source=direct_download (Anna's
      // Archive full-text), which has no reliable server-side audio-format
      // filter (Block A empirics). Force universal mode here so the metadata-
      // provider layer (combined_audiobook → Audible + Hardcover) runs.
      // SearchBar's toggle handler also tries to set this via onSearchModeChange,
      // but we keep the defensive check at dispatch-time too — covers stale
      // persisted state, deep-linked URLs, race conditions.
      const searchMode = resolveEffectiveSearchMode(requestedSearchMode, effectiveContentType);

      // In universal mode, check if we have either a query or field values
      if (searchMode === 'universal') {
        const params = new URLSearchParams(query);
        const searchQuery = params.get('query') || '';
        // Use explicitly passed fieldValues if provided, otherwise fall back to state
        const effectiveFieldValues = fieldValues ?? searchFieldValues;
        const hasFieldValues = Object.values(effectiveFieldValues).some(
          (v) => v !== '' && v !== false,
        );
        const sort = params.get('sort') || 'relevance';

        if (!searchQuery && !hasFieldValues) {
          setBooks([]);
          setLastSearchQuery('');
          setHasMore(false);
          setTotalFound(0);
          setCurrentPage(1);
          setResultsSourceUrl(undefined);
          setResultsSourceTitle(undefined);
          lastSearchParamsRef.current = null;
          return;
        }

        setIsSearching(true);
        setLastSearchQuery(query);
        // Reset pagination for new search
        setCurrentPage(1);
        setHasMore(false);
        setTotalFound(0);

        try {
          const result = await searchMetadata(
            searchQuery,
            40,
            sort,
            effectiveFieldValues,
            1,
            effectiveContentType,
            providerOverride,
          );
          if (result.books.length > 0) {
            setBooks(result.books);
            setHasMore(result.hasMore);
            setTotalFound(result.totalFound);
            setResultsSourceUrl(result.sourceUrl);
            setResultsSourceTitle(result.sourceTitle);
            // Replace URL in search input with list title for display
            if (result.sourceTitle && searchQuery) {
              setSearchInput(result.sourceTitle);
            }
            // Store params for loadMore
            lastSearchParamsRef.current = {
              query: searchQuery,
              sort,
              fieldValues: effectiveFieldValues,
              providerOverride,
              contentType: effectiveContentType,
            };
          } else {
            setBooks([]);
            setHasMore(false);
            setTotalFound(0);
            setResultsSourceUrl(undefined);
            setResultsSourceTitle(undefined);
            showToast('No results found', 'error');
          }
        } catch (error) {
          handleSearchError(error, 'Search failed');
        } finally {
          setIsSearching(false);
        }
        return;
      }

      // Direct mode: require a query
      if (!query) {
        setBooks([]);
        setLastSearchQuery('');
        setHasMore(false);
        setTotalFound(0);
        setCurrentPage(1);
        lastSearchParamsRef.current = null;
        return;
      }
      setIsSearching(true);
      setLastSearchQuery(query);
      // Reset pagination for new search
      setCurrentPage(1);
      setHasMore(false);
      setTotalFound(0);

      try {
        const result = await searchBooks(query, 1);

        if (result.books.length > 0) {
          setBooks(result.books);
          setHasMore(result.hasMore);
          // Store params for loadMore. AA reports no total, so totalFound stays 0
          // and the "showing X of Y" line stays hidden.
          lastSearchParamsRef.current = {
            query: '',
            sort: '',
            fieldValues: {},
            contentType: effectiveContentType,
            rawQuery: query,
          };
        } else {
          setHasMore(false);
          lastSearchParamsRef.current = null;
          showToast('No results found', 'error');
        }
      } catch (error) {
        setHasMore(false);
        lastSearchParamsRef.current = null;
        if (error instanceof AuthenticationError) {
          handleSearchError(error, 'Search failed');
        } else {
          console.error('Search failed:', error);
          const message = error instanceof Error ? error.message : 'Search failed';
          const friendly =
            message.includes('Network restricted') || message.includes('Unable to reach')
              ? message
              : 'Unable to reach download source. Network may be restricted or mirrors blocked.';
          showToast(friendly, 'error');
        }
      } finally {
        setIsSearching(false);
      }
    },
    [showToast, searchFieldValues, handleSearchError, contentType],
  );

  const handleResetSearch = useCallback(
    (config: AppConfig | null) => {
      setBooks([]);
      setSearchInput('');
      setShowAdvanced(false);
      setLastSearchQuery('');
      onSearchReset?.();

      const resetFormats = config?.supported_formats || DEFAULT_FORMAT_SELECTION;
      setAdvancedFilters({
        isbn: '',
        author: '',
        title: '',
        lang: [LANGUAGE_OPTION_DEFAULT],
        sort: '',
        content: '',
        formats: resetFormats,
      });

      // Reset universal mode search field values
      setSearchFieldValues({});
      setSearchFieldLabels({});

      // Reset pagination and drop a page that is still in flight
      cancelPendingLoadMore();
      setResultsSourceUrl(undefined);
      setResultsSourceTitle(undefined);
    },
    [cancelPendingLoadMore, onSearchReset],
  );

  // Load more results (universal metadata pages / direct AA result pages)
  const loadMore = useCallback(
    async (config: AppConfig | null, searchModeOverride?: SearchMode) => {
      const searchMode = (searchModeOverride ?? config?.search_mode) || 'universal';
      if (!lastSearchParamsRef.current) return;
      if (isLoadingMore || !hasMore) return;

      const {
        query,
        sort,
        fieldValues,
        providerOverride,
        contentType: searchContentType,
        rawQuery,
      } = lastSearchParamsRef.current;
      const nextPage = currentPage + 1;
      const generation = searchGenerationRef.current;

      // Direct mode: one more Anna's Archive page, fetched only because the user
      // asked for it (each page is one DDoS-Guard solve, ~45-60s).
      if (searchMode !== 'universal') {
        if (!canLoadMoreDirect({ hasMore, isLoadingMore, rawQuery }) || !rawQuery) return;

        setIsLoadingMore(true);
        try {
          await performDirectLoadMore({
            rawQuery,
            nextPage,
            fetchPage: searchBooks,
            isStale: () => searchGenerationRef.current !== generation,
            setBooks,
            setHasMore,
            setCurrentPage,
            showToast,
            onError: handleSearchError,
          });
        } finally {
          setIsLoadingMore(false);
        }
        return;
      }

      setIsLoadingMore(true);

      try {
        const result = await searchMetadata(
          query,
          40,
          sort,
          fieldValues,
          nextPage,
          searchContentType,
          providerOverride,
        );
        if (searchGenerationRef.current !== generation) return;
        if (result.books.length > 0) {
          setBooks((prev) => appendUniqueBooks(prev, result.books));
          setHasMore(result.hasMore);
          setCurrentPage(nextPage);
        } else {
          setHasMore(false);
        }
      } catch (error) {
        handleSearchError(error, 'Failed to load more results');
      } finally {
        setIsLoadingMore(false);
      }
    },
    [currentPage, hasMore, isLoadingMore, handleSearchError, showToast],
  );

  return {
    books,
    setBooks,
    isSearching,
    lastSearchQuery,
    searchInput,
    setSearchInput,
    showAdvanced,
    setShowAdvanced,
    advancedFilters,
    setAdvancedFilters,
    updateAdvancedFilters,
    handleSearch,
    handleResetSearch,
    cancelPendingLoadMore,
    resetSortFilter,
    // Universal mode search field values
    searchFieldValues,
    updateSearchFieldValue,
    searchFieldLabels,
    // Pagination (universal and direct mode)
    hasMore,
    isLoadingMore,
    loadMore,
    totalFound,
    resultsSourceUrl,
    resultsSourceTitle,
  };
}
