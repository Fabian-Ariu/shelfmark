import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import {
  clearStoredSearchMode,
  getStoredSearchMode,
  setStoredSearchMode,
} from '../utils/searchModeStorage';

const KEY_PREFIX = 'shelfmark:searchMode:';

// Vitest defaults to the node environment, so `window` is undefined.
// We synthesize the minimum localStorage surface the util touches.
function installLocalStorageStub(override?: Partial<Storage>): Map<string, string> {
  const store = new Map<string, string>();
  const localStorage: Storage = {
    getItem: (key: string) => store.get(key) ?? null,
    setItem: (key: string, value: string) => {
      store.set(key, value);
    },
    removeItem: (key: string) => {
      store.delete(key);
    },
    clear: () => {
      store.clear();
    },
    get length() {
      return store.size;
    },
    key: (index: number) => Array.from(store.keys())[index] ?? null,
    ...override,
  };
  vi.stubGlobal('window', { localStorage });
  return store;
}

describe('searchModeStorage', () => {
  let store: Map<string, string>;

  beforeEach(() => {
    store = installLocalStorageStub();
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  describe('getStoredSearchMode', () => {
    it('returns the stored value when it is a valid SearchMode', () => {
      store.set(`${KEY_PREFIX}alice`, 'universal');
      expect(getStoredSearchMode('alice', null)).toBe('universal');
    });

    it('returns the fallback when nothing is stored', () => {
      expect(getStoredSearchMode('alice', 'direct')).toBe('direct');
      expect(getStoredSearchMode('alice', null)).toBeNull();
    });

    it('returns the fallback when the stored value is not a valid SearchMode', () => {
      // Defensive: future renames or stale entries should not crash callers.
      store.set(`${KEY_PREFIX}alice`, 'legacy-mode');
      expect(getStoredSearchMode('alice', 'direct')).toBe('direct');
    });

    it('uses the "shared" key when userId is null or undefined', () => {
      store.set(`${KEY_PREFIX}shared`, 'universal');
      expect(getStoredSearchMode(null, null)).toBe('universal');
      expect(getStoredSearchMode(undefined, null)).toBe('universal');
    });

    it('isolates values across different users', () => {
      setStoredSearchMode('alice', 'direct');
      setStoredSearchMode('bob', 'universal');
      expect(getStoredSearchMode('alice', null)).toBe('direct');
      expect(getStoredSearchMode('bob', null)).toBe('universal');
    });
  });

  describe('setStoredSearchMode', () => {
    it('writes the mode to the user-specific key', () => {
      setStoredSearchMode('alice', 'universal');
      expect(store.get(`${KEY_PREFIX}alice`)).toBe('universal');
    });

    it('writes under the "shared" key when userId is null', () => {
      setStoredSearchMode(null, 'direct');
      expect(store.get(`${KEY_PREFIX}shared`)).toBe('direct');
    });

    it('does not throw when localStorage.setItem fails (e.g. quota exceeded)', () => {
      installLocalStorageStub({
        setItem: () => {
          throw new Error('QuotaExceeded');
        },
      });
      expect(() => setStoredSearchMode('alice', 'direct')).not.toThrow();
    });
  });

  describe('clearStoredSearchMode', () => {
    it('removes the stored value for the given user', () => {
      setStoredSearchMode('alice', 'universal');
      clearStoredSearchMode('alice');
      expect(store.get(`${KEY_PREFIX}alice`)).toBeUndefined();
    });

    it('does not affect other users', () => {
      setStoredSearchMode('alice', 'direct');
      setStoredSearchMode('bob', 'universal');
      clearStoredSearchMode('alice');
      expect(store.get(`${KEY_PREFIX}bob`)).toBe('universal');
    });
  });
});
