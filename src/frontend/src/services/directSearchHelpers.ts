/**
 * Build the query part of a direct-mode (Anna's Archive) release search.
 *
 * `query` is the already-built search query string (buildSearchQuery output). The page
 * is appended separately and never folded into it: it is a cursor, not a filter, and
 * must stay out of the persisted search state and out of deep links.
 */
export const buildDirectSearchQuery = (query: string, page: number): string => {
  const safePage = Number.isFinite(page) ? Math.max(1, Math.trunc(page)) : 1;
  return safePage > 1 ? `${query}&page=${safePage}` : query;
};
