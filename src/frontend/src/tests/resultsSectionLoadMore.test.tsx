/**
 * The Load Next Page button as the user actually gets it.
 *
 * The helper tests around it are pure functions; this one renders the real component
 * so that the wiring itself is covered: putting the old `searchMode === 'universal'`
 * gate back on the Load More block makes this file fail, which is precisely the
 * mutation that used to slip through the whole suite unnoticed.
 */
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';

import { ResultsSection } from '../components/ResultsSection';
import { SearchModeProvider } from '../contexts/SearchModeContext';
import type { Book, ButtonStateInfo, SearchMode } from '../types';

const buttonState = (): ButtonStateInfo => ({ text: 'Download', state: 'download' });

const books: Book[] = [
  { id: 'md5-1', title: 'Sapiens', author: 'Harari', format: 'epub' },
  { id: 'md5-2', title: 'Homo Deus', author: 'Harari', format: 'epub' },
];

const render = (
  searchMode: SearchMode,
  overrides: { hasMore?: boolean; isLoadingMore?: boolean; onLoadMore?: () => void } = {},
): string =>
  renderToStaticMarkup(
    <SearchModeProvider searchMode={searchMode}>
      <ResultsSection
        books={books}
        visible
        onDetails={async () => undefined}
        onDownload={async () => undefined}
        onGetReleases={async () => undefined}
        getButtonState={buttonState}
        getUniversalButtonState={buttonState}
        sortValue=""
        onSortChange={() => undefined}
        hasMore={overrides.hasMore ?? true}
        isLoadingMore={overrides.isLoadingMore ?? false}
        onLoadMore={'onLoadMore' in overrides ? overrides.onLoadMore : () => undefined}
      />
    </SearchModeProvider>,
  );

describe('ResultsSection load-more block', () => {
  it('offers the next Anna’s Archive page in direct mode', () => {
    const html = render('direct');

    expect(html).toContain('Load Next Page');
    expect(html).toContain('each page takes ~45s');
  });

  it('still offers Load More in universal mode', () => {
    const html = render('universal');

    expect(html).toContain('Load More');
    expect(html).not.toContain('Load Next Page');
  });

  it('says what the wait costs while a direct page is loading', () => {
    const html = render('direct', { isLoadingMore: true });

    expect(html).toContain('Loading next page (~45s)...');
    expect(html).toContain('disabled');
  });

  it('hides the button once the backend reported the last page', () => {
    expect(render('direct', { hasMore: false })).not.toContain('Load Next Page');
  });

  it('hides the button when no handler is wired up', () => {
    expect(render('direct', { onLoadMore: undefined })).not.toContain('Load Next Page');
  });
});
