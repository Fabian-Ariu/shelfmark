from shelfmark.core.models import SearchFilters
from shelfmark.core.search_plan import build_release_search_plan
from shelfmark.metadata_providers import BookMetadata
from shelfmark.release_sources import BrowseRecord
from shelfmark.release_sources.direct_download import DirectDownloadSource


def _browse_record(record_id: str, title: str) -> BrowseRecord:
    return BrowseRecord(id=record_id, title=title, source="direct_download")


def _enable_direct_download(
    monkeypatch, *, max_pages: int | None = None, browse_max_pages: int | None = None
):
    import shelfmark.release_sources.direct_download as dd

    original_get = dd.config.get

    def _fake_get(key: str, default=None, user_id=None):
        del user_id
        if key == "DIRECT_DOWNLOAD_ENABLED":
            return True
        if key == "AA_MAX_PAGES" and max_pages is not None:
            return max_pages
        if key == "AA_BROWSE_MAX_PAGES" and browse_max_pages is not None:
            return browse_max_pages
        return original_get(key, default)

    monkeypatch.setattr(dd.config, "get", _fake_get)
    monkeypatch.setattr("shelfmark.core.mirrors.has_aa_mirror_configuration", lambda: True)
    return dd


class TestDirectDownloadSearchQueries:
    def test_uses_search_title_for_english_queries(self, monkeypatch):
        captured: list[str] = []

        def fake_search_books(query: str, filters):
            captured.append(query)
            return []

        dd = _enable_direct_download(monkeypatch)

        monkeypatch.setattr(dd, "search_books", fake_search_books)

        source = DirectDownloadSource()
        book = BookMetadata(
            provider="hardcover",
            provider_id="123",
            title="Mistborn: The Final Empire",
            search_title="The Final Empire",
            search_author="Brandon Sanderson",
            authors=["Brandon Sanderson"],
            titles_by_language={
                "en": "Mistborn: The Final Empire",
                "hu": "A végső birodalom",
            },
        )

        plan = build_release_search_plan(book, languages=["en", "hu"])
        source.search(book, plan, expand_search=True)

        assert "The Final Empire Brandon Sanderson" in captured
        assert "A végső birodalom Brandon Sanderson" in captured
        assert "Mistborn: The Final Empire Brandon Sanderson" not in captured

    def test_deduplicates_results_across_localized_queries(self, monkeypatch):
        captured: list[tuple[str, list[str] | None]] = []
        records_by_query = {
            "The Final Empire Brandon Sanderson": [
                _browse_record("shared", "Shared release"),
                _browse_record("en-only", "English only"),
            ],
            "A végső birodalom Brandon Sanderson": [
                _browse_record("shared", "Shared release"),
                _browse_record("hu-only", "Hungarian only"),
            ],
        }

        def fake_search_books(query: str, filters):
            captured.append((query, filters.lang))
            return records_by_query[query]

        dd = _enable_direct_download(monkeypatch)

        monkeypatch.setattr(dd, "search_books", fake_search_books)

        source = DirectDownloadSource()
        book = BookMetadata(
            provider="hardcover",
            provider_id="123",
            title="Mistborn: The Final Empire",
            search_title="The Final Empire",
            search_author="Brandon Sanderson",
            authors=["Brandon Sanderson"],
            titles_by_language={
                "en": "Mistborn: The Final Empire",
                "hu": "A végső birodalom",
            },
        )

        plan = build_release_search_plan(book, languages=["en", "hu"])
        results = source.search(book, plan, expand_search=True)

        assert captured == [
            ("The Final Empire Brandon Sanderson", ["en"]),
            ("A végső birodalom Brandon Sanderson", ["hu"]),
        ]
        assert [release.source_id for release in results] == ["shared", "en-only", "hu-only"]

    def test_retries_without_language_filters_when_localized_queries_miss(self, monkeypatch):
        captured: list[tuple[str, list[str] | None]] = []
        fallback_results = {
            "The Final Empire Brandon Sanderson": [
                _browse_record("fallback-en", "Fallback English")
            ],
            "A végső birodalom Brandon Sanderson": [
                _browse_record("fallback-hu", "Fallback Hungarian")
            ],
        }

        def fake_search_books(query: str, filters):
            captured.append((query, filters.lang))
            if filters.lang:
                return []
            return fallback_results[query]

        dd = _enable_direct_download(monkeypatch)

        monkeypatch.setattr(dd, "search_books", fake_search_books)

        source = DirectDownloadSource()
        book = BookMetadata(
            provider="hardcover",
            provider_id="123",
            title="Mistborn: The Final Empire",
            search_title="The Final Empire",
            search_author="Brandon Sanderson",
            authors=["Brandon Sanderson"],
            titles_by_language={
                "en": "Mistborn: The Final Empire",
                "hu": "A végső birodalom",
            },
        )

        plan = build_release_search_plan(book, languages=["en", "hu"])
        results = source.search(book, plan, expand_search=True)

        assert captured == [
            ("The Final Empire Brandon Sanderson", ["en"]),
            ("A végső birodalom Brandon Sanderson", ["hu"]),
            ("The Final Empire Brandon Sanderson", None),
            ("A végső birodalom Brandon Sanderson", None),
        ]
        assert [release.source_id for release in results] == ["fallback-en", "fallback-hu"]

    def test_manual_query_fallback_preserves_other_filters(self, monkeypatch):
        captured: list[tuple[str, list[str] | None, list[str] | None]] = []
        dd = _enable_direct_download(monkeypatch, max_pages=5)

        def fake_search_books_page(query: str, filters, *, page: int = 1):
            captured.append((query, filters.lang, filters.format))
            if filters.lang:
                return dd.AASearchPage([], page, has_more=False, page_rows=0)
            return dd.AASearchPage(
                [_browse_record("manual-1", "Manual result")], page, has_more=True, page_rows=100
            )

        monkeypatch.setattr(dd, "search_books_page", fake_search_books_page)

        source = DirectDownloadSource()
        book = BookMetadata(
            provider="hardcover",
            provider_id="123",
            title="Mistborn: The Final Empire",
            authors=["Brandon Sanderson"],
        )

        plan = build_release_search_plan(
            book,
            languages=["en"],
            manual_query="mistborn custom query",
            source_filters=SearchFilters(format=["epub"], sort="newest"),
        )
        results = source.search(book, plan)

        assert [release.source_id for release in results] == ["manual-1"]
        assert captured == [
            ("mistborn custom query", ["en"], ["epub"]),
            ("mistborn custom query", None, ["epub"]),
        ]
        # The fallback answers a different query than the one the client would page
        # on, so it must not advertise a page 2.
        assert source.last_search_has_more is False


class TestDirectDownloadBrowsePaging:
    """Browse mode fetches one AA page per request, on demand."""

    @staticmethod
    def _browse_plan(page: int):
        book = BookMetadata(
            provider="hardcover",
            provider_id="123",
            title="Mistborn: The Final Empire",
            authors=["Brandon Sanderson"],
        )
        plan = build_release_search_plan(
            book,
            languages=["en"],
            manual_query="mistborn custom query",
            source_filters=SearchFilters(format=["epub"]),
            page=page,
        )
        return book, plan

    def test_only_the_requested_page_is_fetched(self, monkeypatch):
        """One browse request = one AA page = one protection-challenge solve."""
        dd = _enable_direct_download(monkeypatch, browse_max_pages=5)
        captured: list[int] = []

        def fake_search_books_page(query: str, filters, *, page: int = 1):
            captured.append(page)
            return dd.AASearchPage(
                [_browse_record(f"page-{page}", "Result")], page, has_more=True, page_rows=100
            )

        monkeypatch.setattr(dd, "search_books_page", fake_search_books_page)

        source = DirectDownloadSource()
        book, plan = self._browse_plan(3)
        results = source.search(book, plan)

        assert captured == [3]
        assert [release.source_id for release in results] == ["page-3"]
        assert source.last_search_page == 3
        assert source.last_search_has_more is True

    def test_page_beyond_the_limit_is_clamped(self, monkeypatch):
        """AA_BROWSE_MAX_PAGES bounds how far a client may page, nothing else."""
        dd = _enable_direct_download(monkeypatch, browse_max_pages=2)
        captured: list[int] = []

        def fake_search_books_page(query: str, filters, *, page: int = 1):
            captured.append(page)
            return dd.AASearchPage(
                [_browse_record(f"page-{page}", "Result")], page, has_more=True, page_rows=100
            )

        monkeypatch.setattr(dd, "search_books_page", fake_search_books_page)

        source = DirectDownloadSource()
        book, plan = self._browse_plan(9)
        source.search(book, plan)

        assert captured == [2]
        assert source.last_search_page == 2
        # The page limit is reached, so no further page is offered.
        assert source.last_search_has_more is False

    def test_language_fallback_does_not_run_on_later_pages(self, monkeypatch):
        """An empty page 5 is the end of the list, not a reason for a second solve."""
        dd = _enable_direct_download(monkeypatch, browse_max_pages=10)
        captured: list[list[str] | None] = []

        def fake_search_books_page(query: str, filters, *, page: int = 1):
            captured.append(filters.lang)
            return dd.AASearchPage([], page, has_more=False, page_rows=0)

        monkeypatch.setattr(dd, "search_books_page", fake_search_books_page)

        source = DirectDownloadSource()
        book, plan = self._browse_plan(5)
        results = source.search(book, plan)

        assert captured == [["en"]]
        assert results == []
        assert source.last_search_has_more is False

    def test_release_search_page_limit_does_not_disable_the_pager(self, monkeypatch):
        """AA_MAX_PAGES=1 is the production value; it must not kill the browse pager.

        The two limits used to be the same key. With AA_MAX_PAGES=1 the browse page was
        clamped to 1 and `page < max_pages` was 1 < 1, so has_more was structurally
        false and the Load More button could never appear -- the whole feature was
        inert in exactly the configuration it ships into.
        """
        dd = _enable_direct_download(monkeypatch, max_pages=1)
        captured: list[int] = []

        def fake_search_books_page(query: str, filters, *, page: int = 1):
            captured.append(page)
            return dd.AASearchPage(
                [_browse_record(f"page-{page}", "Result")], page, has_more=True, page_rows=100
            )

        monkeypatch.setattr(dd, "search_books_page", fake_search_books_page)

        source = DirectDownloadSource()
        book, plan = self._browse_plan(2)
        source.search(book, plan)

        assert captured == [2]
        assert source.last_search_page == 2
        assert source.last_search_has_more is True

    def test_browse_limit_of_one_disables_the_pager(self, monkeypatch):
        """The opt-out is the browse key itself, not the release-search key."""
        dd = _enable_direct_download(monkeypatch, browse_max_pages=1)
        captured: list[int] = []

        def fake_search_books_page(query: str, filters, *, page: int = 1):
            captured.append(page)
            return dd.AASearchPage(
                [_browse_record(f"page-{page}", "Result")], page, has_more=True, page_rows=100
            )

        monkeypatch.setattr(dd, "search_books_page", fake_search_books_page)

        source = DirectDownloadSource()
        book, plan = self._browse_plan(4)
        source.search(book, plan)

        assert captured == [1]
        assert source.last_search_has_more is False
