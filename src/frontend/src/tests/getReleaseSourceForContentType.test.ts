import { describe, expect, it } from 'vitest';

import type { Book } from '../types';
import { getReleaseSourceForContentType } from '../utils/getReleaseSourceForContentType';

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
      expect(
        getReleaseSourceForContentType(ebookProviderBook, 'audiobook', 'audiobookbay'),
      ).toBe('audiobookbay');
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
      expect(getReleaseSourceForContentType(book, 'ebook', 'audiobookbay')).toBe(
        'direct_download',
      );
    });

    it('falls back to book.provider when source missing (legacy direct-mode)', () => {
      const book: Book = { ...baseBook, provider: 'direct_download', source: undefined };
      expect(getReleaseSourceForContentType(book, 'ebook', 'audiobookbay')).toBe(
        'direct_download',
      );
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
      expect(getReleaseSourceForContentType(book, 'ebook', 'audiobookbay')).toBe(
        'direct_download',
      );
    });
  });
});
