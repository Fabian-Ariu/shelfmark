import type { SearchMode } from '../types';

const STORAGE_PREFIX = 'shelfmark:searchMode:';
const VALID_MODES: readonly SearchMode[] = ['direct', 'universal'];

function storageKey(userId: string | null | undefined): string {
  return `${STORAGE_PREFIX}${userId ?? 'shared'}`;
}

function isValidMode(value: unknown): value is SearchMode {
  return typeof value === 'string' && (VALID_MODES as readonly string[]).includes(value);
}

/**
 * Reads the persisted search-mode override for the given user.
 * Returns the fallback if storage is unavailable, the key is missing,
 * or the stored value is no longer a valid SearchMode (defensive against
 * future renames/deprecations).
 */
export function getStoredSearchMode(
  userId: string | null | undefined,
  fallback: SearchMode | null,
): SearchMode | null {
  if (typeof window === 'undefined') return fallback;
  try {
    const raw = window.localStorage.getItem(storageKey(userId));
    return isValidMode(raw) ? raw : fallback;
  } catch {
    return fallback;
  }
}

/**
 * Persists a search-mode choice for the given user. Silently no-ops when
 * storage is unavailable (private browsing, quota exceeded, SSR).
 */
export function setStoredSearchMode(userId: string | null | undefined, mode: SearchMode): void {
  if (typeof window === 'undefined') return;
  if (!isValidMode(mode)) return;
  try {
    window.localStorage.setItem(storageKey(userId), mode);
  } catch {
    // Quota exceeded, storage disabled, etc. — non-fatal.
  }
}

export function clearStoredSearchMode(userId: string | null | undefined): void {
  if (typeof window === 'undefined') return;
  try {
    window.localStorage.removeItem(storageKey(userId));
  } catch {
    // non-fatal
  }
}
