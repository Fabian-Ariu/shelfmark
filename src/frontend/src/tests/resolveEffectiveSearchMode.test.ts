import { describe, expect, it } from 'vitest';

import { resolveEffectiveSearchMode } from '../utils/resolveEffectiveSearchMode';

describe('resolveEffectiveSearchMode', () => {
  it('forces universal when content_type is audiobook, even if requested is direct', () => {
    expect(resolveEffectiveSearchMode('direct', 'audiobook')).toBe('universal');
  });

  it('keeps universal for audiobook when requested is already universal', () => {
    expect(resolveEffectiveSearchMode('universal', 'audiobook')).toBe('universal');
  });

  it('passes direct through for ebook', () => {
    expect(resolveEffectiveSearchMode('direct', 'ebook')).toBe('direct');
  });

  it('passes universal through for ebook', () => {
    expect(resolveEffectiveSearchMode('universal', 'ebook')).toBe('universal');
  });
});
