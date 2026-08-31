import { describe, expect, it } from 'vitest';

import type { Book } from '../types';
import { resolveEffectiveSearchMode } from '../utils/resolveEffectiveSearchMode';
import { shouldUseReleaseFlow } from '../utils/shouldUseReleaseFlow';

const audibleHit: Book = {
  // transformMetadataToBook builds id as `${provider}:${provider_id}` and sets no source
  id: 'audible:B00NWCPRBU',
  title: 'Project Hail Mary',
  author: 'Andy Weir',
  content_type: 'audiobook',
  provider: 'audible',
  provider_id: 'B00NWCPRBU',
};

const directHit: Book = {
  // transformSourceBackedDataToBook sets provider === source
  id: 'd41d8cd98f00b204e9800998ecf8427e',
  title: 'Der Marsianer',
  author: 'Andy Weir',
  content_type: 'ebook',
  source: 'direct_download',
  provider: 'direct_download',
  provider_id: 'd41d8cd98f00b204e9800998ecf8427e',
};

describe('shouldUseReleaseFlow', () => {
  it('routes an audiobook metadata hit to the release flow even in direct mode', () => {
    expect(shouldUseReleaseFlow('direct', audibleHit)).toBe(true);
  });

  it('routes every result to the release flow in universal mode', () => {
    expect(shouldUseReleaseFlow('universal', directHit)).toBe(true);
  });

  it('keeps a source-backed direct result on the direct download action', () => {
    expect(shouldUseReleaseFlow('direct', directHit)).toBe(false);
  });

  it('keeps a direct result without provider fields on the direct download action', () => {
    const bare: Book = { id: 'abc', title: 'Bare', author: 'Nobody', source: 'direct_download' };
    expect(shouldUseReleaseFlow('direct', bare)).toBe(false);
  });

  it('agrees with the content-type-aware mode used by the card context', () => {
    // App.tsx feeds SearchModeContext with resolveEffectiveSearchMode(persisted, contentType);
    // an audiobook search therefore never renders the direct download button.
    const contextMode = resolveEffectiveSearchMode('direct', 'audiobook');
    expect(shouldUseReleaseFlow(contextMode, audibleHit)).toBe(true);
  });
});
