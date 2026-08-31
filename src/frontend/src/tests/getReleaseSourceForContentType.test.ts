import { describe, expect, it } from 'vitest';

import type { Book } from '../types';
import {
  getReleaseSourceForContentType,
  resolveReleaseSourceOrNull,
} from '../utils/getReleaseSourceForContentType';

const baseBook: Book = {
  id: 'B082BHJMFF',
  title: 'The Martian',
  author: 'Andy Weir',
  provider: 'audible',
  provider_id: 'B082BHJMFF',
};

describe('getReleaseSourceForContentType', () => {
  describe('audiobook content_type', () => {
    it('uses the configured audiobook release source', () => {
      expect(getReleaseSourceForContentType(baseBook, 'audiobook', 'audiobookbay')).toBe(
        'audiobookbay',
      );
    });

    it('ignores book.provider even if it is an existing release-source name', () => {
      // E.g. book.provider="direct_download" but we are in audiobook mode →
      // still use the configured audiobook source, not provider.
      const ebookProviderBook: Book = { ...baseBook, provider: 'direct_download' };
      expect(getReleaseSourceForContentType(ebookProviderBook, 'audiobook', 'audiobookbay')).toBe(
        'audiobookbay',
      );
    });

    it('throws when audiobook source is not configured', () => {
      expect(() => getReleaseSourceForContentType(baseBook, 'audiobook', null)).toThrow(
        /no default_release_source_audiobook/,
      );
      expect(() => getReleaseSourceForContentType(baseBook, 'audiobook', '')).toThrow();
      expect(() => getReleaseSourceForContentType(baseBook, 'audiobook', '   ')).toThrow();
    });
  });

  describe('ebook content_type (legacy direct-mode path)', () => {
    it('uses book.source when present', () => {
      const book: Book = { ...baseBook, source: 'direct_download', provider: 'something_else' };
      expect(getReleaseSourceForContentType(book, 'ebook', 'audiobookbay')).toBe('direct_download');
    });

    it('falls back to book.provider when source missing (legacy direct-mode)', () => {
      const book: Book = { ...baseBook, provider: 'direct_download', source: undefined };
      expect(getReleaseSourceForContentType(book, 'ebook', 'audiobookbay')).toBe('direct_download');
    });

    it('throws when both source and provider are missing', () => {
      // Mimic a malformed book with no provider/source. Use Partial<Book> + cast
      // to avoid lint noise — Book.provider is technically required by the type
      // but the runtime function defends against the legacy case anyway.
      const malformed = { ...baseBook, provider: '', source: undefined } as Book;
      expect(() => getReleaseSourceForContentType(malformed, 'ebook', 'audiobookbay')).toThrow(
        /missing source context/,
      );
    });

    it('ignores audiobook-source for ebook content type', () => {
      const book: Book = { ...baseBook, provider: 'direct_download' };
      // Even with an audiobook-source configured, ebook stays on book.provider
      expect(getReleaseSourceForContentType(book, 'ebook', 'audiobookbay')).toBe('direct_download');
    });
  });

  // --- Universal-mode ebook: book.provider is a METADATA provider (hardcover,
  // audible, googlebooks, openlibrary, combined_audiobook), never a release
  // source. Regression guard for the latent "Unknown release source: hardcover"
  // bug (Final-Report B.8).

  describe('ebook content_type (universal-mode / metadata provider)', () => {
    const hardcoverBook: Book = {
      ...baseBook,
      provider: 'hardcover',
      provider_id: '12345',
      source: undefined,
    };

    it('never returns the metadata provider name', () => {
      for (const provider of ['hardcover', 'audible', 'googlebooks', 'openlibrary']) {
        const book: Book = { ...hardcoverBook, provider };
        expect(() => getReleaseSourceForContentType(book, 'ebook', 'audiobookbay')).toThrow(
          /is a metadata provider, not a release source/,
        );
      }
    });

    it('lets book.source win for a metadata-provider book that carries one', () => {
      const book: Book = { ...hardcoverBook, source: 'prowlarr' };
      expect(getReleaseSourceForContentType(book, 'ebook', 'audiobookbay')).toBe('prowlarr');
    });

    it('keeps the legacy direct-mode provider (registered release source)', () => {
      // Production-critical: direct-mode hits carry provider="direct_download",
      // which IS a registered release source and must resolve unchanged.
      const book: Book = { ...hardcoverBook, provider: 'direct_download' };
      expect(getReleaseSourceForContentType(book, 'ebook', 'audiobookbay')).toBe('direct_download');
    });

    it('normalizes the provider name it validated', () => {
      const book: Book = { ...hardcoverBook, provider: ' Direct_Download ' };
      expect(getReleaseSourceForContentType(book, 'ebook', 'audiobookbay')).toBe('direct_download');
    });
  });

  // --- Render-path variant: getDirectActionButtonState → getDirectPolicyMode
  // runs during render and there is no ErrorBoundary, so the resolver used
  // there must return null instead of throwing.

  describe('resolveReleaseSourceOrNull (render path)', () => {
    const hardcoverBook: Book = {
      ...baseBook,
      provider: 'hardcover',
      provider_id: '12345',
      source: undefined,
    };

    it('returns null instead of throwing for a metadata book', () => {
      expect(resolveReleaseSourceOrNull(hardcoverBook, 'ebook', 'audiobookbay')).toBeNull();
      expect(resolveReleaseSourceOrNull(baseBook, 'ebook', 'audiobookbay')).toBeNull();
    });

    it('returns null instead of throwing when the audiobook default is missing', () => {
      expect(resolveReleaseSourceOrNull(baseBook, 'audiobook', null)).toBeNull();
      expect(resolveReleaseSourceOrNull(baseBook, 'audiobook', '  ')).toBeNull();
    });

    it('resolves exactly like the throwing variant when a source exists', () => {
      const sourceBacked: Book = { ...baseBook, source: 'direct_download' };
      expect(resolveReleaseSourceOrNull(sourceBacked, 'ebook', 'audiobookbay')).toBe(
        'direct_download',
      );
      expect(resolveReleaseSourceOrNull(baseBook, 'audiobook', 'audiobookbay')).toBe(
        'audiobookbay',
      );
      const legacyDirect: Book = { ...baseBook, provider: 'prowlarr' };
      expect(resolveReleaseSourceOrNull(legacyDirect, 'ebook', null)).toBe('prowlarr');
    });

    it('returns null for a book with no provider and no source', () => {
      const malformed = { ...baseBook, provider: '', source: undefined } as Book;
      expect(resolveReleaseSourceOrNull(malformed, 'ebook', 'audiobookbay')).toBeNull();
    });
  });
});
