"""Unit tests for the Audible-Direct metadata provider."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from shelfmark.metadata_providers import MetadataSearchOptions
from shelfmark.metadata_providers.audible import (
    AudibleProvider,
    _audible_to_book,
    _coerce_regions,
    _format_runtime,
    _normalize_text,
    _pick_cover_url,
    work_hash,
)


class TestRegionCoercion:
    def test_default_when_empty(self):
        assert _coerce_regions(None) == ["de", "com"]
        assert _coerce_regions("") == ["de", "com"]

    def test_string_csv(self):
        assert _coerce_regions("de, com, uk") == ["de", "com", "uk"]

    def test_drops_unknown(self):
        # "xx" is not in _REGION_TLD so it is dropped
        assert _coerce_regions("de, xx, com") == ["de", "com"]

    def test_falls_back_on_empty_result(self):
        assert _coerce_regions("xx, yy") == ["de", "com"]

    def test_dedupes(self):
        assert _coerce_regions("de, de, com, com") == ["de", "com"]


class TestNormalizeText:
    def test_diacritics_collapsed(self):
        assert _normalize_text("Übermäßig schön") == "ubermassig schon"

    def test_punctuation_collapses(self):
        assert _normalize_text("Die drei ???: Karpatenhund") == "die drei karpatenhund"

    def test_empty_input(self):
        assert _normalize_text("") == ""


class TestWorkHash:
    def test_same_normalization_same_hash(self):
        assert work_hash("Die Therapie", "Sebastian Fitzek") == work_hash(
            "  Die  Therapie!  ", "Sebastian Fitzek"
        )

    def test_diacritics_ignored(self):
        assert work_hash("Übermäßig", "Autor") == work_hash("Ubermassig", "Autor")

    def test_different_authors_different_hash(self):
        assert work_hash("Die Therapie", "Autor A") != work_hash(
            "Die Therapie", "Autor B"
        )

    def test_missing_author_still_stable(self):
        h1 = work_hash("Die Therapie", None)
        h2 = work_hash("Die Therapie", None)
        assert h1 == h2


class TestFormatRuntime:
    def test_zero_or_none(self):
        assert _format_runtime(None) is None
        assert _format_runtime(0) is None

    def test_minutes_only(self):
        assert _format_runtime(45) == "45m"

    def test_hours_and_minutes(self):
        assert _format_runtime(659) == "10h 59m"

    def test_hours_only(self):
        assert _format_runtime(120) == "2h"


class TestPickCoverUrl:
    def test_prefers_500(self):
        assert (
            _pick_cover_url({"500": "https://x/500.jpg", "100": "https://x/100.jpg"})
            == "https://x/500.jpg"
        )

    def test_falls_back_to_first_available(self):
        # No preferred size, but some URL exists
        assert _pick_cover_url({"42": "https://x/42.jpg"}) == "https://x/42.jpg"

    def test_handles_missing(self):
        assert _pick_cover_url(None) is None
        assert _pick_cover_url({}) is None


class TestAudibleToBook:
    def _sample_product(self, **overrides):
        product = {
            "asin": "B082BHJMFF",
            "title": "The Martian",
            "subtitle": "A Novel",
            "language": "english",
            "runtime_length_min": 659,
            "authors": [{"name": "Andy Weir"}],
            "narrators": [{"name": "Wil Wheaton"}],
            "publisher_name": "Audible Studios",
            "release_date": "2014-10-21",
            "product_images": {"500": "https://x/cover.jpg"},
        }
        product.update(overrides)
        return product

    def test_basic_mapping(self):
        book = _audible_to_book(self._sample_product(), region="com")
        assert book is not None
        assert book.provider == "audible"
        assert book.provider_id == "B082BHJMFF"
        assert book.title == "The Martian"
        assert book.subtitle == "A Novel"
        assert book.authors == ["Andy Weir"]
        assert book.language == "en"
        assert book.publisher == "Audible Studios"
        assert book.publish_year == 2014
        assert book.cover_url == "https://x/cover.jpg"

    def test_runtime_in_display_fields(self):
        book = _audible_to_book(self._sample_product(), region="com")
        runtime_field = next(
            (df for df in book.display_fields if df.label == "Runtime"), None
        )
        assert runtime_field is not None
        assert runtime_field.value == "10h 59m"

    def test_narrators_in_display_fields(self):
        book = _audible_to_book(self._sample_product(), region="com")
        narrator_field = next(
            (df for df in book.display_fields if df.label == "Narrator"), None
        )
        assert narrator_field is not None
        assert "Wil Wheaton" in narrator_field.value

    def test_german_language_maps_to_de(self):
        book = _audible_to_book(
            self._sample_product(language="german", title="Die Therapie"), region="de"
        )
        assert book.language == "de"

    def test_missing_asin_returns_none(self):
        product = self._sample_product()
        del product["asin"]
        assert _audible_to_book(product, region="com") is None

    def test_missing_title_returns_none(self):
        product = self._sample_product(title="")
        assert _audible_to_book(product, region="com") is None

    def test_three_narrators_truncated_with_count(self):
        product = self._sample_product(
            narrators=[
                {"name": "A"},
                {"name": "B"},
                {"name": "C"},
                {"name": "D"},
                {"name": "E"},
            ]
        )
        book = _audible_to_book(product, region="com")
        narrator_field = next(
            df for df in book.display_fields if df.label == "Narrator"
        )
        # Shows first 3 then +N
        assert "A, B, C" in narrator_field.value
        assert "+ 2" in narrator_field.value


class TestSearchPaginated:
    def _mock_response(self, products):
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"products": products}
        return resp

    def test_empty_query_returns_empty(self):
        provider = AudibleProvider()
        result = provider.search_paginated(MetadataSearchOptions(query="", limit=10))
        assert result.books == []

    @patch("shelfmark.metadata_providers.audible.requests.get")
    def test_parallel_regions_merge_by_asin(self, mock_get):
        de_product = {
            "asin": "B0FITZEK01",
            "title": "Die Therapie",
            "language": "german",
            "authors": [{"name": "Sebastian Fitzek"}],
            "narrators": [],
            "runtime_length_min": 363,
        }
        com_product = {
            "asin": "B0FITZEK01",  # Same ASIN → dedup
            "title": "Die Therapie",
            "language": "german",
            "authors": [{"name": "Sebastian Fitzek"}],
            "narrators": [],
            "runtime_length_min": 363,
        }
        unique_product = {
            "asin": "B0OTHER001",
            "title": "Andy Weir Martian",
            "language": "english",
            "authors": [{"name": "Andy Weir"}],
            "narrators": [],
            "runtime_length_min": 659,
        }

        def side_effect(url, *_, **__):
            if ".de/" in url:
                return self._mock_response([de_product])
            return self._mock_response([com_product, unique_product])

        mock_get.side_effect = side_effect
        provider = AudibleProvider(regions=["de", "com"])
        result = provider.search_paginated(MetadataSearchOptions(query="fitzek", limit=10))
        assert len(result.books) == 2  # B0FITZEK01 dedupliziert, B0OTHER001 zusätzlich
        asins = {b.provider_id for b in result.books}
        assert asins == {"B0FITZEK01", "B0OTHER001"}

    @patch("shelfmark.metadata_providers.audible.requests.get")
    def test_http_error_falls_back_to_empty(self, mock_get):
        resp = MagicMock()
        resp.status_code = 500
        mock_get.return_value = resp
        provider = AudibleProvider(regions=["de"])
        result = provider.search_paginated(MetadataSearchOptions(query="test", limit=5))
        assert result.books == []

    @patch("shelfmark.metadata_providers.audible.requests.get")
    def test_language_preference_orders_results(self, mock_get):
        en_book = {
            "asin": "B0EN1",
            "title": "Story",
            "language": "english",
            "authors": [{"name": "A"}],
            "narrators": [],
        }
        de_book = {
            "asin": "B0DE1",
            "title": "Geschichte",
            "language": "german",
            "authors": [{"name": "B"}],
            "narrators": [],
        }
        mock_get.return_value = self._mock_response([en_book, de_book])
        provider = AudibleProvider(regions=["de"])  # Single region, both langs returned
        result = provider.search_paginated(
            MetadataSearchOptions(query="x", limit=10, book_languages=["de", "en"])
        )
        # German wins ordering due to book_languages preference
        assert result.books[0].language == "de"


class TestAudibleProviderInterface:
    def test_is_available_true(self):
        assert AudibleProvider().is_available() is True

    def test_name_and_display(self):
        assert AudibleProvider.name == "audible"
        assert AudibleProvider.display_name == "Audible"

    def test_requires_auth_false(self):
        assert AudibleProvider.requires_auth is False
