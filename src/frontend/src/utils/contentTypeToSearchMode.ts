import type { ContentType, SearchMode } from '../types';

/**
 * Maps a content-type toggle choice to the effective search mode.
 *
 * - Audiobooks and combined searches need universal mode (metadata providers
 *   route to audiobook-capable sources like AudioBookBay/Prowlarr).
 * - Plain ebook search uses direct mode for broader full-text coverage.
 *
 * Pure function; side effects (state updates, persistence) happen in callers.
 */
export function contentTypeToSearchMode(
  contentType: ContentType,
  combinedMode: boolean,
): SearchMode {
  if (combinedMode) return 'universal';
  return contentType === 'audiobook' ? 'universal' : 'direct';
}
