import type { Book, ContentType } from '../types';

/**
 * Release-source names registered in the backend plugin registry
 * (`shelfmark/release_sources/__init__.py`, `_SOURCES` / `@register_source`).
 *
 * Why a POSITIVE list of release sources instead of a negative list of
 * metadata providers (hardcover, audible, googlebooks, openlibrary,
 * combined_audiobook):
 *
 * - A negative list breaks SILENTLY the moment upstream adds a metadata
 *   provider. The new provider name is not on the deny-list, so it is treated
 *   as a release source and shipped to the backend, which answers with
 *   `Unknown release source: <name>` — exactly the bug this guard exists for.
 *   Metadata providers are the side that grows (Hardcover, Audible and
 *   combined_audiobook all arrived after direct_download).
 * - A positive list degrades LOUDLY-but-safely: an unlisted name is treated as
 *   "not a release source", so the caller gets a named error (payload path) or
 *   `null` (render path) — we never hand the backend a name it cannot resolve.
 * - The residual risk (a NEW release source not yet listed here) is nearly
 *   inert, because every source-backed search result carries `book.source`
 *   (see `transformSourceBackedDataToBook` in bookTransformers.ts) and
 *   `book.source` is checked BEFORE this list. The list only ever decides for
 *   metadata-shaped books, where the provider is a metadata provider by
 *   construction.
 *
 * The fully robust variant would be the live `/api/release-sources` list, but
 * that is fetched asynchronously (only inside the ReleaseModal session today).
 * During the load window the list would be empty and every direct-mode ebook
 * result would look like a metadata book. A timing-dependent source resolution
 * is a worse failure mode than a stale constant, so we keep the constant and
 * mirror the backend registry here.
 *
 * KEEP IN SYNC with `_BUILTIN_SOURCE_MODULES` in
 * `shelfmark/release_sources/__init__.py`.
 */
const KNOWN_RELEASE_SOURCES = new Set([
  'audiobookbay',
  'direct_download',
  'irc',
  'newznab',
  'prowlarr',
]);

const normalizeSourceName = (name: string | null | undefined): string => {
  return (name ?? '').trim().toLowerCase();
};

/**
 * Resolve the release-source name for a book, content-type-aware, WITHOUT
 * throwing. Returns `null` when no release source can be resolved.
 *
 * Use this in render/policy paths. There is no ErrorBoundary in this app
 * (`grep -r componentDidCatch src/frontend/src` is empty), so a throw during
 * render unmounts the whole tree — a white page that only a reload recovers.
 * The throwing variant below is for payload/download paths only, where the
 * caller can turn the error into a toast.
 *
 * Resolution rules:
 * - audiobook: DEFAULT_RELEASE_SOURCE_AUDIOBOOK, never `book.provider`
 *   (unchanged since the v1.3.0-fork-0.2.3 fix — `book.provider` is `audible` /
 *   `hardcover` / `combined_audiobook` there, all metadata providers).
 * - ebook: `book.source` (source-backed search result) wins, then
 *   `book.provider` if it is a registered release source (legacy direct-mode
 *   and source-backed browse — production-critical, unchanged). A metadata
 *   provider name is NEVER returned.
 *
 * @param book - Book record from search results
 * @param contentType - Effective content type at request time
 * @param defaultAudiobookSource - DEFAULT_RELEASE_SOURCE_AUDIOBOOK from config
 */
export function resolveReleaseSourceOrNull(
  book: Book,
  contentType: ContentType,
  defaultAudiobookSource: string | null | undefined,
): string | null {
  if (contentType === 'audiobook') {
    const trimmed = (defaultAudiobookSource ?? '').trim();
    return trimmed || null;
  }

  // ebook (and combined-mode ebook phase).
  // 1. A source-backed result already names its release source.
  if (book.source) return book.source;

  // 2. Legacy direct-mode / source-backed browse: provider IS a release source.
  //    Return the normalized name we actually validated, not the raw string.
  const provider = normalizeSourceName(book.provider);
  if (provider && KNOWN_RELEASE_SOURCES.has(provider)) return provider;

  // 3. Metadata-shaped book (provider = hardcover/audible/…). There is no
  //    release source to name — see the doc comment on the throwing variant.
  return null;
}

/**
 * Resolve the release-source name for a download payload, content-type-aware.
 * Throws when no release source can be resolved.
 *
 * **Payload/download paths only.** For render/policy paths use
 * `resolveReleaseSourceOrNull` — a throw during render kills the app (no
 * ErrorBoundary exists).
 *
 * Background: the legacy resolver returned `book.source || book.provider`.
 * In direct-mode-ebook flows this happens to work — `book.provider` is
 * `direct_download`, which is also a registered release-source name (the
 * backend sets `BookMetadata.provider = record.source` for browse records).
 * In metadata-driven flows that identity is gone: the provider is Hardcover /
 * Audible / combined_audiobook / OpenLibrary / Google Books, none of which are
 * release sources, and the backend rejects the download with
 * `Unknown release source: <provider>`.
 *
 * Why there is deliberately NO `DEFAULT_RELEASE_SOURCE` fallback for
 * metadata-shaped books: the direct payload builders pair the resolved source
 * with `source_id: book.id`, and for a metadata book `book.id` is
 * `${provider}:${provider_id}` (e.g. `hardcover:12345`, see
 * `transformMetadataToBook`). Substituting a configured source would produce a
 * well-formed but unfulfillable task — `direct_download` reads `task.task_id`
 * as an Anna's-Archive MD5 (`direct_download.py`, `_cascade_*`) — that is
 * accepted, queued, possibly admin-approved, and only THEN fails. Failing here
 * is strictly better. Metadata books must go through the release path
 * (`/api/releases` + ReleaseModal → `buildReleaseDataFromMetadataRelease`),
 * which resolves a concrete release id; that is what the universal-mode UI
 * already does (`BookActionButton` renders `BookGetButton`, not a download
 * button, when the search-mode context is `universal`).
 *
 * @param book - Book record from search results
 * @param contentType - Effective content type at request time
 * @param defaultAudiobookSource - DEFAULT_RELEASE_SOURCE_AUDIOBOOK from config
 */
export function getReleaseSourceForContentType(
  book: Book,
  contentType: ContentType,
  defaultAudiobookSource: string | null | undefined,
): string {
  const resolved = resolveReleaseSourceOrNull(book, contentType, defaultAudiobookSource);
  if (resolved) return resolved;

  if (contentType === 'audiobook') {
    // No default configured — let the caller see a real error instead of
    // silently falling back to book.provider (which would re-introduce the
    // "Unknown release source: audible" bug).
    throw new Error(
      `Book ${book.id}: audiobook content_type but no default_release_source_audiobook configured`,
    );
  }

  if (book.provider) {
    throw new Error(
      `Book ${book.id}: "${book.provider}" is a metadata provider, not a release source — ` +
        'metadata results must be downloaded through the release path, not the direct payload path',
    );
  }
  throw new Error(`Book ${book.id} is missing source context`);
}
