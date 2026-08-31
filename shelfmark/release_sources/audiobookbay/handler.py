"""AudiobookBay download handler - resolves magnet links and uses shared client lifecycle."""

from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

from shelfmark.core.config import config
from shelfmark.core.logger import setup_logger
from shelfmark.download.clients import (
    DownloadClient,
    get_client,
    list_configured_clients,
)
from shelfmark.download.clients.base_handler import (
    DownloadRequest,
    ExternalClientHandler,
)
from shelfmark.release_sources import register_handler
from shelfmark.release_sources.audiobookbay import scraper
from shelfmark.release_sources.audiobookbay.utils import normalize_hostname

if TYPE_CHECKING:
    from collections.abc import Callable

    from shelfmark.core.models import DownloadTask

logger = setup_logger(__name__)
DEFAULT_ABB_HOSTNAME = "audiobookbay.lu"
ALLOWED_DETAIL_URL_SCHEMES = {"https"}

# Actionable rejection text. AudiobookBay resolves a magnet only from its detail
# page; a metadata search result (e.g. source_id "audible:B00NWCPRBU") carries no
# such URL and can never be downloaded. Production 2026-08-30 queued exactly that
# payload from an API client and it died in the worker instead of at submit time.
MISSING_DETAIL_URL_ERROR = (
    "AudiobookBay downloads need the release detail page URL. "
    "Search releases first (GET /api/releases?source=audiobookbay) and queue one of the "
    "returned releases (its source_url), not a metadata search result."
)


def _pick_detail_url(source_url: str | None, task_id: str | None) -> str | None:
    """Return the usable ABB detail URL from a source_url/task_id pair, if any."""
    url = (source_url or "").strip()
    if url:
        return url

    # Backward-compat: older tests and some legacy flows used task_id as URL.
    candidate = (task_id or "").strip()
    if candidate.startswith(("http://", "https://")):
        return candidate
    return None


def _resolve_configured_hostname() -> str:
    """Return a normalized ABB hostname from config when available."""
    configured_hostname = config.get("ABB_HOSTNAME", "")
    return normalize_hostname(configured_hostname if isinstance(configured_hostname, str) else "")


def _resolve_allowed_detail_hostname() -> str:
    """Return the ABB hostname allowed for queued detail URLs."""
    return _resolve_configured_hostname() or DEFAULT_ABB_HOSTNAME


def _detail_url_matches_host(detail_url: str, hostname: str) -> bool:
    """Return True when a detail URL uses the allowed ABB scheme and host."""
    parsed = urlparse(detail_url)
    detail_hostname = normalize_hostname(parsed.hostname)
    allowed_hostname = normalize_hostname(hostname).lower().rstrip(".")
    return (
        parsed.scheme.lower() in ALLOWED_DETAIL_URL_SCHEMES
        and bool(detail_hostname)
        and detail_hostname.lower().rstrip(".") == allowed_hostname
    )


@register_handler("audiobookbay")
class AudiobookBayHandler(ExternalClientHandler):
    """Handler for AudiobookBay downloads via configured torrent client."""

    @staticmethod
    def _resolve_detail_url(task: DownloadTask) -> str | None:
        """Resolve ABB detail URL from queued task metadata."""
        return _pick_detail_url(task.source_url, task.task_id)

    def validate_queue_request(
        self,
        release_data: dict[str, Any],
        source_url: str | None,
    ) -> str | None:
        """Reject releases without a detail URL before they are queued.

        Deliberately no search-based fallback: guessing a release from title/author
        would queue a download the requester never saw (wrong edition, language, or
        abridgement) and it would be indistinguishable from an approved pick in the
        request history. Failing loudly at submit time is the honest behaviour, and
        it is the only variant that also covers non-browser API clients.
        """
        source_id = release_data.get("source_id")
        if _pick_detail_url(source_url, source_id if isinstance(source_id, str) else None):
            return None
        logger.warning("Rejected AudiobookBay release without details URL: %s", source_id)
        return MISSING_DETAIL_URL_ERROR

    def _get_client(self, protocol: str) -> DownloadClient | None:
        """Compatibility shim so module-level patching still works in tests."""
        return get_client(protocol)

    def _list_configured_clients(self) -> list[str]:
        """Compatibility shim so module-level patching still works in tests."""
        return list_configured_clients()

    def _resolve_download(
        self,
        task: DownloadTask,
        status_callback: Callable[[str, str | None], None],
    ) -> DownloadRequest | None:
        """Resolve ABB detail page into a magnet-link download request."""
        detail_url = self._resolve_detail_url(task)
        if not detail_url:
            status_callback("error", MISSING_DETAIL_URL_ERROR)
            logger.warning("Missing details URL for AudiobookBay task: %s", task.task_id)
            return None

        hostname = _resolve_allowed_detail_hostname()
        if not _detail_url_matches_host(detail_url, hostname):
            status_callback("error", "Invalid AudiobookBay details URL")
            logger.warning(
                "Rejected AudiobookBay details URL with invalid scheme or host: %s",
                detail_url,
            )
            return None

        status_callback("resolving", "Extracting magnet link")
        magnet_link = scraper.extract_magnet_link(detail_url, hostname)

        if not magnet_link:
            status_callback("error", "Failed to extract magnet link from detail page")
            return None

        logger.info("Extracted magnet link for task %s", task.task_id)

        return DownloadRequest(
            url=magnet_link,
            protocol="torrent",
            release_name=task.title or "Unknown",
            expected_hash=None,
        )

    def cancel(self, task_id: str) -> bool:
        """Cancel an in-progress download.

        Shelfmark can stop waiting via the queue cancel flag, but once a magnet has
        been sent to the torrent client we do not remove it client-side. Users must
        cancel/remove it in their torrent client UI.
        """
        logger.debug("Cancel requested for AudiobookBay task: %s", task_id)
        return False
