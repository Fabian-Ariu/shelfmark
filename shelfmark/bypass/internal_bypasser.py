"""Internal Cloudflare bypass implementation using SeleniumBase and CDP helpers."""

import _thread
import asyncio
import atexit
import json
import os
import random
import select
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from contextlib import suppress
from datetime import UTC, datetime
from http import HTTPStatus
from pathlib import Path
from threading import Event
from typing import Any, Protocol, TypedDict, TypeGuard
from urllib.parse import urlparse

import requests
from seleniumbase import cdp_driver
from seleniumbase.undetected.cdp_driver.connection import ProtocolException

from shelfmark.bypass import BypassCancelledError
from shelfmark.bypass.fingerprint import get_screen_size
from shelfmark.config import env
from shelfmark.config.env import LOG_DIR
from shelfmark.config.settings import RECORDING_DIR
from shelfmark.core.config import config as app_config
from shelfmark.core.logger import setup_logger
from shelfmark.download import network
from shelfmark.download.network import get_proxies, get_ssl_verify

logger = setup_logger(__name__)

SELENIUMBASE_RUNTIME_ROOT = Path(tempfile.gettempdir()) / "shelfmark" / "seleniumbase"
SELENIUMBASE_DOWNLOADS_DIR = SELENIUMBASE_RUNTIME_ROOT / "downloaded_files"
BROWSER_RUNTIME_ROOT = Path(tempfile.gettempdir()) / "shelfmark" / "browser"
BROWSER_HOME_DIR = BROWSER_RUNTIME_ROOT / "home"
BROWSER_XDG_RUNTIME_DIR = BROWSER_RUNTIME_ROOT / "runtime"
_BYPASSED_BODY_LENGTH_MIN = 100_000
_BYPASS_EMOJI_MATCH_MIN = 3
_LOADING_BODY_LENGTH_MAX = 50
_PAGE_BODY_PREVIEW_CHARS = 500
_BROWSER_START_TIMEOUT_SECONDS = 45.0
_BYPASS_SUBPROCESS_TIMEOUT_SECONDS = 420.0
_BYPASS_CHILD_ENV = "SHELFMARK_INTERNAL_BYPASSER_CHILD"

# Seconds to let a freshly started Chrome settle before the first navigation.
# This used to read app_config.DEFAULT_SLEEP, which is the *download retry delay*
# from the Downloads settings tab (default 5s) - raising that value silently made
# every bypass solve 5s slower. Keep it small but never zero: it also papers over a
# race between the CDP websocket being up and the first driver.get().
_BROWSER_SETTLE_SECONDS = 1.5


def _env_number(name: str, default: float, *, minimum: float = 0.0) -> float:
    """Read one lifecycle bound from the environment.

    Every limit below is a lever we may have to pull on a *running* container - the
    bypass path is the only route to Anna's Archive, so "rebuild the image" is not an
    acceptable first response to it misbehaving. Compose ENV plus a container restart
    has to be enough, therefore none of these may be a bare literal.
    """
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = float(raw.strip())
    except ValueError:
        logger.warning("Ignoring %s=%r: not a number", name, raw)
        return default
    if value < minimum:
        logger.warning("Ignoring %s=%r: below the minimum of %s", name, raw, minimum)
        return default
    return value


# Warm browser limits (child process). A reused Chrome keeps the DDoS-Guard cookies
# in the browser where they actually work, but it also accumulates memory and
# fingerprint drift, so it is retired deliberately.
# SHELFMARK_WARM_BROWSER_MAX_USES=1 is the kill switch: the browser is then retired
# before the second solve, which is exactly the pre-change behaviour.
_WARM_BROWSER_MAX_AGE_SECONDS = _env_number(
    "SHELFMARK_WARM_BROWSER_MAX_AGE_SECONDS", 300.0, minimum=1.0
)
_WARM_BROWSER_MAX_USES = int(_env_number("SHELFMARK_WARM_BROWSER_MAX_USES", 12, minimum=1))
# Chrome plus its renderers above this RSS retires the browser. The container runs at
# mem_limit 512m; an OOM kill there takes the only path to Anna's Archive with it, and
# a warm browser is the one thing that can grow across solves. 0 disables the check.
_WARM_BROWSER_MAX_RSS_MB = _env_number("SHELFMARK_WARM_BROWSER_MAX_RSS_MB", 300.0)

# Helper process pooling (parent process).
_HELPER_IDLE_TIMEOUT_SECONDS = _env_number(
    "SHELFMARK_BYPASS_HELPER_IDLE_SECONDS", 60.0, minimum=1.0
)
_HELPER_MAX_AGE_SECONDS = _env_number("SHELFMARK_BYPASS_HELPER_MAX_AGE_SECONDS", 900.0, minimum=1.0)
_HELPER_MAX_REQUESTS = int(_env_number("SHELFMARK_BYPASS_HELPER_MAX_REQUESTS", 40, minimum=1))
_HELPER_REAPER_INTERVAL_SECONDS = 15.0
_HELPER_RESULT_POLL_SECONDS = 0.1
_HELPER_GRACEFUL_EXIT_SECONDS = 5.0
_HELPER_KILL_GRACE_SECONDS = 5.0
# The child stops itself when the parent goes away without closing stdin. Must stay
# larger than the parent-side idle timeout so the parent reaps first - derived instead
# of fixed, because that timeout is settable from the environment now.
_CHILD_IDLE_EXIT_SECONDS = max(120.0, _HELPER_IDLE_TIMEOUT_SECONDS * 2)

# A method hint is dropped after this many consecutive failures so a protection
# change (DDoS-Guard -> Cloudflare or vice versa) cannot freeze us on a dead method.
_METHOD_HINT_MAX_FAILURES = 2

# Challenge detection indicators
CLOUDFLARE_INDICATORS = [
    "just a moment",
    "verify you are human",
    "verifying you are human",
    "cloudflare.com/products/turnstile",
]

DDOS_GUARD_INDICATORS = [
    "ddos-guard",
    "ddos guard",
    "checking your browser before accessing",
    "complete the manual check to continue",
    "could not verify your browser automatically",
]


class _DisplayState(TypedDict):
    ffmpeg: subprocess.Popen[bytes] | None
    ffmpeg_output: Path | None


class _PageWithWindowRect(Protocol):
    async def set_window_rect(self, x: int, _y: int, width: int, height: int) -> object: ...


class _BrowserWithWindowRectPage(Protocol):
    page: _PageWithWindowRect


DISPLAY: _DisplayState = {
    "ffmpeg": None,
    "ffmpeg_output": None,
}
LOCKED = threading.Lock()
_PGREP_PATH = shutil.which("pgrep")
_PKILL_PATH = shutil.which("pkill")
_RNG = random.SystemRandom()

_CDP_OPERATION_ERRORS = (
    asyncio.TimeoutError,
    AttributeError,
    NameError,
    OSError,
    ProtocolException,
    RuntimeError,
    TypeError,
    ValueError,
)
_PATH_INSPECTION_ERRORS = (OSError, RuntimeError, TypeError, ValueError)
_REQUEST_OPERATION_ERRORS = (
    OSError,
    RuntimeError,
    TypeError,
    ValueError,
    requests.RequestException,
)
_SUBPROCESS_OPERATION_ERRORS = (
    OSError,
    RuntimeError,
    TypeError,
    ValueError,
    subprocess.SubprocessError,
)
_NATIVE_ATTR_ERRORS = (ImportError, AttributeError, RuntimeError)


def _get_native_attr(module: str, name: str, fallback: Any) -> Any:
    """Return an unpatched stdlib attribute when running under gevent."""
    try:
        from gevent import monkey

        original = monkey.get_original(module, name)
    except _NATIVE_ATTR_ERRORS:
        return fallback
    else:
        return original or fallback


_NATIVE_START_NEW_THREAD = _get_native_attr("_thread", "start_new_thread", _thread.start_new_thread)
_NATIVE_EVENT = _get_native_attr("threading", "Event", threading.Event)
_NATIVE_LOCK = _get_native_attr("threading", "Lock", threading.Lock)


def _coerce_positive_int(value: object, default: int) -> int:
    """Return a positive integer config value or the provided default."""
    if isinstance(value, bool):
        return default
    if isinstance(value, int) and value > 0:
        return value
    return default


def _has_window_rect_page(candidate: object) -> TypeGuard[_BrowserWithWindowRectPage]:
    """Check whether a browser wrapper exposes page.set_window_rect()."""
    page = getattr(candidate, "page", None)
    return callable(getattr(page, "set_window_rect", None))


def _describe_runtime_path(path: str | Path) -> str:
    """Return compact ownership/mode info for a runtime path."""
    try:
        path = Path(path)
        link_target = ""
        if path.is_symlink():
            link_target = f" -> {path.readlink()}"
        st = path.stat()
        mode = stat.S_IMODE(st.st_mode)
        return f"{path}{link_target} exists uid={st.st_uid} gid={st.st_gid} mode={oct(mode)}"
    except FileNotFoundError:
        return f"{path} missing"
    except _PATH_INSPECTION_ERRORS as e:
        return f"{path} error={type(e).__name__}: {e}"


class _CdpWorker:
    def __init__(self) -> None:
        self._thread_id: int | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._ready = _NATIVE_EVENT()
        self._lock = _NATIVE_LOCK()

    def _run(self) -> None:
        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            self._loop = loop
            self._ready.set()
            loop.run_forever()
            with suppress(Exception):
                pending = asyncio.all_tasks(loop)
                for task in pending:
                    task.cancel()
                if pending:
                    loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            loop.close()
        finally:
            self._thread_id = None

    def start(self) -> None:
        with self._lock:
            if self._loop and self._loop.is_running() and not self._loop.is_closed():
                return
            self._loop = None
            self._ready.clear()
            self._thread_id = _NATIVE_START_NEW_THREAD(self._run, ())
        if not self._ready.wait(timeout=10):
            msg = "CDP worker loop failed to start"
            raise RuntimeError(msg)

    def run(self, coro: Any, timeout: float | None = None) -> Any:
        self.start()
        if not self._loop or self._loop.is_closed():
            msg = "CDP worker loop not available"
            raise RuntimeError(msg)
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return future.result(timeout=timeout)


_CDP_WORKER = _CdpWorker()

# Cookie storage - shared with requests library for Cloudflare bypass
# Nested mapping of domain to cookie name to cookie metadata.
_cf_cookies: dict[str, dict] = {}
_cf_cookies_lock = threading.Lock()

# User-Agent storage - Cloudflare ties cf_clearance to the UA that solved the challenge
_cf_user_agents: dict[str, str] = {}

# Adaptive bypass-method memory: "<base domain>|<challenge type>" -> method name.
# Guarded by _cf_cookies_lock and shuttled between parent and helper child in the
# same payload that already carries cookies and user agents.
_bypass_method_hints: dict[str, str] = {}
_bypass_method_hint_failures: dict[str, int] = {}

# Protection cookie names we care about (Cloudflare and DDoS-Guard)
CF_COOKIE_NAMES = {"cf_clearance", "__cf_bm", "cf_chl_2", "cf_chl_prog"}
DDG_COOKIE_NAMES = {
    "__ddg1_",
    "__ddg2_",
    "__ddg5_",
    "__ddg8_",
    "__ddg9_",
    "__ddg10_",
    "__ddgid_",
    "__ddgmark_",
    "ddg_last_challenge",
}


# Cookies that actually gate access. If one of these has a stored expiry in the
# past, the whole domain entry is worthless and must be re-solved. Cloudflare uses
# cf_clearance; DDoS-Guard (Anna's Archive since 2026-08) uses __ddg8_/__ddg10_,
# which expire after ~20 minutes -- without this check the store would hand out
# dead cookies forever, because the old check only looked at cf_clearance.
EXPIRY_CRITICAL_COOKIE_NAMES = ("cf_clearance", "__ddg2_", "__ddg8_", "__ddg10_")


def _get_base_domain(domain: str) -> str:
    """Extract base domain from hostname (e.g., 'www.example.com' -> 'example.com')."""
    return ".".join(domain.split(".")[-2:]) if "." in domain else domain


def _get_full_cookie_domains() -> set[str]:
    """Return mirror domains that need full-session cookie extraction."""
    from shelfmark.core.mirrors import get_zlib_cookie_domains

    return {_get_base_domain(domain) for domain in get_zlib_cookie_domains()}


def _should_extract_cookie(name: str, *, extract_all: bool) -> bool:
    """Determine if a cookie should be extracted based on its name."""
    if extract_all:
        return True
    is_cf = name in CF_COOKIE_NAMES or name.startswith("cf_")
    is_ddg = name in DDG_COOKIE_NAMES or name.startswith("__ddg")
    return is_cf or is_ddg


def _store_extracted_cookies(
    *,
    url: str,
    cookies: list[Any],
    user_agent: str | None = None,
) -> None:
    """Store filtered bypass cookies (and optional UA) for a URL domain."""
    parsed = urlparse(url)
    domain = parsed.hostname or ""
    if not domain:
        return

    base_domain = _get_base_domain(domain)
    extract_all = base_domain in _get_full_cookie_domains()

    cookies_found: dict[str, dict[str, Any]] = {}
    for cookie in cookies:
        name = getattr(cookie, "name", "") or ""
        if not _should_extract_cookie(name, extract_all=extract_all):
            continue
        expires = getattr(cookie, "expires", None)
        if expires is not None and expires <= 0:
            expires = None
        cookies_found[name] = {
            "value": getattr(cookie, "value", ""),
            "domain": getattr(cookie, "domain", None) or domain,
            "path": getattr(cookie, "path", None) or "/",
            "expiry": expires,
            "secure": bool(getattr(cookie, "secure", True)),
            "httpOnly": True,
        }

    if not cookies_found:
        return

    with _cf_cookies_lock:
        _cf_cookies[base_domain] = cookies_found
        if user_agent:
            _cf_user_agents[base_domain] = user_agent
            logger.debug("Stored UA for %s: %s...", base_domain, str(user_agent)[:60])
        else:
            logger.debug("No UA captured for %s", base_domain)

    cookie_type = "all" if extract_all else "protection"
    logger.debug("Extracted %s %s cookies for %s", len(cookies_found), cookie_type, base_domain)


def refresh_cookies_for_domain(url: str, cookies: list[Any]) -> None:
    """Refresh stored protection cookies from a successful non-browser request.

    DDoS-Guard rotates ``__ddg8_``/``__ddg10_`` on every response and expires them
    after roughly 20 minutes. Without a write-back the store stays only as fresh as
    the last browser solve, so get_cf_cookies_for_domain() eventually discards a
    cookie set that plain traffic had just renewed -- and pays another 20-67s solve
    for it.

    Deliberately narrow:
    - only refreshes domains that already have a solved entry, so ordinary traffic
      can never create bypass state for a domain we never solved,
    - only protection cookies, never the full-session extraction used for the
      Z-Library mirrors: that snapshot must stay the one the browser produced,
    - keeps the stored expiry when a refreshed cookie carries none.
    """
    domain = urlparse(url).hostname or ""
    if not domain:
        return

    base_domain = _get_base_domain(domain)

    with _cf_cookies_lock:
        stored = _cf_cookies.get(base_domain)
        if not stored:
            return

        refreshed: list[str] = []
        for cookie in cookies:
            name = getattr(cookie, "name", "") or ""
            if not _should_extract_cookie(name, extract_all=False):
                continue
            value = getattr(cookie, "value", None)
            if value is None:
                continue

            expires = getattr(cookie, "expires", None)
            if expires is not None and expires <= 0:
                expires = None

            entry = dict(stored.get(name, {}))
            entry["value"] = value
            entry.setdefault("domain", getattr(cookie, "domain", None) or domain)
            entry.setdefault("path", getattr(cookie, "path", None) or "/")
            entry.setdefault("secure", bool(getattr(cookie, "secure", True)))
            entry.setdefault("httpOnly", True)
            if expires is not None:
                entry["expiry"] = expires
                entry.pop("expires", None)

            stored[name] = entry
            refreshed.append(name)

        if refreshed:
            _cf_cookies[base_domain] = stored
            logger.debug(
                "Refreshed %d protection cookie(s) for %s: %s",
                len(refreshed),
                base_domain,
                ", ".join(sorted(refreshed)),
            )


async def _extract_cookies_from_cdp(driver: Any, page: Any, url: str) -> None:
    """Extract cookies from a CDP browser after successful bypass."""
    try:
        try:
            all_cookies = await driver.cookies.get_all(requests_cookie_format=True)
        except _CDP_OPERATION_ERRORS as e:
            logger.debug("Failed to get cookies via CDP: %s", e)
            return

        try:
            user_agent = await page.evaluate("navigator.userAgent")
        except _CDP_OPERATION_ERRORS:
            user_agent = None

        _store_extracted_cookies(url=url, cookies=all_cookies, user_agent=user_agent)

    except _CDP_OPERATION_ERRORS as e:
        logger.debug("Failed to extract cookies: %s", e)


def get_cf_cookies_for_domain(domain: str) -> dict[str, str]:
    """Get stored cookies for a domain. Returns empty dict if none available."""
    if not domain:
        return {}

    base_domain = _get_base_domain(domain)

    with _cf_cookies_lock:
        cookies = _cf_cookies.get(base_domain, {})
        if not cookies:
            return {}

        now = time.time()
        for name in EXPIRY_CRITICAL_COOKIE_NAMES:
            cookie = cookies.get(name)
            if not cookie:
                continue
            expiry = cookie.get("expiry")
            if expiry is None:
                expiry = cookie.get("expires")
            if expiry and expiry > 0 and now > expiry:
                logger.debug("Protection cookie %s expired for %s", name, base_domain)
                _cf_cookies.pop(base_domain, None)
                return {}

        return {name: c["value"] for name, c in cookies.items()}


def has_valid_cf_cookies(domain: str) -> bool:
    """Check if we have valid Cloudflare cookies for a domain."""
    return bool(get_cf_cookies_for_domain(domain))


def get_cf_user_agent_for_domain(domain: str) -> str | None:
    """Get the User-Agent that was used during bypass for a domain."""
    if not domain:
        return None
    with _cf_cookies_lock:
        return _cf_user_agents.get(_get_base_domain(domain))


def clear_cf_cookies(domain: str | None = None) -> None:
    """Clear stored Cloudflare cookies and User-Agent. If domain is None, clear all."""
    with _cf_cookies_lock:
        if domain:
            base_domain = _get_base_domain(domain)
            _cf_cookies.pop(base_domain, None)
            _cf_user_agents.pop(base_domain, None)
        else:
            _cf_cookies.clear()
            _cf_user_agents.clear()


def _method_hint_key(url: str, challenge_type: str) -> str:
    """Build the memory key for a URL/challenge combination, "" when unusable."""
    if not url or not challenge_type or challenge_type == "none":
        return ""
    hostname = urlparse(url).hostname or ""
    base_domain = _get_base_domain(hostname)
    if not base_domain:
        return ""
    return f"{base_domain}|{challenge_type}"


def get_bypass_method_hint(url: str, challenge_type: str) -> str | None:
    """Return the method name that last solved this host/challenge combination."""
    key = _method_hint_key(url, challenge_type)
    if not key:
        return None
    with _cf_cookies_lock:
        return _bypass_method_hints.get(key)


def record_bypass_method_result(
    url: str, challenge_type: str, method_name: str, *, success: bool
) -> None:
    """Remember (or forget) the method that worked for this host/challenge."""
    key = _method_hint_key(url, challenge_type)
    if not key or not method_name:
        return

    with _cf_cookies_lock:
        if success:
            if _bypass_method_hints.get(key) != method_name:
                logger.debug("Remembering bypass method %s for %s", method_name, key)
            _bypass_method_hints[key] = method_name
            _bypass_method_hint_failures.pop(key, None)
            return

        if _bypass_method_hints.get(key) != method_name:
            return

        failures = _bypass_method_hint_failures.get(key, 0) + 1
        if failures >= _METHOD_HINT_MAX_FAILURES:
            logger.debug("Dropping stale bypass method hint %s for %s", method_name, key)
            _bypass_method_hints.pop(key, None)
            _bypass_method_hint_failures.pop(key, None)
        else:
            _bypass_method_hint_failures[key] = failures


def get_bypass_method_hints() -> dict[str, str]:
    """Snapshot of the method memory, used to seed the helper child."""
    with _cf_cookies_lock:
        return dict(_bypass_method_hints)


def _valid_method_hints(hints: object) -> dict[str, str] | None:
    """Filter a hint snapshot from the other side of the process boundary."""
    if not isinstance(hints, dict):
        return None
    return {
        key: name
        for key, name in hints.items()
        if isinstance(key, str) and isinstance(name, str) and key and name
    }


def merge_bypass_method_hints(hints: dict[str, str]) -> None:
    """Add a method-memory snapshot to what we already know (used to seed the child)."""
    valid = _valid_method_hints(hints)
    if valid is None:
        return
    with _cf_cookies_lock:
        _bypass_method_hints.update(valid)


def replace_bypass_method_hints(hints: dict[str, str]) -> None:
    """Adopt the child's hint snapshot wholesale, deletions included.

    Merging here would make the decay in record_bypass_method_result() unobservable:
    that function only ever runs in the child (it is _bypass() that calls it), the
    parent seeds every child with its full snapshot, and a plain dict.update() can
    never remove a key. A hint the child retired after two failures would be shipped
    straight back into the next child, whose failure counter starts at zero again -
    the hint would be immortal for the life of the gunicorn worker.

    Replacing is safe because the exchange is strictly serialised: the parent holds
    LOCKED for the whole solve, so nothing can have learned a hint in the meantime,
    and the child was seeded with everything the parent knew.
    """
    valid = _valid_method_hints(hints)
    if valid is None:
        return
    with _cf_cookies_lock:
        dropped = set(_bypass_method_hints) - set(valid)
        if dropped:
            logger.debug("Child retired bypass method hints: %s", sorted(dropped))
        _bypass_method_hints.clear()
        _bypass_method_hints.update(valid)
        for key in dropped:
            _bypass_method_hint_failures.pop(key, None)


def clear_bypass_method_hints() -> None:
    """Forget every remembered bypass method."""
    with _cf_cookies_lock:
        _bypass_method_hints.clear()
        _bypass_method_hint_failures.clear()


def _cleanup_orphan_processes() -> int:
    """Kill orphan Chrome/Xvfb/ffmpeg processes. Only runs in Docker mode."""
    if not env.DOCKERMODE:
        return 0

    _stop_ffmpeg_recording()

    processes_to_kill = ["chrome", "chromium", "Xvfb", "ffmpeg"]
    total_killed = 0

    logger.debug("Checking for orphan processes...")
    logger.log_resource_usage()

    if _PGREP_PATH is None or _PKILL_PATH is None:
        logger.warning("Skipping orphan-process cleanup because pgrep/pkill are unavailable")
        return 0

    for proc_name in processes_to_kill:
        try:
            result = subprocess.run(
                [_PGREP_PATH, "-f", proc_name],
                capture_output=True,
                check=False,
                text=True,
                timeout=5,
            )
            if result.returncode != 0 or not result.stdout.strip():
                continue

            pids = result.stdout.strip().split("\n")
            count = len(pids)
            logger.info("Found %s orphan %s process(es), killing...", count, proc_name)

            kill_result = subprocess.run(
                [_PKILL_PATH, "-9", "-f", proc_name],
                capture_output=True,
                check=False,
                timeout=5,
            )
            if kill_result.returncode == 0:
                total_killed += count
            else:
                logger.warning("pkill for %s returned %s", proc_name, kill_result.returncode)

        except subprocess.TimeoutExpired:
            logger.warning("Timeout while checking for %s processes", proc_name)
        except _SUBPROCESS_OPERATION_ERRORS as e:
            logger.debug("Error checking for %s processes: %s", proc_name, e)

    if total_killed > 0:
        time.sleep(1)
        logger.info("Cleaned up %s orphan process(es)", total_killed)
        logger.log_resource_usage()
    else:
        logger.debug("No orphan processes found")

    return total_killed


async def _get_page_info(page: Any) -> tuple[str, str, str]:
    """Extract page title, body text, and current URL safely."""
    try:
        title = (await page.get_title() or "").lower()
    except _CDP_OPERATION_ERRORS:
        title = ""
    try:
        body = await page.evaluate("document.body ? document.body.innerText : ''")
        body = (body or "").lower()
    except _CDP_OPERATION_ERRORS:
        body = ""
    try:
        current_url = await page.get_current_url() or ""
    except _CDP_OPERATION_ERRORS:
        current_url = ""
    return title, body, current_url


def _check_indicators(title: str, body: str, indicators: list[str]) -> str | None:
    """Check if any indicator is present in title or body. Returns the found indicator or None."""
    for indicator in indicators:
        if indicator in title or indicator in body:
            return indicator
    return None


def _has_cloudflare_patterns(body: str, url: str) -> bool:
    """Check for Cloudflare-specific patterns in body or URL."""
    return "cf-" in body or "cloudflare" in url.lower() or "/cdn-cgi/" in url


async def _detect_challenge_type(page: Any) -> str:
    """Detect challenge type: 'cloudflare', 'ddos_guard', or 'none'."""
    title, body, current_url = await _get_page_info(page)

    # DDOS-Guard indicators
    if found := _check_indicators(title, body, DDOS_GUARD_INDICATORS):
        logger.debug("DDOS-Guard indicator found: '%s'", found)
        return "ddos_guard"

    # Cloudflare indicators
    if found := _check_indicators(title, body, CLOUDFLARE_INDICATORS):
        logger.debug("Cloudflare indicator found: '%s'", found)
        return "cloudflare"

    # Check URL patterns
    if _has_cloudflare_patterns(body, current_url):
        return "cloudflare"

    return "none"


async def _is_bypassed(page: Any, *, escape_emojis: bool = True) -> bool:
    """Check if the protection has been bypassed."""
    title, body, current_url = await _get_page_info(page)
    body_len = len(body.strip())

    # Long page content = probably bypassed
    if body_len > _BYPASSED_BODY_LENGTH_MIN:
        logger.debug("Page content too long, probably bypassed (len: %s)", body_len)
        return True

    # Multiple emojis = probably real content
    if escape_emojis:
        import emoji

        if len(emoji.emoji_list(body)) >= _BYPASS_EMOJI_MATCH_MIN:
            logger.debug("Detected emojis in page, probably bypassed")
            return True

    # Check for protection indicators (means NOT bypassed)
    if _check_indicators(title, body, CLOUDFLARE_INDICATORS + DDOS_GUARD_INDICATORS):
        return False

    # Cloudflare URL patterns
    if _has_cloudflare_patterns(body, current_url):
        logger.debug("Cloudflare patterns detected in page")
        return False

    # Page too short = still loading
    if body_len < _LOADING_BODY_LENGTH_MAX:
        logger.debug("Page content too short, might still be loading")
        return False

    logger.debug("Bypass check passed - Title: '%s', Body length: %s", title[:100], body_len)
    return True


async def _bypass_method_humanlike(page: Any) -> bool:
    """Human-like behavior with scroll, wait, and reload."""
    try:
        logger.debug("Attempting bypass: human-like interaction")
        await asyncio.sleep(_RNG.uniform(6, 10))

        try:
            await page.evaluate("window.scrollTo(0, 10000);")
            await page.wait()
            await asyncio.sleep(_RNG.uniform(1, 2))
            await page.evaluate("window.scrollTo(0, 0);")
            await page.wait()
            await asyncio.sleep(_RNG.uniform(2, 3))
        except _CDP_OPERATION_ERRORS as e:
            logger.debug("Scroll behavior failed: %s", e)

        if await _is_bypassed(page):
            return True

        logger.debug("Trying page refresh...")
        await page.reload(ignore_cache=True)
        await asyncio.sleep(_RNG.uniform(5, 8))

        if await _is_bypassed(page):
            return True

        try:
            await page.solve_captcha()
            await asyncio.sleep(_RNG.uniform(3, 5))
        except _CDP_OPERATION_ERRORS as e:
            logger.debug("Final captcha click failed: %s", e)

        return await _is_bypassed(page)
    except _CDP_OPERATION_ERRORS as e:
        logger.debug("Human-like method failed: %s", e)
        return False


def _skip_solve_captcha_settle(solved: object, challenge_type: str) -> bool:
    """May we skip the 3-5s wait after a solve_captcha() that returned a hard False?

    Only for DDoS-Guard, and the distinction matters. SeleniumBase returns False from
    solve_captcha() at *two* places (undetected/cdp_driver/tab.py):

    * the challenge-family dispatch found none of the families it knows (Turnstile,
      reCAPTCHA, Incapsula hCaptcha, Friendly Captcha). DDoS-Guard always lands here
      and nothing is pending, so the wait afterwards is pure loss.
    * the page IS a Cloudflare challenge page (`__on_a_cf_turnstile_page` matched on
      "/challenge-platform/h/b/" or "challenges.cloudf"), but none of the ~15 turnstile
      widget selectors matched. That is the classic "Just a moment" JS interstitial:
      there is nothing to click, and the 3-5s wait is the actual working mechanism -
      the CF JS finishes during it and the next _is_bypassed() then passes.

    The return value alone cannot tell the two apart, so key off the challenge type we
    detected ourselves. Anything but a confirmed DDoS-Guard keeps waiting.
    """
    return solved is False and challenge_type == "ddos_guard"


async def _bypass_method_cdp_solve(page: Any, *, challenge_type: str = "") -> bool:
    """CDP Mode with solve_captcha() - auto-detects challenge type."""
    try:
        logger.debug("Attempting bypass: CDP solve_captcha")
        solved = await page.solve_captcha()
        if _skip_solve_captcha_settle(solved, challenge_type):
            logger.debug("solve_captcha cannot help with %s - skipping settle wait", challenge_type)
            return await _is_bypassed(page)
        await asyncio.sleep(_RNG.uniform(3, 5))
        return await _is_bypassed(page)
    except _CDP_OPERATION_ERRORS as e:
        logger.debug("CDP solve_captcha failed: %s", e)
        return False


_bypass_method_cdp_solve.wants_challenge_type = True  # type: ignore[attr-defined]


CDP_CLICK_SELECTORS = [
    "#turnstile-widget div",  # Cloudflare Turnstile
    "#cf-turnstile div",  # Alternative CF Turnstile
    "iframe[src*='challenges']",  # CF challenge iframe
    "input[type='checkbox']",  # Generic checkbox (DDOS-Guard)
    "[class*='checkbox']",  # Class-based checkbox
    "#challenge-running",  # CF challenge indicator
]


async def _bypass_method_cdp_click(page: Any) -> bool:
    """CDP Mode with native clicking - no PyAutoGUI dependency."""
    try:
        logger.debug("Attempting bypass: CDP native click")

        for selector in CDP_CLICK_SELECTORS:
            try:
                if not await page.is_element_visible(selector):
                    continue

                logger.debug("CDP clicking: %s", selector)
                await page.click(selector)
                await asyncio.sleep(_RNG.uniform(2, 4))

                if await _is_bypassed(page):
                    return True
            except _CDP_OPERATION_ERRORS as e:
                logger.debug("CDP click on '%s' failed: %s", selector, e)

        return await _is_bypassed(page)
    except _CDP_OPERATION_ERRORS as e:
        logger.debug("CDP Mode click failed: %s", e)
        return False


CDP_GUI_CLICK_SELECTORS = [
    "#turnstile-widget div",  # Cloudflare Turnstile
    "#cf-turnstile div",  # Alternative CF Turnstile
    "#challenge-stage div",  # CF challenge stage
    "input[type='checkbox']",  # Generic checkbox
    "[class*='cb-i']",  # DDOS-Guard checkbox
]


async def _bypass_method_cdp_gui_click(page: Any, *, challenge_type: str = "") -> bool:
    """CDP Mode with gui_click-style behavior."""
    try:
        logger.debug("Attempting bypass: CDP gui_click (mouse-based)")

        try:
            logger.debug("Trying solve_captcha()")
            solved = await page.solve_captcha()
            if _skip_solve_captcha_settle(solved, challenge_type):
                logger.debug(
                    "solve_captcha cannot help with %s - going straight to clicks", challenge_type
                )
            else:
                await asyncio.sleep(_RNG.uniform(3, 5))

                if await _is_bypassed(page):
                    return True
        except _CDP_OPERATION_ERRORS as e:
            logger.debug("solve_captcha() failed: %s", e)

        for selector in CDP_GUI_CLICK_SELECTORS:
            try:
                if not await page.is_element_visible(selector):
                    continue

                logger.debug("CDP click_with_offset: %s", selector)
                await page.click_with_offset(selector, 0, 0, center=True)
                await asyncio.sleep(_RNG.uniform(3, 5))

                if await _is_bypassed(page):
                    return True
            except _CDP_OPERATION_ERRORS as e:
                logger.debug("CDP gui_click on '%s' failed: %s", selector, e)

        return await _is_bypassed(page)
    except _CDP_OPERATION_ERRORS as e:
        logger.debug("CDP Mode gui_click failed: %s", e)
        return False


_bypass_method_cdp_gui_click.wants_challenge_type = True  # type: ignore[attr-defined]


async def _call_bypass_method(method: Any, page: Any, challenge_type: str) -> bool:
    """Invoke a bypass method, handing it the challenge type only if it takes one.

    Kept dynamic on purpose: the method list is monkeypatched in tests and could grow
    upstream, and a method that does not care about the challenge type must keep
    working with the plain (page) signature.
    """
    if getattr(method, "wants_challenge_type", False):
        return await method(page, challenge_type=challenge_type)
    return await method(page)


BYPASS_METHODS = [
    _bypass_method_cdp_solve,
    _bypass_method_cdp_gui_click,
    _bypass_method_cdp_click,
    _bypass_method_humanlike,
]

MAX_CONSECUTIVE_SAME_CHALLENGE = 3


def _ordered_bypass_methods(challenge_type: str, hint: str | None) -> list[Any]:
    """Order the live method list for this challenge, never removing a method.

    Derived from BYPASS_METHODS at call time so a monkeypatched list keeps working
    and so every method still gets its turn (issue #524). Two adjustments:

    * DDoS-Guard cannot be solved by _bypass_method_cdp_solve at all (SeleniumBase
      only knows Cloudflare/reCAPTCHA/Incapsula/Friendly captchas), so it moves to
      the back. Cloudflare hosts (welib.org, z-lib.fm) keep it in front, untouched.
    * A remembered method for this exact host/challenge combination goes first.
    """
    methods = list(BYPASS_METHODS)

    if challenge_type == "ddos_guard":
        deprioritized = [m for m in methods if m is _bypass_method_cdp_solve]
        if deprioritized:
            methods = [m for m in methods if m is not _bypass_method_cdp_solve] + deprioritized

    if hint:
        preferred = [m for m in methods if getattr(m, "__name__", "") == hint]
        if preferred:
            methods = preferred + [m for m in methods if m not in preferred]

    return methods


def _check_cancellation(cancel_flag: Event | None, message: str) -> None:
    """Check if cancellation was requested and raise if so."""
    if cancel_flag and cancel_flag.is_set():
        logger.info(message)
        msg = "Bypass cancelled"
        raise BypassCancelledError(msg)


async def _bypass(
    page: Any,
    max_retries: int | None = None,
    cancel_flag: Event | None = None,
    url: str = "",
) -> bool:
    """Attempt to bypass Cloudflare/DDOS-Guard protection using multiple methods."""
    max_retries = (
        max_retries if max_retries is not None else _coerce_positive_int(app_config.MAX_RETRY, 10)
    )

    last_challenge_type = None
    consecutive_same_challenge = 0
    # One counter per challenge type: a "none" round or a switch between protections
    # must not skip a method the way a shared modulo index would.
    attempts_per_challenge: dict[str, int] = {}
    # Allow at least one full pass through all bypass methods before aborting due to a "stuck" challenge.
    min_same_challenge_before_abort = max(MAX_CONSECUTIVE_SAME_CHALLENGE, len(BYPASS_METHODS) + 1)

    for try_count in range(max_retries):
        _check_cancellation(cancel_flag, "Bypass cancelled by user")

        if await _is_bypassed(page):
            if try_count == 0:
                logger.info("Page already bypassed")
            return True

        challenge_type = await _detect_challenge_type(page)
        logger.debug("Challenge detected: %s", challenge_type)

        # No challenge detected but page doesn't look bypassed - wait and retry
        if challenge_type == "none":
            logger.info("No challenge detected, waiting for page to settle...")
            await asyncio.sleep(_RNG.uniform(2, 3))
            if await _is_bypassed(page):
                return True
            # Try a simple refresh instead of captcha methods
            try:
                await page.reload(ignore_cache=True)
                await asyncio.sleep(_RNG.uniform(1, 2))
                if await _is_bypassed(page):
                    logger.info("Bypass successful after refresh")
                    return True
            except _CDP_OPERATION_ERRORS as e:
                logger.debug("Refresh during no-challenge wait failed: %s", e)
            continue

        if challenge_type == last_challenge_type:
            consecutive_same_challenge += 1
            if consecutive_same_challenge >= min_same_challenge_before_abort:
                logger.warning(
                    "Same challenge (%s) detected %s times - aborting",
                    challenge_type,
                    consecutive_same_challenge,
                )
                return False
        else:
            consecutive_same_challenge = 1
        last_challenge_type = challenge_type

        hint = get_bypass_method_hint(url, challenge_type)
        methods = _ordered_bypass_methods(challenge_type, hint)
        method_index = attempts_per_challenge.get(challenge_type, 0)
        attempts_per_challenge[challenge_type] = method_index + 1
        method = methods[method_index % len(methods)]
        logger.info("Bypass attempt %s/%s using %s", try_count + 1, max_retries, method.__name__)

        if try_count > 0:
            # Deliberately unchanged. An earlier revision shortened this to ~1-2s
            # between two *different* methods, on the theory that only a reload hits
            # the origin. That theory does not survive the code: cdp_click clicks every
            # CDP_CLICK_SELECTORS hit, cdp_gui_click clicks the DDoS-Guard checkbox
            # ("[class*=cb-i]") and cdp_solve clicks the turnstile widget - each of
            # those is a verification request. On the only path to Anna's Archive, and
            # combined with a browser that now keeps one fingerprint across solves, the
            # ~2s per method switch is the worst risk/reward in this change.
            wait_time = min(_RNG.uniform(2, 4) * try_count, 12)
            logger.info("Waiting %0.1fs before trying...", wait_time)
            for _ in range(int(wait_time)):
                _check_cancellation(cancel_flag, "Bypass cancelled during wait")
                await asyncio.sleep(1)
            await asyncio.sleep(wait_time - int(wait_time))

        try:
            if await _call_bypass_method(method, page, challenge_type):
                logger.info("Bypass successful using %s", method.__name__)
                record_bypass_method_result(url, challenge_type, method.__name__, success=True)
                return True
        except BypassCancelledError:
            raise
        except _CDP_OPERATION_ERRORS as e:
            logger.warning("Exception in %s: %s", method.__name__, e)

        record_bypass_method_result(url, challenge_type, method.__name__, success=False)
        logger.info("Bypass method %s failed.", method.__name__)

    logger.warning("Exceeded maximum retries. Bypass failed.")
    return False


def _get_browser_args() -> list[str]:
    """Build extra Chrome arguments, pre-resolving hostnames via patched DNS.

    Pre-resolves AA hostnames and passes IPs to Chrome via --host-resolver-rules,
    bypassing Chrome's DNS entirely for those hosts.
    """
    arguments = [
        "--ignore-certificate-errors",
        "--ignore-ssl-errors",
        "--allow-running-insecure-content",
        "--ignore-certificate-errors-spki-list",
        "--ignore-certificate-errors-skip-list",
        # Chrome 144+ disabled automatic SwiftShader fallback for WebGL (security reasons).
        # Without this flag, WebGL is broken in headless/Docker which triggers bot detection.
        # See: https://issues.chromium.org/issues/40277080
        "--enable-unsafe-swiftshader",
    ]

    if app_config.get("DEBUG", False):
        arguments.extend(
            ["--enable-logging", "--v=1", "--log-file=" + str(LOG_DIR / "chrome_browser.log")]
        )

    host_rules = _build_host_resolver_rules()
    if host_rules:
        arguments.append(f"--host-resolver-rules={', '.join(host_rules)}")
        logger.debug("Chrome: Using host resolver rules for %s hosts", len(host_rules))
    else:
        logger.warning("Chrome: No hosts could be pre-resolved")

    return arguments


def _build_host_resolver_rules() -> list[str]:
    """Pre-resolve AA hostnames and build Chrome host resolver rules."""
    host_rules = []

    try:
        for url in network.get_available_aa_urls():
            hostname = urlparse(url).hostname
            if not hostname:
                continue

            try:
                results = socket.getaddrinfo(hostname, 443, socket.AF_INET)
                if results:
                    ip = results[0][4][0]
                    host_rules.append(f"MAP {hostname} {ip}")
                    logger.debug("Chrome: Pre-resolved %s -> %s", hostname, ip)
                else:
                    logger.warning("Chrome: No addresses returned for %s", hostname)
            except socket.gaierror as e:
                logger.warning("Chrome: Could not pre-resolve %s: %s", hostname, e)
    except (OSError, RuntimeError, TypeError, ValueError) as e:
        logger.error_trace(f"Error pre-resolving hostnames for Chrome: {e}")

    return host_rules


DRIVER_RESET_ERRORS = {"ProtocolException", "RuntimeError", "TimeoutError"}


async def _get(url: str, driver: Any, cancel_flag: Event | None = None) -> str:
    """Fetch URL with Cloudflare bypass using a CDP browser."""
    _check_cancellation(cancel_flag, "Bypass cancelled before starting")

    logger.debug("CDP_GET: %s", url)

    logger.debug("Opening URL with SeleniumBase CDP...")
    page = await driver.get(url)
    with suppress(Exception):
        await page.wait()

    _check_cancellation(cancel_flag, "Bypass cancelled after page load")

    try:
        current_url = await page.get_current_url()
        title = await page.get_title()
        logger.debug("Page loaded - URL: %s, Title: %s", current_url, title)
    except _CDP_OPERATION_ERRORS as e:
        logger.debug("Could not get page info: %s", e)

    logger.debug("Starting bypass process...")
    if await _bypass(page, cancel_flag=cancel_flag, url=url):
        await _extract_cookies_from_cdp(driver, page, url)
        return await page.get_page_source()

    logger.warning("Bypass completed but page still shows protection")
    try:
        body = await page.evaluate("document.body ? document.body.innerText : ''")
        if body:
            preview = body
            if len(body) > _PAGE_BODY_PREVIEW_CHARS:
                preview = body[:_PAGE_BODY_PREVIEW_CHARS] + "..."
            logger.debug("Page content: %s", preview)
    except _CDP_OPERATION_ERRORS as exc:
        logger.debug("Could not inspect protected page body: %s", exc)

    return ""


# Warm browser state. Only ever populated inside the helper child process, where
# the process death remains the reaper of last resort for Chrome and Xvfb.
_WARM_BROWSER: dict[str, Any] = {
    "driver": None,
    "created_at": 0.0,
    "uses": 0,
    "proxy": None,
}

# The child keeps ONE event loop for its whole life: a driver is bound to the loop
# that created it, so asyncio.run() per request would make reuse impossible.
_CHILD_LOOP: asyncio.AbstractEventLoop | None = None


def _is_bypasser_child() -> bool:
    """True inside the isolated helper subprocess."""
    return os.environ.get(_BYPASS_CHILD_ENV) == "1"


def _warm_browser_enabled() -> bool:
    """Browser reuse only happens in the helper child, never in the gunicorn worker."""
    return _is_bypasser_child()


def _chrome_pid(driver: Any) -> int | None:
    """The Chrome PID SeleniumBase recorded for this driver, if any.

    Set in cdp_driver/browser.py at start (`self._process_pid = self._process.pid`)
    and reset to None by stop(), so "missing" means "not a browser we may reuse".
    """
    pid = getattr(driver, "_process_pid", None)
    if not pid:
        return None
    try:
        return int(pid)
    except TypeError, ValueError:
        return None


def _chrome_process_alive(driver: Any) -> bool:
    """Liveness check of the Chrome process behind a driver.

    An unknown PID counts as *not* alive on purpose: we cannot verify such a browser,
    and refusing to reuse it only costs one browser start (today's behaviour), while
    reusing a dead one costs the whole solve.
    """
    pid = _chrome_pid(driver)
    if pid is None:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True


def _chrome_tree_rss_mb(driver: Any) -> float | None:
    """RSS of Chrome plus its renderer children, in MB. None when unknown."""
    pid = _chrome_pid(driver)
    if pid is None:
        return None
    try:
        import psutil
    except ImportError:
        return None
    try:
        proc = psutil.Process(pid)
        total = proc.memory_info().rss
        for child in proc.children(recursive=True):
            with suppress(psutil.Error, OSError):
                total += child.memory_info().rss
    except psutil.Error, OSError, ValueError:
        return None
    return total / (1024 * 1024)


def _warm_browser_retire_reason(url: str) -> str | None:
    """Why the warm browser must not serve this request (None = reuse it)."""
    state = _WARM_BROWSER
    driver = state.get("driver")
    if driver is None:
        return None
    if state.get("proxy") != _get_proxy_string(url):
        return "proxy for this URL differs"
    age = time.monotonic() - float(state.get("created_at") or 0.0)
    if age > _WARM_BROWSER_MAX_AGE_SECONDS:
        return f"max age reached ({age:.0f}s)"
    uses = int(state.get("uses") or 0)
    if uses >= _WARM_BROWSER_MAX_USES:
        return f"max uses reached ({uses})"
    if not _chrome_process_alive(driver):
        return "chrome process is gone"
    if _WARM_BROWSER_MAX_RSS_MB > 0:
        rss_mb = _chrome_tree_rss_mb(driver)
        if rss_mb is not None and rss_mb > _WARM_BROWSER_MAX_RSS_MB:
            # The container runs at mem_limit 512m. A browser that keeps growing across
            # solves must be retired here, not by the OOM killer - that one takes the
            # whole container, and with it the only path to Anna's Archive.
            return f"chrome rss {rss_mb:.0f}MB over the {_WARM_BROWSER_MAX_RSS_MB:.0f}MB limit"
    return None


async def _discard_cdp_driver(driver: Any) -> None:
    """Close a driver and forget it as the warm instance."""
    if _WARM_BROWSER.get("driver") is driver:
        _WARM_BROWSER.update(driver=None, created_at=0.0, uses=0, proxy=None)
    await _close_cdp_driver(driver)


async def _acquire_cdp_driver(url: str) -> tuple[Any, bool]:
    """Return (driver, reused). Falls back to a fresh browser whenever in doubt."""
    if not _warm_browser_enabled():
        return await _create_cdp_browser(url), False

    state = _WARM_BROWSER
    driver = state.get("driver")
    if driver is not None:
        reason = _warm_browser_retire_reason(url)
        if reason:
            logger.info("Retiring warm Chrome browser: %s", reason)
            await _discard_cdp_driver(driver)
            driver = None

    if driver is not None:
        state["uses"] = int(state.get("uses") or 0) + 1
        rss_mb = _chrome_tree_rss_mb(driver)
        logger.info(
            "Reusing warm Chrome browser (use %s/%s, age %.0fs, chrome rss %s)",
            state["uses"],
            _WARM_BROWSER_MAX_USES,
            time.monotonic() - float(state.get("created_at") or 0.0),
            f"{rss_mb:.0f}MB" if rss_mb is not None else "unknown",
        )
        # _create_cdp_browser/_close_cdp_driver log this, and a reused browser passes
        # through neither - without this line a whole batch of solves leaves no memory
        # trace at all, which is the one thing an OOM post-mortem needs.
        logger.log_resource_usage()
        return driver, True

    driver = await _create_cdp_browser(url)
    state.update(driver=driver, created_at=time.monotonic(), uses=1, proxy=_get_proxy_string(url))
    return driver, False


async def _restart_cdp_driver(driver: Any, url: str) -> Any:
    """Throw a browser away and put a fresh one in its place."""
    await _discard_cdp_driver(driver)
    new_driver, _ = await _acquire_cdp_driver(url)
    return new_driver


async def _release_cdp_driver(driver: Any, *, keep_warm: bool) -> None:
    """Hand a driver back: keep it warm after a clean solve, close it otherwise."""
    if driver is None:
        return
    if keep_warm and _warm_browser_enabled() and _WARM_BROWSER.get("driver") is driver:
        logger.debug("Keeping Chrome warm for the next request")
        return
    await _discard_cdp_driver(driver)


async def _shutdown_warm_browser() -> None:
    """Close the warm browser if there is one."""
    driver = _WARM_BROWSER.get("driver")
    if driver is not None:
        await _discard_cdp_driver(driver)


def _get_child_event_loop() -> asyncio.AbstractEventLoop:
    """Return the child's long-lived event loop, creating it on first use."""
    global _CHILD_LOOP
    if _CHILD_LOOP is None or _CHILD_LOOP.is_closed():
        _CHILD_LOOP = asyncio.new_event_loop()
        asyncio.set_event_loop(_CHILD_LOOP)
    return _CHILD_LOOP


def _close_child_event_loop() -> None:
    """Cancel leftover CDP tasks and close the child's event loop."""
    global _CHILD_LOOP
    loop = _CHILD_LOOP
    _CHILD_LOOP = None
    if loop is None or loop.is_closed():
        return
    with suppress(*_CDP_OPERATION_ERRORS):
        pending = [task for task in asyncio.all_tasks(loop) if not task.done()]
        for task in pending:
            task.cancel()
        if pending:
            loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
    with suppress(*_CDP_OPERATION_ERRORS):
        loop.run_until_complete(loop.shutdown_asyncgens())
    with suppress(*_CDP_OPERATION_ERRORS):
        loop.close()


def _run_bypass_in_current_process(url: str, retry: int, cancel_flag: Event | None = None) -> str:
    """Run the CDP bypass in the current process, reusing a warm browser if allowed."""

    async def _run_bypass() -> str:
        driver = None
        reused = False
        succeeded = False
        try:
            driver, reused = await _acquire_cdp_driver(url)

            for attempt in range(retry):
                _check_cancellation(cancel_flag, "Bypass cancelled before attempt")

                try:
                    result = await _get(url, driver, cancel_flag)
                    if result:
                        succeeded = True
                        return result
                    # The dominant failure is NOT an exception: _bypass() exhausts its
                    # methods, returns False, and _get() falls through to "". Without
                    # this branch every remaining attempt would run on the same warm
                    # session - and a session DDoS-Guard has already judged badly does
                    # not get better by being asked ten more times.
                    if reused:
                        logger.info(
                            "Warm Chrome produced nothing (attempt %s/%s) - "
                            "falling back to a fresh browser",
                            attempt + 1,
                            retry,
                        )
                        # Cleared first: if the restart itself fails, the exception
                        # handler below must not try to swap the browser a second time.
                        reused = False
                        driver = await _restart_cdp_driver(driver, url)
                except BypassCancelledError:
                    raise
                except _CDP_OPERATION_ERRORS as e:
                    error_details = f"{type(e).__name__}: {e}"
                    logger.warning(
                        "Bypass failed (attempt %s/%s): %s", attempt + 1, retry, error_details
                    )
                    logger.debug("Stack trace: %s", traceback.format_exc())

                    if reused:
                        # A reused browser is guilty until proven innocent: drop straight
                        # back to the pre-warm behaviour (fresh Chrome for this solve).
                        logger.info("Warm Chrome failed - falling back to a fresh browser")
                        reused = False
                        driver = await _restart_cdp_driver(driver, url)
                    elif type(e).__name__ in DRIVER_RESET_ERRORS:
                        # On CDP errors, quit and create a fresh browser
                        logger.info("Restarting Chrome due to browser error...")
                        driver = await _restart_cdp_driver(driver, url)

            logger.error("Bypass failed after %s attempts", retry)
            return ""
        finally:
            # Only a browser that just produced a page is worth keeping; anything else
            # may be parked on a challenge page or half dead.
            await _release_cdp_driver(driver, keep_warm=succeeded)

    if _is_bypasser_child():
        return _get_child_event_loop().run_until_complete(_run_bypass())
    return _CDP_WORKER.run(_run_bypass())


def _store_child_bypass_state(payload: dict[str, Any]) -> None:
    # Replace, do not merge: the child's snapshot is authoritative (see
    # replace_bypass_method_hints). Only touch it when the child actually sent one -
    # an older helper image would otherwise wipe the parent's memory.
    if isinstance(payload.get("method_hints"), dict):
        replace_bypass_method_hints(payload["method_hints"])

    cookies = payload.get("cookies")
    if isinstance(cookies, dict):
        with _cf_cookies_lock:
            _cf_cookies.update(cookies)

    user_agents = payload.get("user_agents")
    if isinstance(user_agents, dict):
        with _cf_cookies_lock:
            _cf_user_agents.update(
                {str(domain): str(agent) for domain, agent in user_agents.items()}
            )


def _prepare_child_browser_env(env_vars: dict[str, str]) -> dict[str, str]:
    """Force writable browser runtime paths for the helper subprocess."""
    home_dir = BROWSER_HOME_DIR
    config_dir = home_dir / ".config"
    cache_dir = home_dir / ".cache"
    runtime_dir = BROWSER_XDG_RUNTIME_DIR

    for path in (home_dir, config_dir, cache_dir, runtime_dir):
        path.mkdir(parents=True, exist_ok=True)

    with suppress(OSError):
        runtime_dir.chmod(stat.S_IRWXU)

    env_vars["HOME"] = str(home_dir)
    env_vars["XDG_CONFIG_HOME"] = str(config_dir)
    env_vars["XDG_CACHE_HOME"] = str(cache_dir)
    env_vars["XDG_RUNTIME_DIR"] = str(runtime_dir)
    return env_vars


class _BypassHelperTransportError(RuntimeError):
    """The helper process could not be talked to - retry on a fresh one."""


class _BypassHelperProcess:
    """A pooled helper child that keeps its Chrome warm across several requests.

    The isolation boundary is unchanged from the one-shot design (same `python -m`
    child, same env, same gevent-free context) - only the number of requests served
    per process changes. Keeping the process short-lived per *batch* rather than
    per *request* preserves the one mechanism that reliably reaps Xvfb: the child
    dying.
    """

    def __init__(self) -> None:
        env_vars = os.environ.copy()
        env_vars[_BYPASS_CHILD_ENV] = "1"
        env_vars = _prepare_child_browser_env(env_vars)
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "shelfmark.bypass.internal_bypasser"],
            stdin=subprocess.PIPE,
            text=True,
            env=env_vars,
            # Own session so a kill takes Chrome and Xvfb with it. Without this the
            # old timeout path killed only the Python process and left the browser
            # behind - which poisoned every later solve.
            start_new_session=True,
        )
        # start_new_session runs setsid() in the child, so the child IS its own process
        # group leader and the group id equals its pid. Remember it NOW: once the child
        # is reaped, os.getpgid(pid) fails, while the group itself lives on as long as
        # Chrome or Xvfb are still in it - which is exactly the case we must be able to
        # clean up. The theoretical cost is PID reuse between the reap and the kill;
        # in a single-purpose container with a kill following within seconds that is
        # far cheaper than leaving a browser behind that poisons every later solve.
        self.pgid: int | None = self.proc.pid
        self.created_at = time.monotonic()
        self.last_used_at = self.created_at
        self.requests = 0

    @property
    def pid(self) -> int:
        return self.proc.pid

    def is_alive(self) -> bool:
        return self.proc.poll() is None

    def idle_seconds(self) -> float:
        return time.monotonic() - self.last_used_at

    def age_seconds(self) -> float:
        return time.monotonic() - self.created_at

    def is_expired(self) -> bool:
        return self.age_seconds() > _HELPER_MAX_AGE_SECONDS or self.requests >= _HELPER_MAX_REQUESTS

    def request(self, url: str, retry: int, cancel_flag: Event | None = None) -> dict[str, Any]:
        """Send one request and wait for its result file."""
        if not self.is_alive():
            msg = f"Bypass helper {self.pid} is not running"
            raise _BypassHelperTransportError(msg)

        result_path = (
            Path(tempfile.gettempdir()) / f"shelfmark-bypass-{os.getpid()}-{time.time_ns()}.json"
        )
        payload = {
            "url": url,
            "retry": retry,
            "result_path": str(result_path),
            "method_hints": get_bypass_method_hints(),
        }

        self.requests += 1
        self.last_used_at = time.monotonic()
        stdin = self.proc.stdin
        if stdin is None:
            msg = f"Bypass helper {self.pid} has no stdin pipe"
            raise _BypassHelperTransportError(msg)
        try:
            stdin.write(json.dumps(payload) + "\n")
            stdin.flush()
        except (OSError, ValueError) as exc:
            msg = f"Could not send a request to bypass helper {self.pid}: {exc}"
            raise _BypassHelperTransportError(msg) from exc

        try:
            return self._await_result(result_path, cancel_flag)
        finally:
            self.last_used_at = time.monotonic()
            with suppress(OSError):
                result_path.unlink()

    def _await_result(self, result_path: Path, cancel_flag: Event | None) -> dict[str, Any]:
        """Poll for the child's result file.

        Polling (instead of reading a pipe) keeps the child's stdout/stderr inherited,
        so its log lines still reach the container log untouched, and it stays
        cooperative under gevent because time.sleep is monkeypatched.
        """
        deadline = time.monotonic() + _BYPASS_SUBPROCESS_TIMEOUT_SECONDS
        while not result_path.exists():
            _check_cancellation(cancel_flag, "Bypass cancelled while waiting for helper")
            if not self.is_alive():
                if result_path.exists():
                    break
                msg = f"Bypass helper exited without a result (code {self.proc.returncode})"
                raise _BypassHelperTransportError(msg)
            if time.monotonic() > deadline:
                msg = "Internal bypasser helper process timed out"
                raise TimeoutError(msg)
            time.sleep(_HELPER_RESULT_POLL_SECONDS)

        try:
            result = json.loads(result_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            msg = f"Could not read the bypass helper result: {exc}"
            raise _BypassHelperTransportError(msg) from exc

        if not isinstance(result, dict):
            msg = "Internal bypasser helper returned an invalid result"
            raise _BypassHelperTransportError(msg)
        return result

    def shutdown(self, *, force: bool = False) -> None:
        """Stop the helper and guarantee its Chrome/Xvfb go with it."""
        proc = self.proc
        if not force and proc.poll() is None:
            # Closing stdin is the graceful stop: the child leaves its read loop and
            # closes the browser on the way out.
            with suppress(OSError, ValueError):
                if proc.stdin is not None:
                    proc.stdin.close()
            try:
                proc.wait(timeout=_HELPER_GRACEFUL_EXIT_SECONDS)
            except subprocess.TimeoutExpired:
                logger.warning("Bypass helper %s ignored the graceful stop", self.pid)

        with suppress(OSError, ValueError):
            if proc.stdin is not None:
                proc.stdin.close()

        # Unconditionally, NOT only while the Python child still runs. A container OOM
        # kill or a segfault takes out exactly that one process, and it never reaches
        # its finally - Chrome and Xvfb then survive in the group as orphans and poison
        # every later solve ("Pure CDP browser startup failed" in series). The group is
        # a no-op when it is already empty, so this costs nothing on the clean path.
        self._kill_process_group()

        with suppress(*_SUBPROCESS_OPERATION_ERRORS):
            proc.wait(timeout=_HELPER_KILL_GRACE_SECONDS)

    def _process_group_alive(self) -> bool:
        """Does the helper's process group still have members?"""
        if self.pgid is None:
            return False
        # Reap the child first, so a zombie leader does not keep the group "alive".
        with suppress(*_SUBPROCESS_OPERATION_ERRORS):
            self.proc.poll()
        try:
            os.killpg(self.pgid, 0)
        except ProcessLookupError:
            return False
        except OSError:
            return True
        return True

    def _kill_process_group(self) -> None:
        """SIGTERM then SIGKILL the helper's own process group (Chrome/Xvfb included)."""
        proc = self.proc
        pgid = self.pgid
        own_pgid = None
        with suppress(OSError):
            own_pgid = os.getpgid(0)

        # Never signal our own group - that would take the gunicorn worker with it.
        if pgid is None or pgid == own_pgid:
            if proc.poll() is None:
                logger.warning("Killing bypass helper %s without its process group", proc.pid)
                with suppress(OSError):
                    proc.kill()
            return

        if not self._process_group_alive():
            # Clean exit: leader gone, no browser left behind. Nothing to do.
            return

        logger.warning("Killing bypass helper process group %s (browser included)", pgid)
        with suppress(OSError):
            os.killpg(pgid, signal.SIGTERM)

        if not self._wait_for_process_group_exit(_HELPER_KILL_GRACE_SECONDS):
            logger.warning("Bypass helper group %s survived SIGTERM - sending SIGKILL", pgid)
            with suppress(OSError):
                os.killpg(pgid, signal.SIGKILL)

    def _wait_for_process_group_exit(self, timeout: float) -> bool:
        """Wait for the group to empty out. False means it is still there."""
        deadline = time.monotonic() + timeout
        while True:
            if not self._process_group_alive():
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(_HELPER_RESULT_POLL_SECONDS)


_HELPER_STATE: dict[str, Any] = {"helper": None, "reaper": False}


def _shutdown_helper(*, force: bool = False) -> None:
    """Stop the pooled helper, if any. Caller must hold LOCKED (or be exiting)."""
    helper = _HELPER_STATE.get("helper")
    _HELPER_STATE["helper"] = None
    if helper is None:
        return
    logger.info(
        "Stopping bypass helper %s after %s request(s), %.0fs alive",
        helper.pid,
        helper.requests,
        helper.age_seconds(),
    )
    helper.shutdown(force=force)


def _acquire_helper() -> _BypassHelperProcess:
    """Return a usable helper, starting or recycling one as needed."""
    helper = _HELPER_STATE.get("helper")
    if helper is not None:
        if not helper.is_alive():
            logger.warning("Bypass helper %s died between requests", helper.pid)
            _shutdown_helper(force=True)
            helper = None
        elif helper.is_expired():
            logger.info("Recycling bypass helper %s (age/request limit)", helper.pid)
            _shutdown_helper()
            helper = None

    if helper is None:
        helper = _BypassHelperProcess()
        _HELPER_STATE["helper"] = helper
        logger.info("Started bypass helper process %s", helper.pid)
        _start_helper_reaper()
    return helper


def _reap_idle_helper() -> None:
    """Shut the helper down when it has been idle long enough. One reaper tick."""
    helper = _HELPER_STATE.get("helper")
    if helper is None:
        return
    if helper.is_alive() and helper.idle_seconds() < _HELPER_IDLE_TIMEOUT_SECONDS:
        return
    # A held lock means a solve is in flight, so the helper is not idle after all.
    if not LOCKED.acquire(blocking=False):
        return
    try:
        helper = _HELPER_STATE.get("helper")
        if helper is None:
            return
        if helper.is_alive() and helper.idle_seconds() < _HELPER_IDLE_TIMEOUT_SECONDS:
            return
        logger.info(
            "Bypass helper %s idle for %.0fs - releasing its browser",
            helper.pid,
            helper.idle_seconds(),
        )
        _shutdown_helper()
    finally:
        LOCKED.release()


def _helper_reaper_loop() -> None:
    while True:
        time.sleep(_HELPER_REAPER_INTERVAL_SECONDS)
        try:
            _reap_idle_helper()
        except _SUBPROCESS_OPERATION_ERRORS as exc:
            logger.debug("Bypass helper reaper tick failed: %s", exc)


def _start_helper_reaper() -> None:
    """Start the idle reaper once; it bounds how long a warm Chrome may sit around."""
    if _HELPER_STATE.get("reaper"):
        return
    _HELPER_STATE["reaper"] = True
    atexit.register(_shutdown_helper_at_exit)
    thread = threading.Thread(target=_helper_reaper_loop, name="bypass-helper-reaper", daemon=True)
    thread.start()


def _shutdown_helper_at_exit() -> None:
    """Last line of defence against a helper (and its Chrome) outliving the app."""
    with suppress(*_SUBPROCESS_OPERATION_ERRORS):
        _shutdown_helper(force=True)


def _get_via_subprocess(url: str, retry: int, cancel_flag: Event | None = None) -> str:
    """Run the browser bypass in a pooled helper process isolated from gunicorn/gevent."""
    _check_cancellation(cancel_flag, "Bypass cancelled before helper process")

    last_error: Exception | None = None
    for attempt in range(2):
        helper = _acquire_helper()
        try:
            result = helper.request(url, retry, cancel_flag)
        except BypassCancelledError:
            # Nobody is waiting for this page any more; do not let the child keep
            # driving Chrome for it.
            _shutdown_helper(force=True)
            raise
        except TimeoutError:
            _shutdown_helper(force=True)
            raise
        except _BypassHelperTransportError as exc:
            last_error = exc
            logger.warning("Bypass helper transport failure (attempt %s/2): %s", attempt + 1, exc)
            _shutdown_helper(force=True)
            continue

        if not result.get("ok"):
            # The child retires itself after a failed request, so reap it now instead
            # of leaving a zombie until the next request or reaper tick.
            _shutdown_helper()
            error_type = result.get("error_type", "RuntimeError")
            error = result.get("error", "Internal bypasser helper failed")
            trace = result.get("traceback")
            if trace:
                logger.debug("Internal bypasser helper traceback: %s", trace)
            msg = f"{error_type}: {error}"
            raise RuntimeError(msg)

        _store_child_bypass_state(result)
        html = result.get("html", "")
        return html if isinstance(html, str) else ""

    msg = f"Internal bypasser helper failed: {last_error}"
    raise RuntimeError(msg)


def get(url: str, retry: int | None = None, cancel_flag: Event | None = None) -> str:
    """Fetch a URL with protection bypass.

    In Docker the work goes to a pooled helper subprocess that keeps one Chrome warm
    across consecutive requests; everywhere else (and whenever reuse is not possible)
    a fresh browser is created per solve, exactly as before.
    """
    retry = retry if retry is not None else _coerce_positive_int(app_config.MAX_RETRY, 10)

    with LOCKED:
        # Try cookies first - another request may have completed bypass while waiting.
        # Skipped inside the helper child: the parent just ran exactly this check with
        # exactly these cookies before handing the request over, so repeating it only
        # buys another futile round trip on the hot path.
        if not _is_bypasser_child():
            cached_result = _try_with_cached_cookies(url, urlparse(url).hostname or "")
            if cached_result:
                return cached_result

        if env.DOCKERMODE and not _is_bypasser_child():
            return _get_via_subprocess(url, retry, cancel_flag)
        return _run_bypass_in_current_process(url, retry, cancel_flag)


def _get_proxy_string(url: str) -> str | None:
    """Return a single proxy string for CDP, honoring NO_PROXY."""
    proxies = get_proxies(url)
    if not proxies:
        return None
    proxy_url = proxies.get("https") or proxies.get("http")
    return proxy_url or None


async def _create_cdp_browser(url: str) -> Any:
    """Create a fresh CDP browser instance."""
    browser_args = _get_browser_args()
    screen_width, screen_height = get_screen_size()
    display_width = screen_width + 100
    display_height = screen_height + 150
    proxy = _get_proxy_string(url)

    logger.debug("Creating Pure CDP browser with args: %s", browser_args)
    logger.debug("Browser screen size: %sx%s", screen_width, screen_height)

    try:
        driver = await asyncio.wait_for(
            cdp_driver.start_async(
                headless=False,
                headed=False,
                xvfb=True,
                xvfb_metrics=f"{display_width},{display_height}",
                sandbox=False,
                lang="en",
                incognito=True,
                ad_block=True,
                proxy=proxy,
                browser_args=browser_args,
            ),
            timeout=_BROWSER_START_TIMEOUT_SECONDS,
        )
    except TimeoutError:
        logger.warning(
            "Pure CDP browser startup timed out after %.0fs",
            _BROWSER_START_TIMEOUT_SECONDS,
        )
        if env.DOCKERMODE:
            _cleanup_orphan_processes()
        raise
    except Exception as e:
        logger.warning("Pure CDP browser startup failed: %s: %s", type(e).__name__, e)
        logger.warning(
            "SeleniumBase runtime paths: cwd=%s; %s; %s; %s; %s",
            Path.cwd(),
            _describe_runtime_path(SELENIUMBASE_DOWNLOADS_DIR),
            _describe_runtime_path("/app/downloaded_files"),
            _describe_runtime_path("downloaded_files"),
            _describe_runtime_path(tempfile.gettempdir()),
        )
        if env.DOCKERMODE:
            _cleanup_orphan_processes()
        msg = f"Pure CDP browser startup failed: {e}"
        raise RuntimeError(msg) from e

    if _has_window_rect_page(driver):
        try:
            await driver.page.set_window_rect(0, 0, screen_width, screen_height)
        except _CDP_OPERATION_ERRORS as e:
            logger.debug("Failed to set window size: %s", e)

    # Start FFmpeg recording if debug mode (record each bypass session)
    if app_config.get("DEBUG", False) and not DISPLAY.get("ffmpeg"):
        _start_ffmpeg_recording(display=os.environ.get("DISPLAY", ":0"))

    await asyncio.sleep(_BROWSER_SETTLE_SECONDS)
    logger.info("Chrome browser ready (Pure CDP)")
    logger.log_resource_usage()
    return driver


async def _close_cdp_driver(driver: Any) -> None:
    """Close CDP connections and stop the browser."""
    if not driver:
        return

    logger.debug("Quitting Chrome browser (CDP)...")

    _stop_ffmpeg_recording()

    try:
        connections = []
        if hasattr(driver, "connection") and driver.connection:
            connections.append(driver.connection)
        if hasattr(driver, "targets") and driver.targets:
            connections.extend(driver.targets)
        for conn in connections:
            await _close_websocket_connection(conn)
    except _CDP_OPERATION_ERRORS as e:
        logger.debug("Error during connection cleanup: %s", e)

    try:
        driver.stop()
        logger.debug("Stopped CDP browser")
    except _CDP_OPERATION_ERRORS as e:
        logger.debug("CDP stop: %s", e)

    if env.DOCKERMODE:
        await asyncio.sleep(0.3)
        try:
            pid = getattr(driver, "_process_pid", None)

            def _pid_alive(check_pid: int) -> bool:
                try:
                    os.kill(check_pid, 0)
                except ProcessLookupError:
                    return False
                except PermissionError:
                    return True
                return True

            if pid and _pid_alive(pid):
                try:
                    os.kill(pid, signal.SIGTERM)
                    await asyncio.sleep(0.1)
                    if _pid_alive(pid):
                        os.kill(pid, signal.SIGKILL)
                    logger.debug("Killed Chrome pid %s", pid)
                except (OSError, RuntimeError, TypeError, ValueError) as e:
                    logger.debug("Failed to kill Chrome pid %s: %s", pid, e)
        except (OSError, RuntimeError, TypeError, ValueError) as e:
            logger.debug("Process cleanup failed: %s", e)

    logger.log_resource_usage()


async def _close_websocket_connection(conn: Any) -> None:
    """Close one websocket-like connection, ignoring best-effort failures."""
    try:
        await conn.aclose()
    except _CDP_OPERATION_ERRORS as e:
        logger.debug("Failed to close websocket connection: %s", e)


def _start_ffmpeg_recording(display: str) -> None:
    """Start FFmpeg screen recording for debug mode."""
    global DISPLAY
    RECORDING_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(UTC).strftime("%y%m%d-%H%M%S")
    output_file = RECORDING_DIR / f"screen_recording_{timestamp}.mp4"

    screen_width, screen_height = get_screen_size()
    display_width = screen_width + 100
    display_height = screen_height + 150

    ffmpeg_cmd = [
        "ffmpeg",
        "-y",
        "-f",
        "x11grab",
        "-video_size",
        f"{display_width}x{display_height}",
        "-i",
        display,
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast",
        "-maxrate",
        "700k",
        "-bufsize",
        "1400k",
        "-crf",
        "36",
        "-pix_fmt",
        "yuv420p",
        "-tune",
        "animation",
        "-x264-params",
        "bframes=0:deblock=-1,-1",
        "-r",
        "15",
        "-an",
        output_file.as_posix(),
        "-nostats",
        "-loglevel",
        "0",
    ]
    logger.debug("Starting FFmpeg recording to %s", output_file)
    logger.debug_trace(f"FFmpeg command: {' '.join(ffmpeg_cmd)}")
    DISPLAY["ffmpeg"] = subprocess.Popen(ffmpeg_cmd)
    DISPLAY["ffmpeg_output"] = output_file


def _stop_ffmpeg_recording() -> None:
    """Stop FFmpeg screen recording if running."""
    import signal

    global DISPLAY
    proc = DISPLAY.get("ffmpeg")
    if not proc:
        return
    if proc.poll() is not None:
        logger.debug("FFmpeg already stopped")
        DISPLAY["ffmpeg"] = None
        DISPLAY["ffmpeg_output"] = None
        return
    try:
        proc.send_signal(signal.SIGINT)
        proc.wait(timeout=5)
        logger.debug("Stopped ffmpeg recording")
    except _SUBPROCESS_OPERATION_ERRORS as e:
        logger.debug("ffmpeg stop: %s", e)
        with suppress(Exception):
            proc.terminate()
            proc.wait(timeout=2)
        with suppress(Exception):
            proc.kill()
    DISPLAY["ffmpeg"] = None
    DISPLAY["ffmpeg_output"] = None


def _try_with_cached_cookies(url: str, hostname: str) -> str | None:
    """Attempt request with cached cookies before using Chrome."""
    cookies = get_cf_cookies_for_domain(hostname)
    if not cookies:
        return None

    try:
        headers = {}
        stored_ua = get_cf_user_agent_for_domain(hostname)
        if stored_ua:
            headers["User-Agent"] = stored_ua

        logger.debug("Trying request with cached cookies: %s", url)
        response = requests.get(
            url,
            cookies=cookies,
            headers=headers,
            proxies=get_proxies(url),
            timeout=(5, 10),
            verify=get_ssl_verify(url),
        )
        if response.status_code == HTTPStatus.OK:
            logger.debug("Cached cookies worked, skipped Chrome bypass")
            return response.text
    except _REQUEST_OPERATION_ERRORS as exc:
        logger.debug("Cached cookie retry failed for %s: %s", url, exc)

    return None


def get_bypassed_page(
    url: str, selector: network.AAMirrorSelector | None = None, cancel_flag: Event | None = None
) -> str | None:
    """Fetch HTML content from a URL using the internal Cloudflare Bypasser."""
    sel = selector or network.AAMirrorSelector()
    attempt_url = sel.rewrite(url)
    hostname = urlparse(attempt_url).hostname or ""

    cached_result = _try_with_cached_cookies(attempt_url, hostname)
    if cached_result:
        return cached_result

    try:
        response_html = get(attempt_url, cancel_flag=cancel_flag)
    except BypassCancelledError:
        raise
    except _CDP_OPERATION_ERRORS + _REQUEST_OPERATION_ERRORS:
        _check_cancellation(cancel_flag, "Bypass cancelled")
        new_base, action = sel.next_mirror_or_rotate_dns()
        if action in ("mirror", "dns") and new_base:
            attempt_url = sel.rewrite(url)
            response_html = get(attempt_url, cancel_flag=cancel_flag)
        else:
            raise

    if not response_html.strip():
        msg = "Failed to bypass Cloudflare"
        raise requests.exceptions.RequestException(msg)

    return response_html


def _read_child_request(idle_timeout: float) -> str | None:
    """Read one request line from stdin. None means the parent went quiet."""
    stream = sys.stdin
    try:
        fileno = stream.fileno()
    except AttributeError, OSError, ValueError:
        fileno = None

    if fileno is not None:
        # Safe because the protocol is strictly request/response: the parent holds
        # LOCKED while it waits for the result, so there is never a second line
        # sitting in Python's buffer that select() would not see.
        ready, _, _ = select.select([fileno], [], [], idle_timeout)
        if not ready:
            return None

    return stream.readline()


def _write_child_result(result_path: Path, payload: dict[str, Any]) -> None:
    """Write the result atomically - the parent polls for this file's existence."""
    tmp_path = result_path.with_name(result_path.name + ".part")
    tmp_path.write_text(json.dumps(payload), encoding="utf-8")
    tmp_path.replace(result_path)


def _handle_child_request(request: dict[str, Any]) -> int:
    """Serve one bypass request. Returns the exit code this request would imply."""
    result_path = Path(str(request["result_path"]))
    url = str(request["url"])
    # Only touch app_config when the parent did not send a retry count: the first
    # config access boots the whole settings registry in this process.
    retry = _coerce_positive_int(request.get("retry"), 0)
    if retry <= 0:
        retry = _coerce_positive_int(app_config.MAX_RETRY, 10)

    merge_bypass_method_hints(request.get("method_hints") or {})

    try:
        html = get(url, retry=retry)
        payload = {
            "ok": True,
            "html": html,
            "cookies": _cf_cookies,
            "user_agents": _cf_user_agents,
            "method_hints": get_bypass_method_hints(),
        }
        _write_child_result(result_path, payload)
    except Exception as exc:  # noqa: BLE001 - helper boundary must serialize failures.
        payload = {
            "ok": False,
            "error_type": type(exc).__name__,
            "error": str(exc),
            "traceback": traceback.format_exc(),
        }
        _write_child_result(result_path, payload)
        return 1
    return 0


def _shutdown_child_browser() -> None:
    """Tear the warm browser down and close the child's event loop."""
    if _WARM_BROWSER.get("driver") is not None:
        logger.info("Closing warm Chrome browser before helper exit")
        with suppress(*_CDP_OPERATION_ERRORS):
            _get_child_event_loop().run_until_complete(_shutdown_warm_browser())
    _close_child_event_loop()


def _run_child_process() -> int:
    """CLI entrypoint used by the Docker helper subprocess.

    Serves requests line by line until the parent closes stdin (EOF), goes away, or
    stops sending for _CHILD_IDLE_EXIT_SECONDS. One Chrome is kept warm across those
    requests; the finally block and this process' death are what reap it.
    """
    exit_code = 0
    try:
        while True:
            line = _read_child_request(_CHILD_IDLE_EXIT_SECONDS)
            if line is None:
                logger.info(
                    "Bypass helper idle for %.0fs without a request - exiting",
                    _CHILD_IDLE_EXIT_SECONDS,
                )
                break

            line = line.strip()
            if not line:
                # EOF: the parent closed stdin or died.
                break

            try:
                request = json.loads(line)
            except ValueError as exc:
                logger.exception("Bypass helper received an unreadable request: %s", exc)  # noqa: TRY401
                exit_code = 1
                break

            try:
                exit_code = _handle_child_request(request)
                if exit_code != 0:
                    # A failed request may have left this process with a poisoned
                    # display (the orphan cleanup pkills Xvfb container-wide). Exit so
                    # the parent starts a clean child, exactly like the old one-shot
                    # helper did.
                    logger.info("Bypass helper exiting after a failed request")
                    break
            except (KeyError, OSError, TypeError, ValueError) as exc:
                # Cannot report this back through the result file - exit so the
                # parent notices immediately instead of waiting for the timeout.
                logger.exception("Bypass helper could not serve a request: %s", exc)  # noqa: TRY401
                exit_code = 1
                break
    finally:
        _shutdown_child_browser()
    return exit_code


if __name__ == "__main__":
    raise SystemExit(_run_child_process())
