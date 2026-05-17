"""Combined audiobook discovery: Hardcover + Audible in parallel, deduplicated.

Block-A empirie:
- Hardcover handles English mainstream well (HP: 248 audio editions, Sanderson:
  64, etc.) but is patchy for German (Fitzek/Perry Rhodan: 0 audio editions).
- Audible Direct fills the German gap and covers English equally well (14/15
  test queries).

This provider fans the search out to both, then merges by ASIN (when both
providers know the ASIN) or by work-hash (sha1 of normalized title + first
author). Audible wins ties because its records carry ASIN + narrators +
runtime natively, which feeds ABB release lookup cleanly.

Each returned BookMetadata keeps its native `provider` field ("audible" or
"hardcover"), so downstream detail/release lookups route to the right inner
provider without needing a prefix.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, ClassVar

from shelfmark.core.logger import setup_logger
from shelfmark.core.settings_registry import (
    CheckboxField,
    HeadingField,
    SettingsField,
    register_settings,
)
from shelfmark.metadata_providers import (
    BookMetadata,
    MetadataProvider,
    MetadataSearchOptions,
    SearchResult,
    SortOrder,
    get_provider,
    get_provider_kwargs,
    is_provider_enabled,
    is_provider_registered,
    register_provider,
    register_provider_kwargs,
)
from shelfmark.metadata_providers.audible import _normalize_text, work_hash

logger = setup_logger(__name__)


def _instantiate(name: str) -> MetadataProvider | None:
    """Best-effort instantiation of a registered sub-provider."""
    if not is_provider_registered(name):
        return None
    try:
        kwargs = get_provider_kwargs(name)
        return get_provider(name, **kwargs)
    except Exception:  # noqa: BLE001 - we degrade gracefully
        logger.exception("CombinedAudiobook: failed to instantiate %s", name)
        return None


def _safe_paginated(
    provider: MetadataProvider,
    audio_options: MetadataSearchOptions,
) -> list[BookMetadata]:
    try:
        return provider.search_paginated(audio_options).books
    except Exception:  # noqa: BLE001 - one sub-provider failure must not kill the other
        logger.exception(
            "CombinedAudiobook: sub-provider %s search crashed", provider.name
        )
        return []


def _first_author(book: BookMetadata) -> str | None:
    return book.authors[0] if book.authors else None


@register_provider("combined_audiobook")
class CombinedAudiobookProvider(MetadataProvider):
    """Fan-out audiobook provider: Hardcover + Audible, deduplicated."""

    name = "combined_audiobook"
    display_name = "Combined audiobook (Hardcover + Audible)"
    requires_auth = True  # Inherits Hardcover's auth requirement
    supported_sorts: ClassVar[tuple[SortOrder, ...]] = (SortOrder.RELEVANCE,)

    SUB_PROVIDERS: ClassVar[tuple[str, ...]] = ("audible", "hardcover")

    def __init__(self) -> None:
        # Sub-providers are resolved lazily per call so that config refreshes
        # (e.g. Hardcover API key change) take effect immediately.
        pass

    def _resolve_sub_providers(self) -> list[MetadataProvider]:
        providers: list[MetadataProvider] = []
        for name in self.SUB_PROVIDERS:
            if not is_provider_enabled(name):
                continue
            provider = _instantiate(name)
            if provider and provider.is_available():
                providers.append(provider)
        return providers

    def is_available(self) -> bool:
        return bool(self._resolve_sub_providers())

    def search(self, options: MetadataSearchOptions) -> list[BookMetadata]:
        return self.search_paginated(options).books

    def search_paginated(self, options: MetadataSearchOptions) -> SearchResult:
        # Always force audiobook content_type into the sub-calls so Hardcover
        # branches into its 2-step audio path even if caller forgot to set it.
        audio_options = MetadataSearchOptions(
            query=options.query,
            search_type=options.search_type,
            language=options.language,
            sort=options.sort,
            limit=options.limit,
            page=options.page,
            fields=dict(options.fields or {}),
            content_type="audiobook",
            book_languages=options.book_languages,
        )

        sub_providers = self._resolve_sub_providers()
        if not sub_providers:
            return SearchResult(books=[], page=options.page, total_found=0, has_more=False)

        # Parallel fan-out (kept short by sub-provider timeouts).
        results_by_provider: dict[str, list[BookMetadata]] = {}
        with ThreadPoolExecutor(max_workers=len(sub_providers)) as pool:
            futures = {
                pool.submit(_safe_paginated, provider, audio_options): provider.name
                for provider in sub_providers
            }
            for fut in as_completed(futures):
                provider_name = futures[fut]
                try:
                    books = fut.result()
                except Exception:  # noqa: BLE001 - guarded by _safe_paginated
                    books = []
                results_by_provider[provider_name] = books

        # Dedup: ASIN first, then work-hash. Audible wins ties.
        by_asin: dict[str, BookMetadata] = {}
        by_work_hash: dict[str, BookMetadata] = {}
        ordered_keys: list[tuple[str, str]] = []  # (bucket, key) preserves insertion order

        priority_order = ["audible", "hardcover"]
        for provider_name in priority_order:
            books = results_by_provider.get(provider_name, [])
            for book in books:
                asin = _asin_for_book(book)
                if asin:
                    if asin in by_asin:
                        continue
                    by_asin[asin] = book
                    ordered_keys.append(("asin", asin))
                    continue
                key = work_hash(book.title, _first_author(book))
                if key in by_work_hash or any(
                    _normalize_text(book.title) == _normalize_text(existing.title)
                    and (_first_author(book) or "") == (_first_author(existing) or "")
                    for existing in by_asin.values()
                ):
                    continue
                by_work_hash[key] = book
                ordered_keys.append(("hash", key))

        merged: list[BookMetadata] = []
        for bucket, key in ordered_keys:
            book = by_asin.get(key) if bucket == "asin" else by_work_hash.get(key)
            if book:
                merged.append(book)

        # Order by user's preferred languages, then by source priority.
        preferred = [
            (lang or "").strip().lower() for lang in (options.book_languages or [])
        ]

        def rank(book: BookMetadata) -> tuple[int, int]:
            lang = (book.language or "").lower()
            try:
                lang_idx = preferred.index(lang) if preferred else 0
            except ValueError:
                lang_idx = len(preferred) if preferred else 0
            src_idx = 0 if book.provider == "audible" else 1
            return (lang_idx, src_idx)

        merged.sort(key=rank)
        merged = merged[: options.limit]

        return SearchResult(
            books=merged,
            page=options.page,
            total_found=len(merged),
            has_more=False,
        )

    def get_book(self, book_id: str) -> BookMetadata | None:
        # Route by ID shape: ASIN-style (10-char alphanumeric starting with B,
        # or pure ISBN-10) → Audible. Otherwise → Hardcover.
        target = "audible" if _looks_like_asin(book_id) else "hardcover"
        provider = _instantiate(target)
        if not provider or not provider.is_available():
            # Fall back to the other side
            other = "hardcover" if target == "audible" else "audible"
            provider = _instantiate(other)
            if not provider or not provider.is_available():
                return None
        return provider.get_book(book_id)

    def search_by_isbn(self, isbn: str) -> BookMetadata | None:
        audible = _instantiate("audible")
        if audible and audible.is_available():
            hit = audible.search_by_isbn(isbn)
            if hit:
                return hit
        hardcover = _instantiate("hardcover")
        if hardcover and hardcover.is_available():
            return hardcover.search_by_isbn(isbn)
        return None


def _asin_for_book(book: BookMetadata) -> str | None:
    """Extract ASIN from a sub-provider's BookMetadata.

    Audible: provider_id is the ASIN.
    Hardcover: ASIN may be hidden in display_fields or source_url, but we don't
    rely on it for dedup (work-hash covers Hardcover-only books). Return None.
    """
    if book.provider == "audible":
        return (book.provider_id or "").strip() or None
    return None


def _looks_like_asin(book_id: str) -> bool:
    value = (book_id or "").strip()
    if len(value) != 10:
        return False
    if value.startswith(("B0", "B1")):
        return True
    # ISBN-10 (numeric or with trailing X) also routes to Audible per its
    # search_by_isbn fallback.
    return value[:-1].isdigit() and (value[-1].isdigit() or value[-1].upper() == "X")


def _combined_kwargs() -> dict[str, Any]:
    return {}


register_provider_kwargs("combined_audiobook")(_combined_kwargs)


@register_settings(
    "combined_audiobook",
    "Combined audiobook",
    icon="layers",
    order=53,
    group="metadata_providers",
)
def combined_audiobook_settings() -> list[SettingsField]:
    """Settings UI for the combined Hardcover + Audible audiobook provider."""
    return [
        HeadingField(
            key="combined_audiobook_heading",
            title="Combined audiobook (Hardcover + Audible)",
            description=(
                "Fan-out provider that queries Hardcover and Audible in parallel "
                "and merges results by ASIN (preferred) or work-hash. Requires "
                "both Hardcover and Audible to be enabled for full coverage; "
                "degrades gracefully when one sub-provider is disabled or fails."
            ),
        ),
        CheckboxField(
            key="COMBINED_AUDIOBOOK_ENABLED",
            label="Enable combined audiobook provider",
            description=(
                "Use the Combined audiobook (Hardcover + Audible) provider for "
                "audiobook discovery. Select it via the audiobook provider "
                "dropdown after enabling."
            ),
            default=True,
        ),
    ]
