"""Direct download source - Anna's Archive/Libgen with fallback cascade."""

import itertools
import json
import os
import re
import time
from dataclasses import replace
from http import HTTPStatus
from typing import TYPE_CHECKING, ClassVar, NamedTuple, NoReturn, TypedDict
from urllib.parse import quote, urlparse

import requests
from bs4 import BeautifulSoup, Tag
from bs4.element import NavigableString

from shelfmark.config.env import DEBUG_SKIP_SOURCES, TMP_DIR
from shelfmark.core.config import config
from shelfmark.core.logger import setup_logger
from shelfmark.core.models import DownloadTask, SearchFilters, build_filename
from shelfmark.core.utils import CONTENT_TYPES, get_aa_content_type_dir
from shelfmark.core.utils import is_audiobook as check_audiobook
from shelfmark.download import http as downloader
from shelfmark.download import network
from shelfmark.release_sources import (
    BrowseRecord,
    ColumnAlign,
    ColumnColorHint,
    ColumnRenderType,
    ColumnSchema,
    DownloadHandler,
    Release,
    ReleaseColumnConfig,
    ReleaseProtocol,
    ReleaseSource,
    SourceUnavailableError,
    register_handler,
    register_source,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable
    from pathlib import Path
    from threading import Event

    from shelfmark.core.search_plan import ReleaseSearchPlan
    from shelfmark.metadata_providers import BookMetadata

logger = setup_logger(__name__)

# Slack for the "a shorter page is the last page" heuristic. AA's real page size is
# not hardcoded anywhere -- it is measured from the pages we actually receive -- but
# rows AA renders differently (ads, malformed <tr>) make a full page look short. The
# slack is proportional to the reference length with an absolute floor: a fixed +2 was
# enough for a single malformed row but not for the handful of ad rows AA injects into
# some result sets, and undercounting by more than the slack truncates the list.
# Being wrong in this direction costs one extra fetch; breaking too early hides hits.
_PAGE_LENGTH_SLACK = 2
_PAGE_LENGTH_SLACK_RATIO = 0.1

# Fallback reference length used only until this process has measured a real AA page
# (see _observed_aa_page_size). Rationale for the value: production has served a
# single browse page carrying 48 result rows, and the fixtures model AA's page size as
# 100 -- a full AA page is therefore never shorter than this. Too low costs one wasted
# fetch at the end of a list; too high would hide page 2 of the FIRST query in a fresh
# worker, which is why it sits below every page length ever observed here.
_AA_MIN_PLAUSIBLE_PAGE_SIZE = 48

# A result row carries at least this many <td>s; see _is_result_row().
_RESULT_ROW_MIN_CELLS = 11

# Highest browse page a client may request (AA_BROWSE_MAX_PAGES). Pure guard rail:
# the browse path fetches one page per request, so this bounds how far the pager can
# walk, not how much work a single request does. Kept separate from AA_MAX_PAGES on
# purpose -- see the comment at the browse branch in DirectDownloadSource.search().
_AA_BROWSE_MAX_PAGES_DEFAULT = 20


def _int_setting(name: str, default: int, *, minimum: int) -> int:
    """Read an int setting, preferring the settings registry over raw os.environ.

    The registry (config.get) is the project-wide configuration path: UI-visible,
    ENV-resolved, per-deployment. It is asked first. The os.environ fallback is not
    decoration: this module is ALSO deployed as a runtime bind-mount patch over an
    older image whose settings registry does not know these keys yet, and there
    config.get() would silently return the default and ignore the AA_MAX_PAGES from
    docker-compose. The fallback keeps the patched file honest on the old image and
    is a no-op once the image carries the registered fields.
    """
    raw = config.get(name, None)
    if raw is None:
        raw = os.environ.get(name)
    try:
        return max(minimum, int(raw))
    except ValueError, TypeError:
        return default


class SourcePriorityEntry(TypedDict):
    """Normalized source priority entry from config."""

    id: str
    enabled: bool


def _raise_runtime_error(message: str) -> NoReturn:
    raise RuntimeError(message)


def _coerce_str_list(value: object) -> list[str]:
    """Return only string items from a config value."""
    if not isinstance(value, list | tuple):
        return []
    return [item for item in value if isinstance(item, str)]


def _get_supported_formats() -> list[str]:
    """Return configured supported formats as a clean string list."""
    return _coerce_str_list(config.SUPPORTED_FORMATS)


def _parse_source_priority_entries(
    value: object,
    *,
    allowed_ids: set[str] | None = None,
    excluded_ids: set[str] | None = None,
) -> list[SourcePriorityEntry]:
    """Normalize orderable-list config values into typed source entries."""
    if not isinstance(value, list):
        return []

    entries: list[SourcePriorityEntry] = []
    for item in value:
        if not isinstance(item, dict):
            continue

        source_id = item.get("id")
        if not isinstance(source_id, str):
            continue

        if allowed_ids is not None and source_id not in allowed_ids:
            continue
        if excluded_ids is not None and source_id in excluded_ids:
            continue

        entries.append({"id": source_id, "enabled": bool(item.get("enabled", True))})

    return entries


def _html_response_text(response: str | tuple[str, str]) -> str:
    """Extract the HTML body from downloader responses."""
    if isinstance(response, tuple):
        return response[0]
    return response


def _attr_to_str(value: object) -> str | None:
    """Convert a BeautifulSoup attribute value to a plain string."""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        for item in value:
            if isinstance(item, str):
                return item
    return None


def _get_attr(tag: Tag, attr: str) -> str | None:
    """Safely fetch a tag attribute as a string."""
    return _attr_to_str(tag.get(attr))


def _first_stripped_text(tag: Tag | None) -> str | None:
    """Return the first non-empty stripped string from a tag."""
    if tag is None:
        return None

    for text in tag.stripped_strings:
        return text
    return None


def _iter_child_tags(tag: Tag) -> Iterable[Tag]:
    """Iterate only over child tags, skipping text nodes."""
    for child in tag.children:
        if isinstance(child, Tag):
            yield child


def _find_first_anchor_with_text(
    container: BeautifulSoup | Tag,
    text: str,
    *,
    contains: bool = False,
) -> Tag | None:
    """Find the first anchor whose text matches the requested value."""
    expected = text.lower()
    for anchor in container.find_all("a", href=True):
        anchor_text = anchor.get_text(strip=True)
        if not anchor_text:
            continue
        candidate = anchor_text.lower()
        if candidate == expected or (contains and expected in candidate):
            return anchor
    return None


def _find_text_node(container: BeautifulSoup | Tag, needle: str) -> NavigableString | None:
    """Find a text node containing a case-insensitive substring."""
    expected = needle.lower()
    for text_node in container.find_all(string=True):
        if isinstance(text_node, NavigableString) and expected in text_node.strip().lower():
            return text_node
    return None


def _tag_has_class_containing(tag: Tag, needle: str) -> bool:
    """Check whether a tag has a CSS class containing a substring."""
    class_values = tag.get("class")
    if isinstance(class_values, str):
        return needle in class_values
    if isinstance(class_values, list):
        return any(isinstance(value, str) and needle in value for value in class_values)
    return False


_aa_slow_rotation = itertools.count()
_url_source_types: dict[str, str] = {}

if DEBUG_SKIP_SOURCES:
    logger.warning("DEBUG_SKIP_SOURCES active: skipping sources %s", DEBUG_SKIP_SOURCES)

_DOWNLOAD_SOURCES = [
    ("welib", "Welib", ["welib.org"]),
    ("aa-fast", "Anna's Archive (Fast)", ["/dyn/api/fast_download"]),
    ("aa-slow-wait", "Anna's Archive (Waitlist)", []),  # Matched via _url_source_types
    ("aa-slow-nowait", "Anna's Archive", []),  # Matched via _url_source_types
    ("aa-slow", "Anna's Archive", ["/slow_download/", "annas-"]),  # Fallback for untagged AA URLs
    ("libgen", "Libgen", ["libgen"]),
    ("zlib", "Z-Library", ["z-lib", "zlibrary"]),
]

_SOURCE_FAILURE_THRESHOLD = 4
_MIN_VALID_FILE_SIZE = 10 * 1024
_AA_COUNTDOWN_MAX_SECONDS = 300

# Sources that require Cloudflare bypass
_CF_BYPASS_REQUIRED = frozenset({"aa-slow-nowait", "aa-slow-wait", "zlib", "welib"})

# Sources whose URLs come from AA page (multiple mirrors)
_AA_PAGE_SOURCES = frozenset({"aa-slow-nowait", "aa-slow-wait"})


def _is_configured_zlib_link(url: str) -> bool:
    """Return True when a URL belongs to a configured Z-Library mirror."""
    from shelfmark.core.mirrors import get_zlib_cookie_domains

    hostname = (urlparse(url).hostname or "").lower()
    if not hostname:
        return False

    base_domain = ".".join(hostname.split(".")[-2:]) if "." in hostname else hostname

    for domain in get_zlib_cookie_domains():
        candidate = str(domain).lower()
        if hostname == candidate or hostname.endswith(f".{candidate}") or base_domain == candidate:
            return True

    return False


def _get_md5_url_template(source_id: str) -> str | None:
    """Get URL template for MD5-based sources from centralized config."""
    from shelfmark.core import mirrors

    if source_id == "zlib":
        return mirrors.get_zlib_url_template()
    if source_id == "welib":
        return mirrors.get_welib_url_template()
    return None


def _get_libgen_domains() -> list[str]:
    """Get LibGen domains from centralized config."""
    from shelfmark.core import mirrors

    return mirrors.get_libgen_mirrors()


_LIBGEN_GET_PATTERNS = [
    re.compile(
        r'<a\s+href=["\']([^"\']*get\.php\?md5=[^"\']+&key=[^"\']+)["\'][^>]*>\s*<h2[^>]*>GET</h2>\s*</a>',
        re.IGNORECASE,
    ),
    re.compile(
        r'<a[^>]+href=["\']([^"\']*get\.php\?md5=[^"\']+&(?:amp;)?key=[^"\']+)["\']', re.IGNORECASE
    ),
    re.compile(
        r'<a\s+href=["\']([^"\']*get\.php[^"\']*)["\'][^>]*>[\s\S]*?<h2[^>]*>GET</h2>',
        re.IGNORECASE,
    ),
    re.compile(
        r'href=["\']([^"\']*get\.php\?[^"\']*md5=[^"\']*&[^"\']*key=[^"\']+)["\']', re.IGNORECASE
    ),
]


def _get_source_priority() -> list[SourcePriorityEntry]:
    """Get the full source priority list.

    Fast sources come from user config (FAST_SOURCES_DISPLAY).
    Slow sources come from user config.
    """
    from shelfmark.core import mirrors

    fast_sources = _parse_source_priority_entries(
        config.get("FAST_SOURCES_DISPLAY"),
        allowed_ids={"aa-fast", "libgen"},
    )
    has_donator_key = bool(config.get("AA_DONATOR_KEY"))

    for source in fast_sources:
        if (not mirrors.has_download_source_mirror_configuration(source["id"])) or (
            source["id"] == "aa-fast" and not has_donator_key
        ):
            source["enabled"] = False

    slow_sources = _parse_source_priority_entries(
        config.get("SOURCE_PRIORITY"),
        excluded_ids={"aa-fast", "libgen"},
    )
    for source in slow_sources:
        if not mirrors.has_download_source_mirror_configuration(source["id"]):
            source["enabled"] = False

    return fast_sources + slow_sources


def _is_source_enabled(source_id: str) -> bool:
    """Check if a source is enabled in the priority config.

    Returns False for unknown sources.
    """
    for item in _get_source_priority():
        if item["id"] == source_id:
            return item.get("enabled", True)
    return False


def _get_direct_download_unavailable_reason() -> str | None:
    """Return a user-facing reason when Direct Download cannot be used."""
    from shelfmark.core import mirrors

    if not config.get("DIRECT_DOWNLOAD_ENABLED", False):
        return (
            "Direct Download is disabled. Enable the source in Settings and add your mirror URLs."
        )

    if not mirrors.has_aa_mirror_configuration():
        return (
            "Direct Download is not configured. Add at least one Anna's Archive mirror URL in "
            "Settings."
        )

    return None


def _ensure_direct_download_available() -> None:
    """Raise a source-unavailable error when Direct Download is disabled or unconfigured."""
    reason = _get_direct_download_unavailable_reason()
    if reason:
        raise SearchUnavailableError(reason)


_SIZE_UNIT_PATTERN = re.compile(r"(kb|mb|gb|tb)", re.IGNORECASE)


def _normalize_size(size_str: str) -> str:
    """Normalize size string by uppercasing units (e.g., '5.2 mb' -> '5.2 MB')."""
    return _SIZE_UNIT_PATTERN.sub(lambda m: m.group(1).upper(), size_str.strip())


class SearchUnavailableError(SourceUnavailableError):
    """Raised when Anna's Archive cannot be reached via any mirror/DNS."""


class AASearchPage(NamedTuple):
    """One Anna's Archive result page plus what a pager needs to ask for the next one."""

    records: list[BrowseRecord]
    page: int
    has_more: bool
    page_rows: int


class _AAPageFetch(NamedTuple):
    """Raw outcome of a single AA page fetch, before dedup or any paging policy."""

    records: list[BrowseRecord]
    page_rows: int
    # "ok" | "unreachable" | "no_files" | "no_table"
    status: str


class _AASearchRequest(NamedTuple):
    """Everything that stays constant across the pages of one AA query."""

    query_html: str
    filters_query: str
    formats: list[str]
    selector: network.AAMirrorSelector


# Longest AA result page observed in this process, measured in rows AA actually
# rendered. AA's page size is a property of the site, not of a query, so a single-page
# fetch can reuse what earlier fetches measured -- that is the only way the "a short
# page is the last page" heuristic survives being called one page at a time.
#
# HONEST LIMITS (do not restore the old "can never truncate a result set" claim):
# this is process-wide, monotonic and never reset, so it is shared across queries and
# users of a worker. A query whose full page renders fewer countable rows than the
# longest page this process has seen CAN be cut short at has_more. The value is
# bounded above by AA's page size (a page cannot exceed it), so the error is always
# small -- which is exactly what _PAGE_LENGTH_SLACK_RATIO is sized for -- but it is
# not zero. Get it wrong the other way and the cost is one extra fetch.
_observed_aa_page_size = 0


def _record_observed_page_size(page_rows: int) -> None:
    """Remember the longest AA page seen so far (see _observed_aa_page_size)."""
    global _observed_aa_page_size
    _observed_aa_page_size = max(_observed_aa_page_size, page_rows)


def _page_length_slack(reference_rows: int) -> int:
    """How many rows a page may be missing and still count as a full page."""
    return max(_PAGE_LENGTH_SLACK, int(reference_rows * _PAGE_LENGTH_SLACK_RATIO))


def _page_looks_full(page_rows: int) -> bool:
    """Whether a page of this length is long enough to expect another one after it.

    Without a measured reference the fallback length is used rather than an
    unconditional "yes": a fresh worker (gunicorn runs --workers 1, so that is every
    container restart) would otherwise promise a page 2 for every first search, and
    the user pays a 45-60s protection-challenge solve to be shown nothing.
    """
    if page_rows <= 0:
        return False
    reference = _observed_aa_page_size or _AA_MIN_PLAUSIBLE_PAGE_SIZE
    return page_rows + _page_length_slack(reference) >= reference


def _prepare_search_request(query: str, filters: SearchFilters) -> _AASearchRequest:
    """Build the query/filter parts that every page of one AA search shares."""
    query_html = quote(query)

    if filters.isbn:
        isbns = " || ".join([f"('isbn13:{isbn}' || 'isbn10:{isbn}')" for isbn in filters.isbn])
        query_html = quote(f"({isbns}) {query}")

    filters_query = ""

    for value in filters.lang or []:
        if value and value != "all":
            filters_query += f"&lang={quote(value)}"

    if filters.sort and filters.sort != "relevance":
        filters_query += f"&sort={quote(filters.sort)}"

    if filters.content:
        for value in filters.content:
            filters_query += f"&content={quote(value)}"

    formats_to_use = filters.format or _get_supported_formats()

    index = 1
    for filter_type, filter_values in vars(filters).items():
        if filter_type in ("author", "title") and filter_values:
            for value in filter_values:
                filters_query += f"&termtype_{index}={filter_type}&termval_{index}={quote(value)}"
                index += 1

    return _AASearchRequest(
        query_html=query_html,
        filters_query=filters_query,
        formats=formats_to_use,
        selector=network.AAMirrorSelector(),
    )


def _search_page_url(search_request: _AASearchRequest, page: int) -> str:
    """Build the AA search URL for one page (AA pages are 1-based)."""
    return (
        f"{network.get_aa_base_url()}"
        f"/search?index=&page={page}&display=table"
        f"&acc=aa_download&acc=external_download"
        f"&ext={'&ext='.join(search_request.formats)}"
        f"&q={search_request.query_html}"
        f"{search_request.filters_query}"
    )


def _fetch_search_page(url: str, selector: network.AAMirrorSelector) -> _AAPageFetch:
    """Fetch and parse exactly one AA result page. Never raises for an empty result."""
    # BYPASS-LEITER-FIX 2026-08-31: use_bypasser=True erzwang den Browser-Solve auf
    # JEDER Seite (~60s, Chrome-Neustart pro Page) und uebersprang damit die Leiter in
    # http.py: erst Plain-GET mit gecachten Bypass-Cookies, bei 403 Retry mit frischen
    # Cookies, erst dann Browser. AA sitzt inzwischen hinter DDoS-Guard statt
    # Cloudflare. allow_bypasser_fallback=True bleibt (Upstream-Default ist zwar True,
    # aber der urspruengliche Suchpfad setzte hier explizit False).
    html = downloader.html_get_page(url, selector=selector, allow_bypasser_fallback=True)
    if not html:
        return _AAPageFetch([], 0, "unreachable")

    if "No files found." in html:
        return _AAPageFetch([], 0, "no_files")

    soup = BeautifulSoup(_html_response_text(html), "html.parser")
    tbody = soup.find("table")

    if tbody is None:
        return _AAPageFetch([], 0, "no_table")
    if not isinstance(tbody, Tag):
        msg = f"Expected results table tag, got {type(tbody).__name__}"
        raise TypeError(msg)

    records: list[BrowseRecord] = []
    page_rows = 0
    for line_tr in tbody.find_all("tr"):
        # Count what AA SERVED, not what we managed to parse. _parse_search_result_row
        # legitimately returns None for ad rows and for rows with an empty publisher,
        # year or language span -- all common on AA. Counting parse successes would
        # turn a full page into a "short" one and end pagination after page 1.
        if _is_result_row(line_tr):
            page_rows += 1
        book = _parse_search_result_row(line_tr)
        if not book:
            continue
        records.append(book)

    return _AAPageFetch(records, page_rows, "ok")


def _sort_by_supported_format(records: list[BrowseRecord]) -> None:
    """Stable in-place sort putting preferred formats first, keeping AA's ranking."""
    supported_formats = _get_supported_formats()
    records.sort(
        key=lambda x: (
            supported_formats.index(x.format)
            if x.format in supported_formats
            else len(supported_formats)
        )
    )


def search_books_page(query: str, filters: SearchFilters, *, page: int = 1) -> AASearchPage:
    """Search for books, fetching exactly ONE result page.

    One call fetches one page, i.e. at most one DDoS-Guard browser solve (40-60s).
    This is the entry point for on-demand paging, where the user asks for each further
    page explicitly; search_books() keeps the multi-page loop for the flows that have
    no UI to page with.

    Args:
        query: Search term (ISBN, title, author, etc.)
        filters: Search filters (language, format, content type, etc.)
        page: 1-based AA result page to fetch.

    Returns:
        AASearchPage: the page's records plus has_more for the next page.

    Raises:
        SearchUnavailableError: If Anna's Archive cannot be reached -- on ANY page. A
            dead mirror must not look like the end of the list.
        RuntimeError: If page 1 has no results table at all.

    """
    page = max(1, page)
    search_request = _prepare_search_request(query, filters)
    fetch = _fetch_search_page(_search_page_url(search_request, page), search_request.selector)

    if fetch.status == "unreachable":
        msg = "Unable to reach download source. Network restricted or mirrors are blocked."
        raise SearchUnavailableError(msg)

    if fetch.status == "no_files":
        logger.info("No books found for query: %s (page %d)", query, page)
        return AASearchPage([], page, has_more=False, page_rows=0)

    if fetch.status == "no_table":
        # Page 1 without a table is a broken response; a later page without one is
        # simply the end of the list and must stay a normal, empty 200.
        if page == 1:
            logger.warning("No results table found for query: %s", query)
            msg = "No books found. Please try another query."
            raise RuntimeError(msg)
        logger.info("No results table on page %d, treating it as the end of the list", page)
        return AASearchPage([], page, has_more=False, page_rows=0)

    seen_ids: set[str] = set()
    records: list[BrowseRecord] = []
    for record in fetch.records:
        if record.id in seen_ids:
            continue
        seen_ids.add(record.id)
        records.append(record)

    # Ask before recording, otherwise the current page always matches itself.
    # Measured in rows AA SERVED, not in parsed records: a page whose rows all fail
    # the content checks (ads, missing spans) is still a full page with more behind it,
    # and `bool(records)` would end the list there without a word.
    has_more = _page_looks_full(fetch.page_rows)
    _record_observed_page_size(fetch.page_rows)
    _sort_by_supported_format(records)

    logger.info(
        "search_books_page: %d unique books on page %d for '%s' (has_more=%s)",
        len(records),
        page,
        query,
        has_more,
    )
    return AASearchPage(records, page, has_more, fetch.page_rows)


def search_books(query: str, filters: SearchFilters) -> list[BrowseRecord]:
    """Search for books matching the query, fetching up to AA_MAX_PAGES pages.

    Used by the flows without a pager UI (ISBN / title+author release search). The
    browse path fetches one page per request via search_books_page() instead.

    Args:
        query: Search term (ISBN, title, author, etc.)
        filters: Search filters (language, format, content type, etc.)

    Returns:
        List[BrowseRecord]: List of matching books

    Raises:
        SearchUnavailableError: If Anna's Archive cannot be reached
        Exception: If parsing fails

    """
    search_request = _prepare_search_request(query, filters)

    # PAGINATION-PATCH 2026-05-04: hole bis zu AA_MAX_PAGES Pages, dedupe per ID.
    # Eine Page, die deutlich kuerzer ist als die laengste bisher gesehene, ist die
    # letzte -- die Page-Groesse wird gemessen, nicht angenommen.
    max_pages = _int_setting("AA_MAX_PAGES", 5, minimum=1)
    # ZEITBUDGET 2026-08-31: jede Page hinter DDoS-Guard kann einen Browser-Solve
    # kosten (gemessen 20-67s pro Page). Ohne Budget laeuft eine breite Suche in den
    # Server-Timeout und der Client bekommt gar nichts; mit Budget kommen die bereits
    # geholten Seiten als Teilergebnis zurueck. Das Budget kann nur den START einer
    # weiteren Page verhindern, nie einen laufenden Solve abbrechen -- der Worst Case
    # bleibt Budget + eine Page-Dauer. 0 schaltet das Budget ab.
    search_budget = _int_setting("AA_SEARCH_BUDGET_SECONDS", 90, minimum=0)
    search_deadline = time.monotonic() + search_budget if search_budget > 0 else None

    all_books: list[BrowseRecord] = []
    seen_ids: set[str] = set()
    last_page_reached = 1
    # Longest page seen so far, measured in result rows AA actually rendered. AA's
    # page size is observed, never assumed, so the loop keeps working if AA changes
    # it (or serves a different size in display=table mode).
    max_page_rows = 0

    for page in range(1, max_pages + 1):
        # Page 1 is never budgeted: without it there is no result at all, and the
        # caller needs the SearchUnavailableError path rather than an empty list.
        if (
            page > 1
            and all_books
            and search_deadline is not None
            and time.monotonic() > search_deadline
        ):
            logger.info(
                "AA search budget of %ss exhausted, stopping pagination at %d results",
                search_budget,
                len(all_books),
            )
            break

        last_page_reached = page
        fetch = _fetch_search_page(_search_page_url(search_request, page), search_request.selector)

        if fetch.status == "unreachable":
            if page == 1:
                # Network/mirror exhaustion path bubbles up so API can notify clients
                msg = "Unable to reach download source. Network restricted or mirrors are blocked."
                raise SearchUnavailableError(msg)
            logger.info(
                "Page %d unreachable, stopping pagination at %d results", page, len(all_books)
            )
            break

        if fetch.status == "no_files":
            if page == 1:
                logger.info("No books found for query: %s", query)
                return []
            break

        if fetch.status == "no_table":
            if page == 1:
                logger.warning("No results table found for query: %s", query)
                msg = "No books found. Please try another query."
                raise RuntimeError(msg)
            break

        page_unique_count = 0
        for book in fetch.records:
            if book.id in seen_ids:
                continue
            seen_ids.add(book.id)
            all_books.append(book)
            page_unique_count += 1

        # A page that is clearly shorter than the longest page seen is the last page.
        # Without this every search paid one extra fetch of an empty page -- i.e. one
        # extra DDoS-Guard solve. The comparison is against the OBSERVED page length
        # plus slack, so neither AA's page size nor a stray unparsable row can cut a
        # search short.
        if page_unique_count == 0:
            break
        if fetch.page_rows + _page_length_slack(max_page_rows) < max_page_rows:
            logger.debug(
                "Page %d returned %d of %d rows, treating it as the last page",
                page,
                fetch.page_rows,
                max_page_rows,
            )
            break
        max_page_rows = max(max_page_rows, fetch.page_rows)
        _record_observed_page_size(fetch.page_rows)

    _sort_by_supported_format(all_books)

    logger.info(
        "search_books pagination: %d unique books across %d page(s) for '%s'",
        len(all_books),
        last_page_reached,
        query,
    )
    return all_books


def get_book_info(book_id: str, *, fetch_download_count: bool = True) -> BrowseRecord:
    """Get detailed information for a specific book.

    Args:
        book_id: Book identifier (MD5 hash)
        fetch_download_count: Whether to fetch download count from summary API.
            Only needed for display in DetailsModal, not for downloads.

    Returns:
        BrowseRecord: Detailed book information including download URLs

    """
    url = f"{network.get_aa_base_url()}/md5/{book_id}"
    selector = network.AAMirrorSelector()
    # BYPASS-LEITER-FIX 2026-08-31: siehe search_books
    html = downloader.html_get_page(url, selector=selector, allow_bypasser_fallback=True)

    if not html:
        msg = "Unable to reach download source. Network restricted or mirrors are blocked."
        raise SearchUnavailableError(msg)

    soup = BeautifulSoup(_html_response_text(html), "html.parser")

    return _parse_book_info_page(soup, book_id, fetch_download_count=fetch_download_count)


def _is_result_row(row: Tag) -> bool:
    """Whether AA rendered this <tr> as a result row.

    Structural gate only -- the same one _parse_search_result_row() applies, minus its
    content checks. Used to measure how long a page is: over-counting costs at most one
    extra fetch, under-counting would silently truncate the search.
    """
    return len(row.find_all("td")) >= _RESULT_ROW_MIN_CELLS and bool(row.find_all("a", href=True))


def _parse_search_result_row(row: Tag) -> BrowseRecord | None:
    """Parse a single search result row into a browse record."""
    try:
        if row.text.strip().lower().startswith("your ad here"):
            return None

        cells = row.find_all("td")
        anchors = row.find_all("a", href=True)
        if len(cells) < _RESULT_ROW_MIN_CELLS or not anchors:
            return None

        record_id = (_get_attr(anchors[0], "href") or "").split("/")[-1]
        if not record_id:
            return None

        preview_img = cells[0].find("img")
        preview = _get_attr(preview_img, "src") if isinstance(preview_img, Tag) else None

        title = _first_stripped_text(cells[1].find("span"))
        author = _first_stripped_text(cells[2].find("span"))
        publisher = _first_stripped_text(cells[3].find("span"))
        year = _first_stripped_text(cells[4].find("span"))
        language = _first_stripped_text(cells[7].find("span"))
        content = _first_stripped_text(cells[8].find("span"))
        file_format = _first_stripped_text(cells[9].find("span"))
        size = _first_stripped_text(cells[10].find("span"))

        if (
            title is None
            or author is None
            or publisher is None
            or year is None
            or language is None
            or content is None
            or file_format is None
            or size is None
        ):
            return None

        return BrowseRecord(
            id=record_id,
            title=title,
            source="direct_download",
            preview=preview,
            author=author,
            publisher=publisher,
            year=year,
            language=language,
            content=content.lower() if content else None,
            format=file_format.lower() if file_format else None,
            size=size,
        )
    except (AttributeError, IndexError, KeyError, TypeError) as e:
        logger.error_trace(f"Error parsing search result row: {e}")
        return None


def _parse_book_info_page(
    soup: BeautifulSoup,
    book_id: str,
    *,
    fetch_download_count: bool = True,
) -> BrowseRecord:
    """Parse the book info page HTML into a browse record."""
    data = soup.select_one("body > main > div:nth-of-type(1)")

    if not data:
        msg = f"Failed to parse book info for ID: {book_id}"
        raise RuntimeError(msg)

    preview: str = ""

    node = data.select_one("div:nth-of-type(1) > img")
    if isinstance(node, Tag):
        preview = _get_attr(node, "src") or ""

    main_inner = next(
        (tag for tag in soup.find_all("div", {"class": "main-inner"}) if isinstance(tag, Tag)),
        None,
    )
    if main_inner is None:
        msg = f"Failed to parse book details for ID: {book_id}"
        raise RuntimeError(msg)

    details_container = main_inner.find_next("div")
    if not isinstance(details_container, Tag):
        msg = f"Expected details container tag for book ID {book_id}, got {type(details_container).__name__}"
        raise TypeError(msg)

    original_nodes = list(details_container.children)
    divs = [node for node in original_nodes if isinstance(node, Tag)]

    slow_urls_no_waitlist: set[str] = set()
    slow_urls_with_waitlist: set[str] = set()

    for anchor in soup.find_all("a"):
        try:
            text = anchor.text.strip().lower()
            href = _get_attr(anchor, "href")
            if not href:
                continue

            next_text = ""
            next_elements = anchor.next_elements
            next(next_elements, None)
            second_next = next(next_elements, None)
            if second_next is not None:
                next_text = (
                    second_next.get_text(strip=True).lower()
                    if isinstance(second_next, Tag)
                    else str(second_next).strip().lower()
                )

            if text.startswith("slow partner server") and "waitlist" in next_text:
                if "no waitlist" in next_text:
                    slow_urls_no_waitlist.add(href)
                else:
                    slow_urls_with_waitlist.add(href)
        except AttributeError, TypeError:
            pass

    logger.debug(
        "Source inventory for %s -> aa_no_wait=%d, aa_wait=%d",
        book_id,
        len(slow_urls_no_waitlist),
        len(slow_urls_with_waitlist),
    )

    # Convert to absolute URLs and tag by source type
    base_url = network.get_aa_base_url()
    urls = []

    for rel_url in slow_urls_no_waitlist:
        abs_url = downloader.get_absolute_url(base_url, rel_url)
        if abs_url:
            urls.append(abs_url)
            _url_source_types[abs_url] = "aa-slow-nowait"

    for rel_url in slow_urls_with_waitlist:
        abs_url = downloader.get_absolute_url(base_url, rel_url)
        if abs_url:
            urls.append(abs_url)
            _url_source_types[abs_url] = "aa-slow-wait"

    divs = [div for div in divs if div.get_text(strip=True)]

    all_details = _find_in_divs(divs, " · ")
    file_format = ""
    size = ""
    content = ""
    supported_formats = _get_supported_formats()

    for _details in all_details:
        _details = _details.split(" · ")
        for f in _details:
            stripped_lower = f.strip().lower()
            if file_format == "" and stripped_lower in supported_formats:
                file_format = f.strip().lower()
            if size == "" and any(u in f.strip().lower() for u in ("mb", "kb", "gb")):
                size = _normalize_size(f)
            if content == "":
                for ct in CONTENT_TYPES:
                    if ct in f.strip().lower():
                        content = ct
                        break
        if file_format == "" or size == "":
            for f in _details:
                stripped = f.strip().lower()
                if file_format == "" and stripped and " " not in stripped:
                    file_format = stripped
                if size == "" and "." in stripped:
                    size = _normalize_size(f)

    book_title = (_find_in_divs(divs, "🔍") or [""])[0].strip("🔍").strip()

    # Extract basic information
    description = _extract_book_description(soup)

    book_info = BrowseRecord(
        id=book_id,
        title=book_title,
        source="direct_download",
        preview=preview,
        content=content,
        publisher=(_find_in_divs(divs, "icon-[mdi--company]", is_class=True) or [""])[0],
        author=(_find_in_divs(divs, "icon-[mdi--user-edit]", is_class=True) or [""])[0],
        format=file_format,
        size=size,
        description=description,
        download_urls=urls,
    )

    # Extract additional metadata
    metadata_node = original_nodes[-6]
    if not isinstance(metadata_node, Tag):
        msg = f"Expected metadata container tag for book ID {book_id}, got {type(metadata_node).__name__}"
        raise TypeError(msg)
    info = _extract_book_metadata(metadata_node)

    if fetch_download_count:
        try:
            summary_url = f"{network.get_aa_base_url()}/dyn/md5/summary/{book_id}"
            summary_response = downloader.html_get_page(
                summary_url, selector=network.AAMirrorSelector(), allow_bypasser_fallback=True
            )
            if summary_response:
                summary_data = json.loads(_html_response_text(summary_response))
                if "downloads_total" in summary_data:
                    info["Downloads"] = [str(summary_data["downloads_total"])]
        except (
            SearchUnavailableError,
            RuntimeError,
            json.JSONDecodeError,
            TypeError,
            KeyError,
            AttributeError,
        ) as e:
            logger.debug("Failed to fetch download count for %s: %s", book_id, e)

    book_info.info = info

    # Set language and year from metadata if available
    if info.get("Language"):
        book_info.language = info["Language"][0]
    if info.get("Year"):
        book_info.year = info["Year"][0]

    # Set source URL for linking back to Anna's Archive
    book_info.source_url = f"{network.get_aa_base_url()}/md5/{book_id}"

    return book_info


def _find_in_divs(divs: list[Tag], text: str, *, is_class: bool = False) -> list[str]:
    """Find divs containing text or having a specific class."""
    results: list[str] = []
    for div in divs:
        if is_class:
            if div.find(class_=text):
                results.append(div.text.strip())
        elif text in div.text.strip():
            results.append(div.text.strip())
    return results


def _get_next_value_div(label_div: Tag) -> Tag | None:
    """Find the next sibling div that holds the value for a metadata label."""
    sibling = label_div.next_sibling
    while sibling:
        if isinstance(sibling, Tag) and sibling.name == "div":
            return sibling
        sibling = sibling.next_sibling
    return None


def _extract_book_description(soup: BeautifulSoup) -> str | None:
    """Extract the primary or alternative description from the book page."""
    container = soup.select_one(".js-md5-top-box-description")
    if not container:
        return None

    alternative: str | None = None

    label_divs = container.select("div.text-xs.text-gray-500.uppercase")
    for label_div in label_divs:
        label_text = label_div.get_text(strip=True).lower()
        value_div = _get_next_value_div(label_div)
        if not value_div:
            continue

        value_text = value_div.get_text(separator=" ", strip=True)
        if not value_text:
            continue

        if label_text == "description":
            return value_text
        if label_text == "alternative description" and not alternative:
            alternative = value_text

    if alternative:
        return alternative

    # Fallback to the first text block inside the description container
    fallback_div = container.find("div", class_="mb-1")
    if fallback_div:
        fallback_text = fallback_div.get_text(separator=" ", strip=True)
        if fallback_text:
            return fallback_text

    return None


def _extract_book_metadata(metadata_divs: Tag) -> dict[str, list[str]]:
    """Extract metadata from book info divs."""
    info: dict[str, set[str]] = {}

    sub_datas = metadata_divs.find_all("div")[0]
    for sub_data in _iter_child_tags(sub_datas):
        if sub_data.get_text(strip=True) == "":
            continue
        children = list(_iter_child_tags(sub_data))
        key = children[0].get_text(strip=True)
        value = children[1].get_text(strip=True)
        if key not in info:
            info[key] = set()
        info[key].add(value)

    relevant_prefixes = ("isbn-", "alternative", "asin", "goodreads", "language", "year")
    return {
        k.strip(): list(v)
        for k, v in info.items()
        if k.lower().startswith(relevant_prefixes) and "filename" not in k.lower()
    }


def _get_source_info(link: str) -> tuple[str, str]:
    """Get source label and friendly name for a download link.

    Args:
        link: Download URL

    Returns:
        Tuple of (log_label, friendly_name)

    """
    # Check detailed source type mapping first (for AA slow distinction)
    if link in _url_source_types:
        detailed_label = _url_source_types[link]
        for log_label, friendly_name, _ in _DOWNLOAD_SOURCES:
            if log_label == detailed_label:
                return log_label, friendly_name

    for log_label, friendly_name, patterns in _DOWNLOAD_SOURCES:
        if patterns and any(pattern in link for pattern in patterns):
            return log_label, friendly_name
    return "unknown", "Mirror"


def _friendly_source_name(link: str) -> str:
    """Get user-friendly name for a download source."""
    return _get_source_info(link)[1]


def _group_urls_by_source(urls: list[str], urls_by_source: dict[str, list[str]]) -> None:
    """Group URLs into urls_by_source dict by their source type."""
    for url in urls:
        source_type = _url_source_types.get(url)
        if source_type:
            urls_by_source.setdefault(source_type, []).append(url)


def _fetch_aa_page_urls(book_info: BrowseRecord, urls_by_source: dict[str, list[str]]) -> None:
    """Fetch and parse AA page, populating urls_by_source dict.

    Groups existing book_info.download_urls by source type. If book_info
    has no URLs, fetches the AA page fresh.
    """
    if book_info.download_urls:
        _group_urls_by_source(book_info.download_urls, urls_by_source)
        return

    try:
        fresh_book_info = get_book_info(book_info.id, fetch_download_count=False)
        _group_urls_by_source(fresh_book_info.download_urls, urls_by_source)
    except (SearchUnavailableError, RuntimeError, TypeError, AttributeError) as e:
        logger.warning("Failed to fetch AA page: %s", e)


def _get_urls_for_source(
    source_id: str,
    book_info: BrowseRecord,
    selector: network.AAMirrorSelector,
    cancel_flag: Event | None,
    status_callback: Callable[[str, str | None], None] | None,
    urls_by_source: dict[str, list[str]],
) -> list[str]:
    """Get URLs for a specific source, fetching lazily if needed."""
    # AA Fast - generate URL dynamically
    if source_id == "aa-fast":
        if not config.AA_DONATOR_KEY:
            return []
        url = f"{network.get_aa_base_url()}/dyn/api/fast_download.json?md5={book_info.id}&key={config.AA_DONATOR_KEY}"
        _url_source_types[url] = "aa-fast"
        return [url]

    # MD5-based sources - generate URL from template
    template = _get_md5_url_template(source_id)
    if template:
        url = template.format(md5=book_info.id)
        _url_source_types[url] = source_id
        return [url]

    if source_id == "libgen":
        urls = []
        for base_url in _get_libgen_domains():
            url = f"{base_url}/ads.php?md5={book_info.id}"
            _url_source_types[url] = "libgen"
            urls.append(url)
        return urls

    # Welib - fetch page and parse for slow_download links
    if source_id == "welib":
        if status_callback:
            status_callback("resolving", "Fetching welib sources")
        return _get_download_urls_from_welib(
            book_info.id,
            selector=selector,
            cancel_flag=cancel_flag,
            status_callback=status_callback,
        )

    # AA page sources - fetch AA page if not already done
    if source_id in _AA_PAGE_SOURCES:
        if not urls_by_source:
            if status_callback:
                status_callback("resolving", "Fetching download sources")
            _fetch_aa_page_urls(book_info, urls_by_source)

        return urls_by_source.get(source_id, [])

    return []


def _try_download_url(
    url: str,
    source_id: str,
    book_info: BrowseRecord,
    book_path: Path,
    progress_callback: Callable[[float], None] | None,
    cancel_flag: Event | None,
    status_callback: Callable[[str, str | None], None] | None,
    selector: network.AAMirrorSelector,
    source_context: str,
) -> str | None:
    """Attempt to download from a single URL.

    Returns: download URL on success, None on failure.
    """
    try:
        logger.info("Trying download source [%s]: %s", source_id, url)

        if status_callback:
            status_callback("resolving", f"Trying {source_context}")

        download_url = _get_download_url(
            url, book_info.title, cancel_flag, status_callback, selector, source_context
        )
        if not download_url:
            _raise_runtime_error("No download URL resolved")

        logger.info("Resolved download URL [%s]: %s", source_id, download_url)

        data = downloader.download_url(
            download_url,
            book_info.size or "",
            progress_callback,
            cancel_flag,
            selector,
            status_callback,
            referer=url,
        )

        if not data:
            _raise_runtime_error("No data received from download")

        file_size = data.tell()
        if file_size < _MIN_VALID_FILE_SIZE:
            logger.warning("Downloaded file too small (%s bytes), likely an error page", file_size)
            _raise_runtime_error(f"File too small ({file_size} bytes)")

        logger.debug("Download finished (%s bytes). Writing to %s", file_size, book_path)
        data.seek(0)
        with book_path.open("wb") as f:
            f.write(data.getbuffer())

    except (
        RuntimeError,
        requests.exceptions.RequestException,
        OSError,
        KeyError,
        ValueError,
        TypeError,
        AttributeError,
    ) as e:
        logger.warning("Failed to download from %s (source=%s): %s", url, source_id, e)
        return None
    else:
        return download_url


def _get_download_urls_from_welib(
    book_id: str,
    selector: network.AAMirrorSelector | None = None,
    cancel_flag: Event | None = None,
    status_callback: Callable[[str, str | None], None] | None = None,
) -> list[str]:
    """Get download URLs from welib.org (bypasser required)."""
    from shelfmark.core import mirrors

    if not _is_source_enabled("welib"):
        return []
    template = mirrors.get_welib_url_template()
    if not template:
        return []
    url = template.format(md5=book_id)
    logger.info("Fetching welib download URLs for %s", book_id)
    try:
        html = downloader.html_get_page(
            url,
            use_bypasser=True,
            selector=selector or network.AAMirrorSelector(),
            cancel_flag=cancel_flag,
            status_callback=status_callback,
        )
    except (
        SearchUnavailableError,
        requests.exceptions.RequestException,
        RuntimeError,
        ValueError,
        TypeError,
        AttributeError,
    ) as exc:
        logger.error_trace(f"Welib fetch failed for {book_id}: {exc}")
        return []
    if not html:
        logger.warning("Welib page empty for %s", book_id)
        return []

    soup = BeautifulSoup(_html_response_text(html), "html.parser")
    links = [
        downloader.get_absolute_url(url, href)
        for a in soup.find_all("a", href=True)
        if (href := _get_attr(a, "href")) and "/slow_download/" in href
    ]
    return list(dict.fromkeys(links))  # Dedupe while preserving order


def _extract_libgen_download_url(link: str, cancel_flag: Event | None = None) -> str:
    """Extract download URL from Libgen ads.php page using direct HTTP."""
    if cancel_flag and cancel_flag.is_set():
        return ""

    base_url = "/".join(link.split("/")[:3])
    logger.debug("Libgen fast: trying %s", link)

    try:
        response = requests.get(
            link,
            headers=downloader.DOWNLOAD_HEADERS,
            timeout=(5, 10),
            allow_redirects=True,
            proxies=network.get_proxies(link),
            verify=network.get_ssl_verify(link),
        )

        if response.status_code != HTTPStatus.OK:
            logger.debug("Libgen fast: %s returned %s", link, response.status_code)
            return ""

        html = response.text
        final_url = response.url

        if "libgen" not in final_url.lower() and "ads.php" not in final_url.lower():
            logger.debug("Libgen fast: redirected away to %s", final_url)
            return ""

        if "get.php" not in html:
            logger.debug("Libgen fast: page doesn't contain get.php")
            return ""

        download_url = None
        for pattern in _LIBGEN_GET_PATTERNS:
            match = pattern.search(html)
            if match:
                download_url = (
                    match.group(1).replace("&amp;", "&").replace("&gt;", ">").replace("&lt;", "<")
                )
                break

        if not download_url:
            logger.debug("Libgen fast: couldn't extract GET link")
            return ""
        if not download_url.startswith("http"):
            download_url = f"{base_url}/{download_url.lstrip('/')}"

        logger.debug("Libgen fast: extracted %s", download_url)
    except requests.exceptions.RequestException as e:
        logger.debug("Libgen fast: request failed: %s", e)
        return ""
    except (AttributeError, TypeError, ValueError) as e:
        logger.warning("Libgen fast: unexpected error: %s", e)
        return ""
    else:
        return download_url


def _download_book(
    book_info: BrowseRecord,
    book_path: Path,
    progress_callback: Callable[[float], None] | None = None,
    cancel_flag: Event | None = None,
    status_callback: Callable[[str, str | None], None] | None = None,
) -> str | None:
    """Download a book using sources in configured priority order.

    Returns: Download URL if successful, None otherwise.
    """
    selector = network.AAMirrorSelector()
    source_failures: dict[str, int] = {}
    urls_by_source: dict[str, list[str]] = {}
    url_attempt_counter = 0

    # Get enabled sources in priority order
    priority = [s for s in _get_source_priority() if s.get("enabled", True)]

    for source_config in priority:
        source_id = source_config["id"]

        if cancel_flag and cancel_flag.is_set():
            return None

        # Debug: skip sources for testing fallback chains
        if source_id in DEBUG_SKIP_SOURCES:
            logger.info("DEBUG_SKIP_SOURCES: skipping %s", source_id)
            continue

        # Skip if source requires CF bypass and it's not enabled
        if source_id in _CF_BYPASS_REQUIRED and not config.USE_CF_BYPASS:
            logger.debug("Skipping %s - requires CF bypass", source_id)
            continue

        # Skip if source has failed too many times
        if source_failures.get(source_id, 0) >= _SOURCE_FAILURE_THRESHOLD:
            logger.debug("Skipping %s - too many failures", source_id)
            continue

        # Get URLs for this source (lazy-loads as needed)
        urls_to_try = _get_urls_for_source(
            source_id,
            book_info,
            selector,
            cancel_flag,
            status_callback,
            urls_by_source,
        )

        if not urls_to_try:
            continue

        # Apply round-robin rotation if multiple URLs
        if len(urls_to_try) > 1:
            rotation_value = next(_aa_slow_rotation)
            rotation = rotation_value % len(urls_to_try)
            urls_to_try = urls_to_try[rotation:] + urls_to_try[:rotation]
            if rotation:
                logger.debug("Rotated %s URLs by %s", source_id, rotation)

        # Try each URL for this source
        for url in urls_to_try:
            if cancel_flag and cancel_flag.is_set():
                return None

            if source_id == "libgen":
                source_context = "Libgen (Fast)"
            else:
                url_attempt_counter += 1
                friendly_name = _friendly_source_name(url)
                source_context = f"{friendly_name} (Server #{url_attempt_counter})"

            result = _try_download_url(
                url,
                source_id,
                book_info,
                book_path,
                progress_callback,
                cancel_flag,
                status_callback,
                selector,
                source_context,
            )

            if result:
                return result

            source_failures[source_id] = source_failures.get(source_id, 0) + 1

            # Check if we've hit the failure threshold
            if source_failures[source_id] >= _SOURCE_FAILURE_THRESHOLD:
                logger.info("Source %s hit failure threshold, moving to next source", source_id)
                break

    if status_callback:
        status_callback("error", "All sources failed")
    return None


def _get_download_url(
    link: str,
    title: str,
    cancel_flag: Event | None = None,
    status_callback: Callable[[str, str | None], None] | None = None,
    selector: network.AAMirrorSelector | None = None,
    source_context: str | None = None,
) -> str:
    """Extract actual download URL from various source pages.

    Args:
        link: URL to extract download link from
        title: Book title for logging
        cancel_flag: Optional cancellation flag
        status_callback: Optional callback for status updates
        selector: Optional AA mirror selector
        source_context: Optional context string like "Welib (1/12)" for status messages

    """
    sel = selector or network.AAMirrorSelector()

    # AA fast download API (JSON response)
    if link.startswith(f"{network.get_aa_base_url()}/dyn/api/fast_download.json"):
        page = downloader.html_get_page(
            link, selector=sel, cancel_flag=cancel_flag, status_callback=status_callback
        )
        page_data = json.loads(_html_response_text(page))
        download_url = page_data.get("download_url", "")
        return (
            downloader.get_absolute_url(link, download_url) if isinstance(download_url, str) else ""
        )

    if "/ads.php?md5=" in link and any(domain in link for domain in _get_libgen_domains()):
        return _extract_libgen_download_url(link, cancel_flag)

    html = downloader.html_get_page(
        link, selector=sel, cancel_flag=cancel_flag, status_callback=status_callback
    )
    if not html:
        return ""

    soup = BeautifulSoup(_html_response_text(html), "html.parser")
    url = ""

    # Z-Library
    if _is_configured_zlib_link(link):
        dl = soup.find("a", href=True, class_="addDownloadedBook")
        if not dl:
            # Retry after delay if page not fully loaded
            time.sleep(2)
            html = downloader.html_get_page(
                link, selector=sel, cancel_flag=cancel_flag, status_callback=status_callback
            )
            if html:
                soup = BeautifulSoup(_html_response_text(html), "html.parser")
                dl = soup.find("a", href=True, class_="addDownloadedBook")
        url = (_get_attr(dl, "href") or "") if isinstance(dl, Tag) else ""

    # AA slow download / partner servers
    elif "/slow_download/" in link:
        url = _extract_slow_download_url(
            soup, link, title, cancel_flag, status_callback, sel, source_context
        )

    else:
        get_btn = _find_first_anchor_with_text(soup, "GET") or _find_first_anchor_with_text(
            soup, "Download"
        )
        if get_btn:
            url = _get_attr(get_btn, "href") or ""
        else:
            logger.warning("Unknown source type, couldn't find download link: %s", link)
            url = ""

    return downloader.get_absolute_url(link, url)


def _extract_slow_download_url(
    soup: BeautifulSoup,
    link: str,
    title: str,
    cancel_flag: Event | None,
    status_callback: Callable[[str, str | None], None] | None,
    selector: network.AAMirrorSelector,
    source_context: str | None = None,
) -> str:
    """Extract download URL from AA slow download pages."""
    html_str = str(soup)

    clipboard_match = re.search(r"navigator\.clipboard\.writeText\(['\"]([^'\"]+)['\"]\)", html_str)
    if clipboard_match:
        url = clipboard_match.group(1)
        if url.startswith("http") and "/slow_download/" not in url:
            return url

    dl_link = _find_first_anchor_with_text(soup, "📚 Download now") or _find_first_anchor_with_text(
        soup, "Download now", contains=True
    )
    if dl_link:
        return _get_attr(dl_link, "href") or ""

    for a_tag in soup.find_all("a", href=True):
        if a_tag.has_attr("download"):
            href = _get_attr(a_tag, "href")
            if not href:
                continue
            if href.startswith("http") and "/slow_download/" not in href:
                return href

    for span in soup.find_all("span"):
        if not _tag_has_class_containing(span, "whitespace-normal"):
            continue
        text = span.get_text(strip=True)
        if text.startswith(("http://", "https://")) and "/slow_download/" not in text:
            return text

    for span in soup.find_all("span"):
        if not _tag_has_class_containing(span, "bg-gray-200"):
            continue
        text = span.get_text(strip=True)
        if text.startswith(("http://", "https://")):
            return text

    location_match = re.search(r"window\.location\.href\s*=\s*['\"]([^'\"]+)['\"]", html_str)
    if location_match:
        url = location_match.group(1)
        if url.startswith("http") and "/slow_download/" not in url:
            return url

    copy_text = _find_text_node(soup, "copy this url")
    if copy_text and copy_text.parent:
        parent = copy_text.parent
        next_link = parent.find_next("a", href=True)
        if isinstance(next_link, Tag):
            next_href = _get_attr(next_link, "href")
            if next_href:
                return next_href
        code_elem = parent.find_next("code")
        if isinstance(code_elem, Tag):
            return code_elem.get_text(strip=True)
        for sibling in parent.find_next_siblings():
            text = (
                sibling.get_text(strip=True) if isinstance(sibling, Tag) else str(sibling).strip()
            )
            if text.startswith("http"):
                return text

    countdown_seconds = _extract_countdown_seconds(soup, html_str)
    if countdown_seconds > 0:
        max_countdown_seconds = 600
        sleep_time = min(countdown_seconds, max_countdown_seconds)
        if countdown_seconds > max_countdown_seconds:
            logger.warning(
                "Countdown %ss exceeds max, capping at %ss",
                countdown_seconds,
                max_countdown_seconds,
            )
        logger.info("AA waitlist: %ss for %s", sleep_time, title)

        # Live countdown with status updates
        for remaining in range(sleep_time, 0, -1):
            wait_msg = (
                f"{source_context} - Waiting {remaining}s"
                if source_context
                else f"Waiting {remaining}s"
            )
            if status_callback:
                status_callback("resolving", wait_msg)

            # Wait 1 second (or until cancelled)
            if cancel_flag and cancel_flag.wait(timeout=1):
                logger.info("Cancelled wait for %s", title)
                return ""

        # After countdown, update status and re-fetch
        if status_callback and source_context:
            status_callback("resolving", f"{source_context} - Fetching")

        return _get_download_url(
            link, title, cancel_flag, status_callback, selector, source_context
        )

    link_texts = [a.get_text(strip=True)[:50] for a in soup.find_all("a", href=True)[:10]]
    logger.warning("No download URL found. First 10 links: %s", link_texts)
    return ""


def _extract_countdown_seconds(soup: BeautifulSoup, html_str: str) -> int:
    """Extract countdown timer seconds from AA slow download page."""
    countdown_elem = soup.find("span", class_="js-partner-countdown")
    if isinstance(countdown_elem, Tag):
        seconds = _parse_countdown_seconds_from_element(countdown_elem)
        if seconds is not None:
            return seconds

    for elem in soup.find_all(["span", "div"]):
        if not (
            _tag_has_class_containing(elem, "timer") or _tag_has_class_containing(elem, "countdown")
        ):
            continue
        seconds = _parse_countdown_seconds_from_element(elem)
        if seconds is not None:
            return seconds

    countdown_attr = re.search(r'data-countdown=["\'](\d+)["\']', html_str)
    if countdown_attr:
        seconds = int(countdown_attr.group(1))
        if 0 < seconds < _AA_COUNTDOWN_MAX_SECONDS:
            return seconds

    js_countdown = re.search(r"countdown:\s*(\d+)", html_str)
    if js_countdown:
        seconds = int(js_countdown.group(1))
        if 0 < seconds < _AA_COUNTDOWN_MAX_SECONDS:
            return seconds
    js_var = re.search(r"(?:var|let|const)\s+countdown\s*=\s*(\d+)", html_str)
    if js_var:
        seconds = int(js_var.group(1))
        if 0 < seconds < _AA_COUNTDOWN_MAX_SECONDS:
            return seconds

    countdown_secs = re.search(r"countdownSeconds\s*=\s*(\d+)", html_str)
    if countdown_secs:
        seconds = int(countdown_secs.group(1))
        if 0 < seconds < _AA_COUNTDOWN_MAX_SECONDS:
            return seconds

    json_countdown = re.search(r'["\']countdown[_-]?seconds["\']\s*:\s*(\d+)', html_str)
    if json_countdown:
        seconds = int(json_countdown.group(1))
        if 0 < seconds < _AA_COUNTDOWN_MAX_SECONDS:
            return seconds

    wait_text = re.search(r"wait\s+(\d+)\s+seconds", html_str, re.IGNORECASE)
    if wait_text:
        seconds = int(wait_text.group(1))
        if 0 < seconds < _AA_COUNTDOWN_MAX_SECONDS:
            return seconds

    return 0


def _parse_countdown_seconds_from_element(element: Tag) -> int | None:
    """Parse an integer countdown from a tag, returning None when invalid."""
    try:
        seconds = int(element.get_text(strip=True))
    except ValueError, TypeError:
        return None

    if 0 < seconds < _AA_COUNTDOWN_MAX_SECONDS:
        return seconds
    return None


def _browse_record_to_release(record: BrowseRecord) -> Release:
    """Convert a browse record to a Release object.

    This bridges the direct source's browse data to the generic release model.
    """
    return Release(
        source=record.source,
        source_id=record.id,
        title=record.title,
        format=record.format,
        language=record.language,  # Top-level language for filtering
        size=record.size,
        download_url=record.download_urls[0] if record.download_urls else None,
        info_url=f"{network.get_aa_base_url()}/md5/{record.id}",
        protocol=ReleaseProtocol.HTTP,
        indexer="Direct Download",
        content_type=record.content,  # Preserve content type from source
        extra={
            "author": record.author,
            "publisher": record.publisher,
            "year": record.year,
            "language": record.language,
            "preview": record.preview,
            "description": record.description,
            "download_urls": record.download_urls,
            "info": record.info,
        },
    )


@register_source("direct_download")
class DirectDownloadSource(ReleaseSource):
    """Direct download source - searches web sources for books.

    This wraps the search_books() functionality to provide releases
    via the plugin interface.
    """

    name = "direct_download"
    display_name = "Direct Download"
    supported_content_types: ClassVar[list[str]] = ["ebook"]  # Direct downloads only support ebooks

    def __init__(self) -> None:
        """Initialize per-instance search state for direct downloads."""
        # Tracks which search method was used in the last search() call
        # "isbn" = ISBN search returned results, "title_author" = title+author was used
        self._last_search_type: str = "title_author"
        # Paging state of the last browse search, reported to the API so the client
        # can offer "load more". get_source() builds a fresh instance per request, so
        # this never leaks between requests.
        self._last_search_page: int = 1
        self._last_search_has_more: bool = False

    @property
    def last_search_type(self) -> str:
        """Returns the search type used in the last search() call."""
        return self._last_search_type

    @property
    def last_search_page(self) -> int:
        """Returns the result page fetched in the last search() call."""
        return self._last_search_page

    @property
    def last_search_has_more(self) -> bool:
        """Whether another result page can be requested after the last search() call."""
        return self._last_search_has_more

    def get_column_config(self) -> ReleaseColumnConfig:
        """Column configuration for Direct Download source.

        Shows language, format, and size badges for each release.
        Language is hidden on mobile; format and size are shown.
        """
        return ReleaseColumnConfig(
            columns=[
                ColumnSchema(
                    key="extra.language",
                    label="Language",
                    render_type=ColumnRenderType.BADGE,
                    align=ColumnAlign.CENTER,
                    width="60px",
                    hide_mobile=False,  # Language shown on mobile
                    color_hint=ColumnColorHint(type="map", value="language"),
                    uppercase=True,
                ),
                ColumnSchema(
                    key="format",
                    label="Format",
                    render_type=ColumnRenderType.BADGE,
                    align=ColumnAlign.CENTER,
                    width="80px",
                    hide_mobile=False,  # Format shown on mobile
                    color_hint=ColumnColorHint(type="map", value="format"),
                    uppercase=True,
                ),
                ColumnSchema(
                    key="size",
                    label="Size",
                    render_type=ColumnRenderType.SIZE,
                    align=ColumnAlign.CENTER,
                    width="80px",
                    hide_mobile=False,  # Size shown on mobile
                ),
            ],
            grid_template="minmax(0,2fr) 60px 80px 80px",
            supported_filters=["format", "language"],  # AA has reliable language metadata
        )

    def get_record(
        self,
        record_id: str,
        *,
        fetch_download_count: bool = True,
    ) -> BrowseRecord | None:
        """Resolve a direct-download record for direct-mode info/download flows."""
        _ensure_direct_download_available()
        return get_book_info(record_id, fetch_download_count=fetch_download_count)

    def search_results_are_releases(self) -> bool:
        """Direct search results already represent concrete downloadable releases."""
        return True

    def get_destination_override(self, task: DownloadTask) -> Path | None:
        """Apply Anna's Archive content-type routing when configured."""
        if check_audiobook(task.content_type):
            return None
        return get_aa_content_type_dir(task.content_type)

    def _search_page_with_language_fallback(
        self,
        query: str,
        filters: SearchFilters,
        *,
        page: int,
    ) -> tuple[AASearchPage, bool]:
        """Fetch one browse page, retrying page 1 without the language filter if empty.

        The retry is restricted to page 1 on purpose: on a later page it would cost a
        second DDoS-Guard solve AND continue a different query than the pages already
        shown. Returns the page plus whether the fallback produced the records.
        """
        result = search_books_page(query, filters, page=page)
        if result.records or page > 1 or not filters.lang:
            return result, False

        logger.debug(
            "No manual results with langs=%s, retrying without language filter",
            filters.lang,
        )
        fallback = search_books_page(query, replace(filters, lang=None), page=page)
        return fallback, bool(fallback.records)

    def search(
        self,
        book: BookMetadata,
        plan: ReleaseSearchPlan,
        *,
        expand_search: bool = False,
        content_type: str = "ebook",
    ) -> list[Release]:
        """Search for releases using the book's metadata.

        Priority: ISBN search first (most precise), then title+author fallback.
        For non-English languages, uses localized titles from book.titles_by_language.

        Args:
            book: Book metadata from provider
            plan: Precomputed search plan with normalized queries and filters.
            expand_search: If True, skip ISBN and use title+author directly
            languages: Language codes to filter by (overrides book.language/config)
            content_type: Ignored - Direct download uses format filtering instead

        """
        _ensure_direct_download_available()
        lang_filter = plan.languages

        # Reset search type tracking
        self._last_search_type = "title_author"
        self._last_search_page = 1
        self._last_search_has_more = False

        if plan.source_filters is not None:
            query = plan.manual_query or ""
            logger.debug(
                "Searching direct_download: source_query='%s', langs=%s", query, lang_filter
            )
            filters = plan.source_filters or SearchFilters()
            filters.lang = lang_filter if lang_filter is not None else (filters.lang or [])
            # The browse path fetches exactly ONE page per request: every page costs a
            # DDoS-Guard browser solve, so further pages are only fetched when the user
            # asks for them.
            #
            # Deliberately NOT AA_MAX_PAGES. That key is the loop bound of the pagerless
            # release search (search_books below), where every extra page is spent
            # unasked and where production keeps it at 1 to stay fast. Reusing it here
            # would make the two settings fight: AA_MAX_PAGES=1 clamps page to 1, so
            # `page < max_pages` is 1 < 1 and has_more can never be true -- the Load
            # More button would never appear. AA_BROWSE_MAX_PAGES is only a guard rail
            # against a client jumping to page 400; each page here is still one request.
            browse_max_page = _int_setting(
                "AA_BROWSE_MAX_PAGES", _AA_BROWSE_MAX_PAGES_DEFAULT, minimum=1
            )
            # getattr, not plan.page: this module is ALSO deployed as a runtime
            # bind-mount patch over an older image whose ReleaseSearchPlan has no page
            # field yet. There the browse path simply stays on page 1.
            requested_page: int = getattr(plan, "page", 1) or 1
            page = min(max(1, requested_page), browse_max_page)
            result, used_language_fallback = self._search_page_with_language_fallback(
                query, filters, page=page
            )
            self._last_search_page = page
            # A fallback page 1 answers a different query (no language filter) than the
            # one the client would page on, so it does not offer a page 2.
            self._last_search_has_more = (
                result.has_more and page < browse_max_page and not used_language_fallback
            )
            self._last_search_type = "manual" if query else "title_author"
            return [_browse_record_to_release(record) for record in result.records]

        # ISBN search first (unless expand_search requested)
        if plan.manual_query:
            expand_search = True

        if not expand_search:
            isbn = plan.isbn_candidates[0] if plan.isbn_candidates else None
            if isbn:
                logger.debug("Searching direct_download: isbn='%s', langs=%s", isbn, lang_filter)
                filters = SearchFilters(isbn=[isbn])
                filters.lang = lang_filter if lang_filter is not None else []
                try:
                    results = search_books(isbn, filters)
                    if results:
                        logger.info("Found %s releases via ISBN", len(results))
                        self._last_search_type = "isbn"
                        return [_browse_record_to_release(record) for record in results]
                    logger.debug("No ISBN results, falling back to title+author")
                except SearchUnavailableError:
                    raise
                except (ValueError, TypeError, AttributeError, RuntimeError) as e:
                    logger.warning("ISBN search failed: %s", e)

        # Title + author fallback
        author = plan.author
        searches = [(v.title, v.languages) for v in plan.grouped_title_variants]

        # Execute searches with deduplication
        seen_ids: set = set()
        all_results: list[BrowseRecord] = []

        for title, langs in searches:
            query = f"{title} {author}".strip()
            if not query:
                continue

            logger.debug("Searching direct_download: title_author='%s', langs=%s", query, langs)
            filters = SearchFilters(lang=langs if langs is not None else [])
            try:
                for bi in search_books(query, filters):
                    if bi.id not in seen_ids:
                        seen_ids.add(bi.id)
                        all_results.append(bi)
            except SearchUnavailableError:
                raise
            except Exception:
                logger.exception("Search error")

        if not all_results and any(langs for _, langs in searches):
            logger.debug(
                "No title+author results with language filter, retrying without language filter"
            )
            for title, _langs in searches:
                query = f"{title} {author}".strip()
                if not query:
                    continue

                logger.debug("Searching direct_download: title_author='%s', langs=[]", query)
                try:
                    for bi in search_books(query, SearchFilters()):
                        if bi.id not in seen_ids:
                            seen_ids.add(bi.id)
                            all_results.append(bi)
                except SearchUnavailableError:
                    raise
                except Exception:
                    logger.exception("Search error")

        logger.info("Found %s releases via title+author", len(all_results))
        return [_browse_record_to_release(record) for record in all_results]

    def is_available(self) -> bool:
        """Check if Direct Download has been explicitly enabled and configured."""
        return _get_direct_download_unavailable_reason() is None


@register_handler("direct_download")
class DirectDownloadHandler(DownloadHandler):
    """Handler for direct HTTP downloads from Anna's Archive, Libgen, etc.

    Receives a DownloadTask with task_id (AA MD5 hash) and cascades through
    sources in priority order. The AA page is only fetched if AA slow sources
    are enabled in the user's source priority configuration.
    """

    def download(
        self,
        task: DownloadTask,
        cancel_flag: Event,
        progress_callback: Callable[[float], None],
        status_callback: Callable[[str, str | None], None],
    ) -> str | None:
        """Execute a direct HTTP download.

        Uses task.task_id (AA MD5 hash) to cascade through sources in priority
        order. The AA page is only fetched if AA slow sources are enabled.

        Args:
            task: Download task with task_id (AA MD5 hash)
            cancel_flag: Event to check for cancellation
            progress_callback: Called with progress percentage (0-100)
            status_callback: Called with (status, message) for status updates

        Returns:
            Path to downloaded file if successful, None otherwise

        """
        try:
            # Check for cancellation before starting
            if cancel_flag.is_set():
                logger.info("Download cancelled before starting: %s", task.task_id)
                status_callback("cancelled", "Cancelled")
                return None

            # Create browse record from task data - NO AA page fetch here
            # AA page is fetched lazily by _fetch_aa_page_urls only when
            # we actually reach an AA slow source in the priority order
            book_info = BrowseRecord(
                id=task.task_id,
                title=task.title,
                source="direct_download",
                author=task.author,
                year=task.year,
                format=task.format,
                size=task.size,
                preview=task.preview,
            )

            return self._execute_download(
                book_info, cancel_flag, progress_callback, status_callback
            )

        except Exception as e:
            if cancel_flag.is_set():
                logger.info("Download cancelled during error handling: %s", task.task_id)
                status_callback("cancelled", "Cancelled")
            else:
                logger.exception("Error downloading book")
                status_callback("error", str(e))
            return None

    def _execute_download(
        self,
        book_info: BrowseRecord,
        cancel_flag: Event,
        progress_callback: Callable[[float], None],
        status_callback: Callable[[str, str | None], None],
    ) -> str | None:
        """Execute the direct-download flow with a fetched browse record.

        This contains the core download logic: cascade through sources,
        handle bypass, move to final location.
        """
        try:
            logger.debug("Starting download: %s", book_info.title)

            # Prepare paths - use descriptive staging filename, orchestrator will rename
            # based on FILE_ORGANIZATION setting
            file_org = config.get("FILE_ORGANIZATION", "rename")
            if file_org == "none":
                book_name = f"{book_info.id}.{book_info.format or 'bin'}"
            else:
                book_name = build_filename(
                    book_info.title,
                    book_info.author,
                    book_info.year,
                    book_info.format,
                )
            book_path = TMP_DIR / book_name

            # Check cancellation before download
            if cancel_flag.is_set():
                logger.info("Download cancelled before download call: %s", book_info.id)
                status_callback("cancelled", "Cancelled")
                return None

            # Execute download via _download_book (handles cascade and bypass)
            status_callback("resolving", "Finding download source")
            success_url = _download_book(
                book_info, book_path, progress_callback, cancel_flag, status_callback
            )

            # Check for cancellation after download
            if cancel_flag.is_set():
                logger.info("Download cancelled during download: %s", book_info.id)
                if book_path.exists():
                    book_path.unlink()
                status_callback("cancelled", "Cancelled")
                return None

            if not success_url:
                status_callback("error", "All download sources failed")
                return None

            # Return temp path - orchestrator handles post-processing (archive extraction, ingest)
            return str(book_path)

        except Exception:
            if cancel_flag.is_set():
                logger.info("Download cancelled during error handling: %s", book_info.id)
                status_callback("cancelled", "Cancelled")
            else:
                logger.exception("Error downloading book")
            return None

    def cancel(self, task_id: str) -> bool:
        """Cancel an in-progress download.

        Cancellation is handled via the cancel_flag passed to download().
        This method exists for the interface but actual cancellation
        happens through the Event flag mechanism.
        """
        # Cancellation is handled by the orchestrator via cancel_flag
        return False
