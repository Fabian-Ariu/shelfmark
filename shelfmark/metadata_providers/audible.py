"""Audible-Direct metadata provider.

Calls Audible's public catalog API (`api.audible.{de,com,...}`) for audiobook
metadata. No authentication required. Multiple regions are queried in parallel
and results are merged + deduplicated by ASIN.

The Audible Catalog API is officially intended for Audible's own apps, not
for third-party use, but it has been used by community projects (Audnexus,
AudiMeta) for years. If Audible ever restricts it, fall back to Hardcover.
"""

from __future__ import annotations

import re
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, ClassVar

import requests

from shelfmark.core.config import config as app_config
from shelfmark.core.logger import setup_logger
from shelfmark.metadata_providers import (
    BookMetadata,
    DisplayField,
    MetadataProvider,
    MetadataSearchOptions,
    SearchResult,
    SortOrder,
    register_provider,
    register_provider_kwargs,
)

logger = setup_logger(__name__)

# Allowed Audible regions and their TLD mapping. Public API endpoints all
# follow `https://api.audible.<tld>/1.0/catalog/products`. We support DE+COM
# by default; other regions can be enabled via AUDIBLE_REGIONS.
_REGION_TLD: dict[str, str] = {
    "de": "de",
    "com": "com",
    "us": "com",  # alias
    "uk": "co.uk",
    "co.uk": "co.uk",
    "fr": "fr",
    "it": "it",
    "es": "es",
    "ca": "ca",
    "au": "com.au",
    "jp": "co.jp",
}

# Audible "language" field uses English lowercase names (e.g. "german",
# "english"). Map to ISO 639-1 codes so the rest of Shelfmark stays uniform.
_LANG_TO_ISO: dict[str, str] = {
    "english": "en",
    "german": "de",
    "deutsch": "de",
    "french": "fr",
    "italian": "it",
    "spanish": "es",
    "portuguese": "pt",
    "japanese": "ja",
    "dutch": "nl",
    "swedish": "sv",
    "norwegian": "no",
    "danish": "da",
    "finnish": "fi",
    "polish": "pl",
    "russian": "ru",
}

# Response groups required to populate BookMetadata. Keep this minimal to keep
# Audible response payloads small.
_RESPONSE_GROUPS = "product_desc,product_attrs,contributors,media"

# Default number of regions x results to merge. With 2 regions and 10 per
# region we get up to 20 raw results, dedupe to ~10-15 unique works.
_DEFAULT_NUM_PER_REGION = 10
_HTTP_TIMEOUT_S = 10.0
_USER_AGENT = (
    "Shelfmark/1.0 (audiobook metadata; +https://github.com/calibrain/shelfmark)"
)


def _coerce_regions(raw: object) -> list[str]:
    """Parse AUDIBLE_REGIONS config into normalized region keys."""
    if not raw:
        return ["de", "com"]
    if isinstance(raw, (list, tuple)):
        items = [str(x) for x in raw]
    else:
        items = [s for s in str(raw).split(",")]
    out: list[str] = []
    for item in items:
        key = item.strip().lower()
        if key and key in _REGION_TLD and key not in out:
            out.append(key)
    return out or ["de", "com"]


def _build_url(region: str) -> str:
    """Return the catalog endpoint for a given region key."""
    tld = _REGION_TLD.get(region.lower(), "com")
    return f"https://api.audible.{tld}/1.0/catalog/products"


def _normalize_text(value: str) -> str:
    """Lowercase, strip diacritics, collapse non-alphanumerics. Used for dedup."""
    if not value:
        return ""
    nfkd = unicodedata.normalize("NFKD", value)
    ascii_only = "".join(c for c in nfkd if not unicodedata.combining(c))
    ascii_only = ascii_only.lower().replace("ß", "ss")
    return re.sub(r"[^a-z0-9]+", " ", ascii_only).strip()


def work_hash(title: str, first_author: str | None) -> str:
    """Stable work-level identifier for cross-provider deduplication."""
    import hashlib

    base = _normalize_text(title or "")
    if first_author:
        base = f"{base}|{_normalize_text(first_author)}"
    return hashlib.sha1(base.encode("utf-8"), usedforsecurity=False).hexdigest()


def _extract_contributor_names(items: list[dict[str, Any]] | None) -> list[str]:
    if not items:
        return []
    names: list[str] = []
    for item in items:
        name = (item.get("name") or "").strip()
        if name and name not in names:
            names.append(name)
    return names


def _format_runtime(minutes: int | float | None) -> str | None:
    if not minutes:
        return None
    try:
        total = int(minutes)
    except (TypeError, ValueError):
        return None
    if total <= 0:
        return None
    hours, mins = divmod(total, 60)
    if hours and mins:
        return f"{hours}h {mins}m"
    if hours:
        return f"{hours}h"
    return f"{mins}m"


def _pick_cover_url(product_images: dict[str, Any] | None) -> str | None:
    if not isinstance(product_images, dict):
        return None
    # Prefer 500/720 over smaller thumbnails. Audible returns string keys.
    for key in ("500", "720", "300", "1024"):
        url = product_images.get(key)
        if url:
            return url
    # Fallback: first available
    for url in product_images.values():
        if url:
            return url
    return None


def _audible_to_book(product: dict[str, Any], region: str) -> BookMetadata | None:
    """Map an Audible catalog product to BookMetadata."""
    asin = (product.get("asin") or "").strip()
    title = (product.get("title") or "").strip()
    if not asin or not title:
        return None

    authors = _extract_contributor_names(product.get("authors"))
    narrators = _extract_contributor_names(product.get("narrators"))
    audible_lang = (product.get("language") or "").strip().lower()
    iso_lang = _LANG_TO_ISO.get(audible_lang)

    runtime_min = product.get("runtime_length_min")
    runtime_label = _format_runtime(runtime_min)

    display_fields: list[DisplayField] = []
    if runtime_label:
        display_fields.append(
            DisplayField(label="Runtime", value=runtime_label, icon="book")
        )
    if narrators:
        narrator_str = ", ".join(narrators[:3])
        if len(narrators) > 3:
            narrator_str += f" + {len(narrators) - 3}"
        display_fields.append(DisplayField(label="Narrator", value=narrator_str))

    publish_year: int | None = None
    raw_date = product.get("release_date") or product.get("issue_date") or ""
    if isinstance(raw_date, str) and len(raw_date) >= 4 and raw_date[:4].isdigit():
        publish_year = int(raw_date[:4])

    subtitle = (product.get("subtitle") or "").strip() or None

    source_url = f"https://www.audible.{_REGION_TLD.get(region, 'com')}/pd/{asin}"

    return BookMetadata(
        provider="audible",
        provider_display_name="Audible",
        provider_id=asin,
        title=title,
        subtitle=subtitle,
        authors=authors,
        cover_url=_pick_cover_url(product.get("product_images")),
        publisher=(product.get("publisher_name") or None),
        publish_year=publish_year,
        language=iso_lang,
        source_url=source_url,
        display_fields=display_fields,
    )


def _fetch_region(
    region: str,
    query: str,
    *,
    num_results: int,
    timeout: float,
) -> list[BookMetadata]:
    url = _build_url(region)
    params = {
        "keywords": query,
        "num_results": str(num_results),
        "response_groups": _RESPONSE_GROUPS,
    }
    try:
        resp = requests.get(
            url,
            params=params,
            headers={"User-Agent": _USER_AGENT, "Accept": "application/json"},
            timeout=timeout,
        )
    except requests.RequestException as exc:
        logger.warning("Audible.%s request failed: %s", region, exc)
        return []
    if resp.status_code != 200:
        logger.warning("Audible.%s HTTP %s for '%s'", region, resp.status_code, query)
        return []
    try:
        payload = resp.json()
    except ValueError:
        logger.warning("Audible.%s non-JSON response for '%s'", region, query)
        return []
    products = payload.get("products") or []
    books: list[BookMetadata] = []
    for product in products:
        book = _audible_to_book(product, region)
        if book:
            books.append(book)
    return books


def _audible_kwargs() -> dict[str, Any]:
    """Provider kwargs factory: just region config, no auth."""
    return {
        "regions": _coerce_regions(app_config.get("AUDIBLE_REGIONS", "de,com")),
    }


@register_provider("audible")
class AudibleProvider(MetadataProvider):
    """Audible Catalog API metadata provider (no auth)."""

    name = "audible"
    display_name = "Audible"
    requires_auth = False
    supported_sorts: ClassVar[tuple[SortOrder, ...]] = (SortOrder.RELEVANCE,)

    def __init__(self, regions: list[str] | None = None) -> None:
        self.regions = _coerce_regions(regions) if regions is not None else ["de", "com"]

    def is_available(self) -> bool:
        return True

    def search(self, options: MetadataSearchOptions) -> list[BookMetadata]:
        return self.search_paginated(options).books

    def search_paginated(self, options: MetadataSearchOptions) -> SearchResult:
        query = (options.query or "").strip()
        if not query:
            return SearchResult(books=[], page=options.page, total_found=0, has_more=False)

        per_region = min(max(options.limit // max(len(self.regions), 1), 5), 25)
        merged: dict[str, BookMetadata] = {}

        with ThreadPoolExecutor(max_workers=max(len(self.regions), 1)) as pool:
            futures = {
                pool.submit(
                    _fetch_region,
                    region,
                    query,
                    num_results=per_region,
                    timeout=_HTTP_TIMEOUT_S,
                ): region
                for region in self.regions
            }
            for fut in as_completed(futures):
                region = futures[fut]
                try:
                    books = fut.result()
                except Exception:  # noqa: BLE001 - log + continue per region
                    logger.exception("Audible.%s search crashed", region)
                    continue
                for book in books:
                    if book.provider_id in merged:
                        continue
                    merged[book.provider_id] = book

        # Order by language preference (user's BOOK_LANGUAGE wins), then keep
        # the original Audible relevance ordering within each language bucket.
        preferred = [
            (lang or "").strip().lower() for lang in (options.book_languages or [])
        ]
        preferred = [lang for lang in preferred if lang]

        def rank(book: BookMetadata) -> tuple[int, int]:
            lang = (book.language or "").lower()
            try:
                idx = preferred.index(lang) if preferred else 0
            except ValueError:
                idx = len(preferred) if preferred else 0
            return (idx, 0)

        books = list(merged.values())
        books.sort(key=rank)
        books = books[: options.limit]
        return SearchResult(
            books=books,
            page=options.page,
            total_found=len(merged),
            has_more=False,
        )

    def get_book(self, book_id: str) -> BookMetadata | None:
        asin = (book_id or "").strip()
        if not asin:
            return None
        for region in self.regions:
            url = f"{_build_url(region)}/{asin}"
            try:
                resp = requests.get(
                    url,
                    params={"response_groups": _RESPONSE_GROUPS},
                    headers={
                        "User-Agent": _USER_AGENT,
                        "Accept": "application/json",
                    },
                    timeout=_HTTP_TIMEOUT_S,
                )
            except requests.RequestException as exc:
                logger.warning("Audible.%s get_book %s failed: %s", region, asin, exc)
                continue
            if resp.status_code != 200:
                continue
            try:
                payload = resp.json()
            except ValueError:
                continue
            product = payload.get("product") or {}
            if product:
                book = _audible_to_book(product, region)
                if book:
                    return book
        return None

    def search_by_isbn(self, isbn: str) -> BookMetadata | None:
        # Audible search accepts ISBN as keyword; pick best match if any.
        result = self.search_paginated(
            MetadataSearchOptions(query=isbn, limit=5)
        )
        return result.books[0] if result.books else None


register_provider_kwargs("audible")(_audible_kwargs)
