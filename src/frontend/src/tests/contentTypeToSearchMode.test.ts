import { describe, expect, it } from 'vitest';

import { contentTypeToSearchMode } from '../utils/contentTypeToSearchMode';

describe('contentTypeToSearchMode', () => {
  it('maps ebook + combined=false to direct', () => {
    expect(contentTypeToSearchMode('ebook', false)).toBe('direct');
  });

  it('maps audiobook + combined=false to universal', () => {
    expect(contentTypeToSearchMode('audiobook', false)).toBe('universal');
  });

  it('maps ebook + combined=true to universal (combined wins)', () => {
    expect(contentTypeToSearchMode('ebook', true)).toBe('universal');
  });

  it('maps audiobook + combined=true to universal', () => {
    expect(contentTypeToSearchMode('audiobook', true)).toBe('universal');
  });
});
