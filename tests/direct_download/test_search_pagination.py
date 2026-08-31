"""Pagination behaviour of direct_download.search_books (Anna's Archive).

These tests deliberately drive the REAL `_parse_search_result_row`. Stubbing the
parser hides the bug the short-page heuristic is most prone to: counting parse
successes instead of the rows AA actually served, which turns one unparsable row on
a full page into "this was the last page".
"""

import itertools

import pytest

from shelfmark.core.models import SearchFilters
from shelfmark.release_sources.direct_download import SearchUnavailableError

# AA's real page size is never assumed by the code under test; these are just the
# sizes the fixtures below happen to serve.
PAGE_SIZE = 100
SMALL_PAGE_SIZE = 25


def _result_row(record_id: str, *, publisher: bool = True) -> str:
    """A well-formed AA result row (11 cells, anchor with the md5 in the href)."""
    publisher_cell = "<span>Publisher</span>" if publisher else "<span> </span>"
    return (
        f'<tr><td><a href="/md5/{record_id}"><img src="/cover.jpg"/></a></td>'
        f"<td><span>Title {record_id}</span></td>"
        f"<td><span>Author</span></td>"
        f"<td>{publisher_cell}</td>"
        f"<td><span>2020</span></td>"
        f"<td><span>-</span></td>"
        f"<td><span>-</span></td>"
        f"<td><span>English</span></td>"
        f"<td><span>book_nonfiction</span></td>"
        f"<td><span>epub</span></td>"
        f"<td><span>2.1MB</span></td></tr>"
    )


_AD_ROW = '<tr><td colspan="11">Your ad here — advertise on Anna\'s Archive</td></tr>'
_MALFORMED_ROW = '<tr><td><a href="/md5/broken">x</a></td><td><span>Title</span></td></tr>'
_HEADER_ROW = "<tr><th>cover</th><th>title</th></tr>"

_ids = itertools.count()


def _page_html(rows: int, *, extra_rows: str = "", unparsable: int = 0) -> str:
    """A results table with `rows` well-formed rows, `unparsable` of them incomplete."""
    body = _HEADER_ROW
    for i in range(rows):
        body += _result_row(f"md5-{next(_ids):06d}", publisher=i >= unparsable)
    return f"<table>{body}{extra_rows}</table>"


def _install_stubs(monkeypatch, pages: list[str]):
    """Serve `pages` one per html_get_page call, with the real row parser in place."""
    import shelfmark.release_sources.direct_download as dd

    requested: list[str] = []

    def fake_html_get_page(url: str, **_kwargs) -> str:
        requested.append(url)
        index = len(requested) - 1
        return pages[index] if index < len(pages) else ""

    monkeypatch.setattr(dd.downloader, "html_get_page", fake_html_get_page)
    monkeypatch.setattr(dd, "_get_supported_formats", lambda: ["epub"])
    monkeypatch.setattr(dd.network, "get_aa_base_url", lambda: "https://annas-archive.gl")
    monkeypatch.setattr(dd.network, "AAMirrorSelector", lambda: object())
    # Keep the settings registry out of the way; these tests drive the ENV fallback.
    monkeypatch.setattr(dd.config, "get", lambda _key, default=None, **_kw: default)
    return dd, requested


class TestShortPageHeuristic:
    def test_unparsable_row_on_a_full_page_does_not_end_pagination(self, monkeypatch):
        """A row without a publisher span must not shrink the page.

        _parse_search_result_row returns None for it, so counting parse successes
        would report 99 of 100 rows and stop after page 1 -- silently returning a
        fraction of the hits for the production eBook direct path.
        """
        monkeypatch.setenv("AA_MAX_PAGES", "5")
        pages = [
            _page_html(PAGE_SIZE, unparsable=1),
            _page_html(PAGE_SIZE),
            _page_html(48),
        ]
        dd, requested = _install_stubs(monkeypatch, pages)

        books = dd.search_books("sapiens harari", SearchFilters())

        assert len(requested) == 3
        # 99 parsed on page 1 (the incomplete row is dropped), 100 + 48 after.
        assert len(books) == (PAGE_SIZE - 1) + PAGE_SIZE + 48

    def test_ad_row_on_a_full_page_does_not_end_pagination(self, monkeypatch):
        monkeypatch.setenv("AA_MAX_PAGES", "3")
        dd, requested = _install_stubs(
            monkeypatch,
            [_page_html(PAGE_SIZE, extra_rows=_AD_ROW), _page_html(20)],
        )

        books = dd.search_books("query", SearchFilters())

        assert len(requested) == 2
        assert len(books) == PAGE_SIZE + 20

    def test_structurally_broken_row_is_absorbed_by_the_slack(self, monkeypatch):
        """A row with too few cells is not countable, so the slack must cover it."""
        monkeypatch.setenv("AA_MAX_PAGES", "3")
        dd, requested = _install_stubs(
            monkeypatch,
            [_page_html(PAGE_SIZE), _page_html(PAGE_SIZE - 1, extra_rows=_MALFORMED_ROW), ""],
        )

        books = dd.search_books("query", SearchFilters())

        assert len(requested) == 3
        assert len(books) == PAGE_SIZE + (PAGE_SIZE - 1)

    def test_short_page_ends_pagination(self, monkeypatch):
        monkeypatch.setenv("AA_MAX_PAGES", "5")
        dd, requested = _install_stubs(
            monkeypatch, [_page_html(PAGE_SIZE), _page_html(48), _page_html(PAGE_SIZE)]
        )

        books = dd.search_books("query", SearchFilters())

        assert len(requested) == 2
        assert len(books) == PAGE_SIZE + 48

    def test_page_size_is_observed_not_assumed(self, monkeypatch):
        """A 25-row page size must paginate exactly like a 100-row one."""
        monkeypatch.setenv("AA_MAX_PAGES", "5")
        pages = [
            _page_html(SMALL_PAGE_SIZE),
            _page_html(SMALL_PAGE_SIZE),
            _page_html(12),
            _page_html(SMALL_PAGE_SIZE),
        ]
        dd, requested = _install_stubs(monkeypatch, pages)

        books = dd.search_books("query", SearchFilters())

        assert len(requested) == 3
        assert len(books) == 2 * SMALL_PAGE_SIZE + 12

    def test_duplicate_only_page_ends_pagination(self, monkeypatch):
        monkeypatch.setenv("AA_MAX_PAGES", "5")
        page = _page_html(PAGE_SIZE)
        dd, requested = _install_stubs(monkeypatch, [page, page, page])

        books = dd.search_books("query", SearchFilters())

        assert len(requested) == 2
        assert len(books) == PAGE_SIZE


class TestSearchBooksPagination:
    def test_max_pages_env_caps_the_loop(self, monkeypatch):
        monkeypatch.setenv("AA_MAX_PAGES", "2")
        dd, requested = _install_stubs(monkeypatch, [_page_html(PAGE_SIZE) for _ in range(4)])

        books = dd.search_books("sapiens harari", SearchFilters())

        assert len(books) == 2 * PAGE_SIZE
        assert len(requested) == 2
        assert "page=1" in requested[0]
        assert "page=2" in requested[1]

    def test_invalid_max_pages_env_falls_back_to_default(self, monkeypatch):
        monkeypatch.setenv("AA_MAX_PAGES", "not-a-number")
        dd, requested = _install_stubs(monkeypatch, [_page_html(PAGE_SIZE) for _ in range(6)])

        dd.search_books("query", SearchFilters())

        assert len(requested) == 5

    def test_registered_setting_wins_over_environment(self, monkeypatch):
        """The settings registry is the primary source; os.environ is only a fallback."""
        monkeypatch.setenv("AA_MAX_PAGES", "5")
        dd, requested = _install_stubs(monkeypatch, [_page_html(PAGE_SIZE) for _ in range(6)])
        monkeypatch.setattr(
            dd.config,
            "get",
            lambda key, default=None, **_kw: 2 if key == "AA_MAX_PAGES" else default,
        )

        dd.search_books("query", SearchFilters())

        assert len(requested) == 2

    def test_time_budget_stops_pagination_with_partial_results(self, monkeypatch):
        """An exhausted budget returns what was already fetched instead of a timeout."""
        monkeypatch.setenv("AA_MAX_PAGES", "5")
        monkeypatch.setenv("AA_SEARCH_BUDGET_SECONDS", "90")
        dd, requested = _install_stubs(monkeypatch, [_page_html(PAGE_SIZE) for _ in range(5)])

        clock = itertools.count(step=100.0)
        monkeypatch.setattr(dd.time, "monotonic", lambda: next(clock))

        books = dd.search_books("query", SearchFilters())

        # Page 1 is never budgeted; page 2 sees the deadline already blown.
        assert len(requested) == 1
        assert len(books) == PAGE_SIZE

    def test_time_budget_can_be_disabled(self, monkeypatch):
        monkeypatch.setenv("AA_MAX_PAGES", "3")
        monkeypatch.setenv("AA_SEARCH_BUDGET_SECONDS", "0")
        dd, requested = _install_stubs(monkeypatch, [_page_html(PAGE_SIZE) for _ in range(3)])

        clock = itertools.count(step=1000.0)
        monkeypatch.setattr(dd.time, "monotonic", lambda: next(clock))

        dd.search_books("query", SearchFilters())

        assert len(requested) == 3

    def test_unreachable_first_page_still_raises(self, monkeypatch):
        """The budget must not turn a dead mirror into a silent empty result."""
        monkeypatch.setenv("AA_MAX_PAGES", "3")
        dd, _requested = _install_stubs(monkeypatch, [""])

        with pytest.raises(SearchUnavailableError):
            dd.search_books("query", SearchFilters())

    def test_unreachable_later_page_returns_partial_results(self, monkeypatch):
        monkeypatch.setenv("AA_MAX_PAGES", "3")
        dd, requested = _install_stubs(monkeypatch, [_page_html(PAGE_SIZE), ""])

        books = dd.search_books("query", SearchFilters())

        assert len(books) == PAGE_SIZE
        assert len(requested) == 2


class TestIsResultRow:
    def test_counts_rows_the_parser_rejects(self, monkeypatch):
        """The row counter is structural: rows the parser drops still count as served."""
        from bs4 import BeautifulSoup

        import shelfmark.release_sources.direct_download as dd

        html = _page_html(1, unparsable=1)
        row = BeautifulSoup(html, "html.parser").find_all("tr")[1]

        assert dd._parse_search_result_row(row) is None
        assert dd._is_result_row(row) is True

    def test_rejects_header_and_ad_rows(self):
        from bs4 import BeautifulSoup

        import shelfmark.release_sources.direct_download as dd

        soup = BeautifulSoup(f"<table>{_HEADER_ROW}{_AD_ROW}</table>", "html.parser")
        assert [dd._is_result_row(tr) for tr in soup.find_all("tr")] == [False, False]
