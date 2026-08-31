import type { Book, ContentType, CreateRequestPayload, Release } from '../types';
import { getReleaseSourceForContentType } from './getReleaseSourceForContentType';

export const toContentType = (value: string): ContentType => {
  return value.trim().toLowerCase() === 'audiobook' ? 'audiobook' : 'ebook';
};

/**
 * Legacy source resolver. Returns book.source || book.provider.
 *
 * **Use only for ebook/direct-mode flows.** For audiobook flows the resolved
 * `book.provider` (e.g. `audible`, `hardcover`, `combined_audiobook`) is NOT
 * a registered release-source name and the backend will reject the download
 * with `Unknown release source: audible`. Use
 * `getReleaseSourceForContentType(book, contentType, defaultAudiobookSource)`
 * instead — it honors DEFAULT_RELEASE_SOURCE_AUDIOBOOK.
 */
export const getBrowseSource = (book: Book): string => {
  const source = book.source || book.provider;
  if (source) {
    return source;
  }
  throw new Error(`Book ${book.id} is missing source context`);
};

export const isSourceBackedRequestPayload = (
  payload: CreateRequestPayload | null | undefined,
): boolean => {
  if (!payload) {
    return false;
  }

  const provider =
    typeof payload.book_data?.provider === 'string' ? payload.book_data.provider.trim() : '';
  const source = typeof payload.context?.source === 'string' ? payload.context.source.trim() : '';

  return Boolean(provider) && Boolean(source) && source !== '*' && provider === source;
};

export const buildMetadataBookRequestData = (book: Book, contentType: ContentType) => {
  return {
    title: book.title || 'Unknown title',
    author: book.author || 'Unknown author',
    content_type: contentType,
    provider: book.provider || 'metadata',
    provider_id: book.provider_id || book.id,
    year: book.year,
    preview: book.preview,
    series_name: book.series_name,
    series_position: book.series_position,
    series_count: book.series_count,
    subtitle: book.subtitle,
    source_url: book.source_url,
  };
};

const buildDirectBookRequestData = (
  book: Book,
  contentType: ContentType,
  defaultAudiobookSource: string | null | undefined,
) => {
  const source = getReleaseSourceForContentType(book, contentType, defaultAudiobookSource);
  return {
    title: book.title || 'Unknown title',
    author: book.author || 'Unknown author',
    content_type: contentType,
    provider: source,
    provider_id: book.provider_id || book.id,
    year: book.year,
    format: book.format,
    size: book.size,
    preview: book.preview,
    source,
    source_url: book.source_url,
  };
};

export const buildReleaseDataFromMetadataRelease = (
  book: Book,
  release: Release,
  contentType: ContentType,
) => {
  const isSourceBackedReleaseContext =
    Boolean(book.provider) &&
    book.provider === release.source &&
    (!book.source || book.source === release.source);

  return {
    source: release.source,
    source_id: release.source_id,
    title: book.title || release.title || 'Unknown title',
    author: book.author,
    year: book.year,
    format: release.format,
    size: release.size,
    size_bytes: release.size_bytes,
    download_url: release.download_url,
    protocol: release.protocol,
    indexer: release.indexer,
    seeders: release.seeders,
    extra: release.extra,
    preview: book.preview,
    content_type: contentType,
    series_name: book.series_name,
    series_position: book.series_position,
    series_count: book.series_count,
    subtitle: book.subtitle,
    ...(isSourceBackedReleaseContext ? { search_mode: 'direct' as const } : {}),
  };
};

/**
 * Build the release_data payload for a direct (card-level) download.
 *
 * @param book - Book record (search result)
 * @param contentType - Effective content type at the time of the click.
 *   Defaults to 'ebook' because the historical direct-mode flow was eBook-
 *   only. Callers in audiobook flows MUST pass 'audiobook' explicitly,
 *   otherwise the backend rejects the download with
 *   `Unknown release source: audible`.
 * @param defaultAudiobookSource - DEFAULT_RELEASE_SOURCE_AUDIOBOOK from
 *   config. Required when contentType is 'audiobook'.
 *
 * **Source-backed books only.** `source_id` is `book.id`, which is a concrete
 * release id for source-backed results but `${provider}:${provider_id}` for a
 * metadata book (`transformMetadataToBook`). Metadata books therefore have no
 * business here — `getReleaseSourceForContentType` throws for them on purpose.
 * Their download path is `/api/releases` + ReleaseModal
 * (`buildReleaseDataFromMetadataRelease`).
 */
export const buildReleaseDataFromDirectBook = (
  book: Book,
  contentType: ContentType = 'ebook',
  defaultAudiobookSource: string | null | undefined = null,
) => {
  const source = getReleaseSourceForContentType(book, contentType, defaultAudiobookSource);
  return {
    source,
    source_id: book.id,
    title: book.title || 'Unknown title',
    author: book.author,
    year: book.year,
    format: book.format,
    size: book.size,
    preview: book.preview,
    content_type: contentType,
    search_mode: 'direct' as const,
  };
};

/**
 * Build a CreateRequestPayload for direct-mode actions.
 *
 * @param book - Book record
 * @param contentType - See buildReleaseDataFromDirectBook. Default 'ebook'.
 * @param defaultAudiobookSource - DEFAULT_RELEASE_SOURCE_AUDIOBOOK from config.
 */
export const buildDirectRequestPayload = (
  book: Book,
  contentType: ContentType = 'ebook',
  defaultAudiobookSource: string | null | undefined = null,
): CreateRequestPayload => {
  const bookData = buildDirectBookRequestData(book, contentType, defaultAudiobookSource);
  const source = getReleaseSourceForContentType(book, contentType, defaultAudiobookSource);

  // In direct mode, every result already represents a concrete downloadable release.
  // Keep request payloads release-level so admins can approve immediately while still
  // allowing alternate release selection from the same direct record.
  return {
    book_data: bookData,
    release_data: buildReleaseDataFromDirectBook(book, contentType, defaultAudiobookSource),
    context: {
      source,
      content_type: contentType,
      request_level: 'release',
    },
  };
};

export const getRequestSuccessMessage = (payload: CreateRequestPayload): string => {
  const bookData = payload.book_data || {};
  const releaseData = payload.release_data || {};
  const title =
    (typeof bookData.title === 'string' && bookData.title.trim()) ||
    (typeof releaseData.title === 'string' && releaseData.title.trim()) ||
    'Untitled';
  return `Request submitted: ${title}`;
};
