"""Unit tests for the CombinedAudiobookProvider (Hardcover + Audible fan-out)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from shelfmark.metadata_providers import (
    BookMetadata,
    MetadataSearchOptions,
    SearchResult,
)
from shelfmark.metadata_providers.combined_audiobook import (
    CombinedAudiobookProvider,
    _looks_like_asin,
)


def _mk_book(provider: str, provider_id: str, title: str, authors=None, language="en"):
    return BookMetadata(
        provider=provider,
        provider_display_name=provider.title(),
        provider_id=provider_id,
        title=title,
        authors=authors or [],
        language=language,
    )


class TestLooksLikeAsin:
    def test_asin_b0_prefix(self):
        assert _looks_like_asin("B082BHJMFF") is True
        assert _looks_like_asin("B0CFM6FZTT") is True

    def test_numeric_isbn10(self):
        assert _looks_like_asin("0593065565") is True

    def test_isbn10_with_x(self):
        assert _looks_like_asin("059306556X") is True

    def test_wrong_length(self):
        assert _looks_like_asin("B0") is False
        assert _looks_like_asin("B082BHJMFF1") is False

    def test_hardcover_id_numeric(self):
        # Hardcover IDs are usually short integers
        assert _looks_like_asin("328491") is False
        assert _looks_like_asin("1") is False

    def test_empty_input(self):
        assert _looks_like_asin("") is False
        assert _looks_like_asin(None) is False


class TestCombinedSearchDedup:
    def _setup_providers(self, audible_books, hardcover_books):
        """Build mock sub-providers returning preset book lists."""
        audible = MagicMock()
        audible.name = "audible"
        audible.is_available.return_value = True
        audible.search_paginated.return_value = SearchResult(
            books=audible_books, page=1, total_found=len(audible_books), has_more=False
        )
        hardcover = MagicMock()
        hardcover.name = "hardcover"
        hardcover.is_available.return_value = True
        hardcover.search_paginated.return_value = SearchResult(
            books=hardcover_books,
            page=1,
            total_found=len(hardcover_books),
            has_more=False,
        )
        return audible, hardcover

    @patch("shelfmark.metadata_providers.combined_audiobook._instantiate")
    @patch("shelfmark.metadata_providers.combined_audiobook.is_provider_enabled")
    def test_audible_only_when_hardcover_absent(self, mock_enabled, mock_inst):
        mock_enabled.return_value = True
        audible, _ = self._setup_providers(
            [_mk_book("audible", "B0X", "Martian", ["Andy Weir"])], []
        )

        def fake_instantiate(name):
            return audible if name == "audible" else None

        mock_inst.side_effect = fake_instantiate
        provider = CombinedAudiobookProvider()
        result = provider.search_paginated(MetadataSearchOptions(query="martian", limit=10))
        assert len(result.books) == 1
        assert result.books[0].provider == "audible"

    @patch("shelfmark.metadata_providers.combined_audiobook._instantiate")
    @patch("shelfmark.metadata_providers.combined_audiobook.is_provider_enabled")
    def test_work_hash_dedup_across_providers(self, mock_enabled, mock_inst):
        mock_enabled.return_value = True
        # Same work returned by both providers, different IDs - work-hash should dedupe
        audible_book = _mk_book(
            "audible", "B0X", "Die Therapie", ["Sebastian Fitzek"], language="de"
        )
        hardcover_book = _mk_book(
            "hardcover", "123", "Die Therapie", ["Sebastian Fitzek"], language="de"
        )
        audible, hardcover = self._setup_providers([audible_book], [hardcover_book])
        mock_inst.side_effect = lambda name: {"audible": audible, "hardcover": hardcover}.get(name)

        provider = CombinedAudiobookProvider()
        result = provider.search_paginated(MetadataSearchOptions(query="fitzek", limit=10))
        # Audible wins on tie because it has ASIN
        assert len(result.books) == 1
        assert result.books[0].provider == "audible"
        assert result.books[0].provider_id == "B0X"

    @patch("shelfmark.metadata_providers.combined_audiobook._instantiate")
    @patch("shelfmark.metadata_providers.combined_audiobook.is_provider_enabled")
    def test_unique_books_from_both_kept(self, mock_enabled, mock_inst):
        mock_enabled.return_value = True
        audible_books = [
            _mk_book("audible", "B0FITZEK", "Die Therapie", ["Sebastian Fitzek"], language="de"),
        ]
        hardcover_books = [
            _mk_book("hardcover", "123", "Andy Weir Diary", ["Andy Weir"], language="en"),
        ]
        audible, hardcover = self._setup_providers(audible_books, hardcover_books)
        mock_inst.side_effect = lambda name: {"audible": audible, "hardcover": hardcover}.get(name)

        provider = CombinedAudiobookProvider()
        result = provider.search_paginated(MetadataSearchOptions(query="x", limit=10))
        assert len(result.books) == 2
        providers_seen = {b.provider for b in result.books}
        assert providers_seen == {"audible", "hardcover"}

    @patch("shelfmark.metadata_providers.combined_audiobook._instantiate")
    @patch("shelfmark.metadata_providers.combined_audiobook.is_provider_enabled")
    def test_sub_provider_crash_doesnt_break_other(self, mock_enabled, mock_inst):
        mock_enabled.return_value = True
        crashing = MagicMock()
        crashing.name = "audible"
        crashing.is_available.return_value = True
        crashing.search_paginated.side_effect = RuntimeError("Audible API down")
        good = MagicMock()
        good.name = "hardcover"
        good.is_available.return_value = True
        good.search_paginated.return_value = SearchResult(
            books=[_mk_book("hardcover", "1", "Surviving Book", ["A"])],
            page=1,
            total_found=1,
            has_more=False,
        )
        mock_inst.side_effect = lambda name: {"audible": crashing, "hardcover": good}.get(name)

        provider = CombinedAudiobookProvider()
        result = provider.search_paginated(MetadataSearchOptions(query="x", limit=10))
        assert len(result.books) == 1
        assert result.books[0].provider == "hardcover"

    @patch("shelfmark.metadata_providers.combined_audiobook._instantiate")
    @patch("shelfmark.metadata_providers.combined_audiobook.is_provider_enabled")
    def test_language_preference_orders_merged_results(self, mock_enabled, mock_inst):
        mock_enabled.return_value = True
        audible_books = [
            _mk_book("audible", "B0EN", "Story", ["A"], language="en"),
            _mk_book("audible", "B0DE", "Geschichte", ["B"], language="de"),
        ]
        audible, hardcover = self._setup_providers(audible_books, [])
        mock_inst.side_effect = lambda name: {"audible": audible, "hardcover": hardcover}.get(name)

        provider = CombinedAudiobookProvider()
        result = provider.search_paginated(
            MetadataSearchOptions(query="x", limit=10, book_languages=["de", "en"])
        )
        # German comes first because book_languages preference
        assert result.books[0].language == "de"

    @patch("shelfmark.metadata_providers.combined_audiobook._instantiate")
    @patch("shelfmark.metadata_providers.combined_audiobook.is_provider_enabled")
    def test_forces_audiobook_content_type_to_sub_providers(self, mock_enabled, mock_inst):
        """Even if caller passes content_type='ebook', sub-calls get 'audiobook'."""
        mock_enabled.return_value = True
        audible, hardcover = self._setup_providers([], [])
        mock_inst.side_effect = lambda name: {"audible": audible, "hardcover": hardcover}.get(name)

        provider = CombinedAudiobookProvider()
        provider.search_paginated(
            MetadataSearchOptions(query="x", limit=10, content_type="ebook")
        )
        # Audible got called with content_type='audiobook'
        audible.search_paginated.assert_called_once()
        passed_options = audible.search_paginated.call_args[0][0]
        assert passed_options.content_type == "audiobook"


class TestGetBookRouting:
    @patch("shelfmark.metadata_providers.combined_audiobook._instantiate")
    def test_asin_routes_to_audible(self, mock_inst):
        audible = MagicMock()
        audible.is_available.return_value = True
        audible.get_book.return_value = _mk_book("audible", "B082BHJMFF", "The Martian")
        mock_inst.side_effect = lambda name: audible if name == "audible" else None
        provider = CombinedAudiobookProvider()
        book = provider.get_book("B082BHJMFF")
        assert book is not None
        assert book.provider == "audible"

    @patch("shelfmark.metadata_providers.combined_audiobook._instantiate")
    def test_numeric_id_routes_to_hardcover(self, mock_inst):
        hardcover = MagicMock()
        hardcover.is_available.return_value = True
        hardcover.get_book.return_value = _mk_book("hardcover", "328491", "HP")
        mock_inst.side_effect = lambda name: hardcover if name == "hardcover" else None
        provider = CombinedAudiobookProvider()
        book = provider.get_book("328491")
        assert book is not None
        assert book.provider == "hardcover"
