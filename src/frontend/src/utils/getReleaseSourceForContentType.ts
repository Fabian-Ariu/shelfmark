import type { Book, ContentType } from '../types';

/**
 * Resolve the release-source name for a download payload, content-type-aware.
 *
 * Background: `getBrowseSource(book)` returns `book.source || book.provider`.
 * In direct-mode-ebook flows this happens to work — book.provider is
 * `direct_download`, which is both a metadata-provider name and a registered
 * release-source name. With variante-2-prime that identity is gone:
 * audiobook-toggle uses Audible / Hardcover / combined_audiobook as metadata
 * providers, none of which are release sources. The actual release source for
 * audiobook downloads is configured via DEFAULT_RELEASE_SOURCE_AUDIOBOOK
 * (default: audiobookbay).
 *
 * If we ever add a separate ebook metadata-provider path (Hardcover for
 * ebook), the same gap will appear there too — Hardcover is not a release
 * source. That fix is tracked as a backlog item (Final-Report B.8).
 *
 * @param book - Book record from search results
 * @param contentType - Effective content type at request time
 * @param defaultAudiobookSource - DEFAULT_RELEASE_SOURCE_AUDIOBOOK from
 *   config; falls back to legacy resolution if not set
 */
export function getReleaseSourceForContentType(
  book: Book,
  contentType: ContentType,
  defaultAudiobookSource: string | null | undefined,
): string {
  if (contentType === 'audiobook') {
    const trimmed = (defaultAudiobookSource ?? '').trim();
    if (trimmed) return trimmed;
    // No default configured — let the caller see a real error instead of
    // silently falling back to book.provider (which would re-introduce the
    // "Unknown release source: audible" bug).
    throw new Error(
      `Book ${book.id}: audiobook content_type but no default_release_source_audiobook configured`,
    );
  }
  // ebook (and combined-mode ebook phase): legacy behavior. book.source wins,
  // book.provider is the direct_download fallback.
  const source = book.source || book.provider;
  if (source) return source;
  throw new Error(`Book ${book.id} is missing source context`);
}
