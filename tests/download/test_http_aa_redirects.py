import requests


class _FakeResponse:
    def __init__(
        self, status_code: int, *, headers: dict | None = None, text: str = "", url: str = ""
    ) -> None:
        self.status_code = status_code
        self.headers = headers or {}
        self.text = text
        self.url = url

    @property
    def is_redirect(self) -> bool:  # requests.Response compatibility
        return self.status_code in (301, 302, 303, 307, 308) and bool(self.headers.get("Location"))

    def raise_for_status(self) -> None:  # requests.Response compatibility
        if self.status_code >= 400:
            raise requests.exceptions.HTTPError(response=self)


class _DummySelector:
    """Minimal AA selector stub for unit testing http.html_get_page()."""

    def __init__(self, bases: list[str]) -> None:
        self._bases = bases
        self._index = 0
        self.current_base = bases[0]
        self.attempts_this_dns = 0

    def rewrite(self, url: str) -> str:
        for base in self._bases:
            if url.startswith(base):
                return url.replace(base, self.current_base, 1)
        return url

    def next_mirror_or_rotate_dns(self, allow_dns: bool = True) -> tuple[str | None, str]:
        self.attempts_this_dns += 1
        self._index = (self._index + 1) % len(self._bases)
        self.current_base = self._bases[self._index]
        return self.current_base, "mirror"


def test_html_get_page_aa_cross_host_redirect_rotates_mirror(monkeypatch):
    import shelfmark.download.http as http

    # Avoid bypasser imports in unit tests.
    monkeypatch.setattr(http, "_is_cf_bypass_enabled", lambda: False)
    monkeypatch.setattr(http, "get_proxies", lambda _url: {})
    monkeypatch.setattr(http.time, "sleep", lambda _s: None)
    monkeypatch.setattr(http.network, "get_aa_base_url", lambda: "https://annas-archive.li")
    monkeypatch.setattr(http.network, "is_aa_auto_mode", lambda: True)

    calls: list[dict] = []

    def fake_get(url: str, **kwargs):
        calls.append({"url": url, "allow_redirects": kwargs.get("allow_redirects")})
        if url.startswith("https://annas-archive.li/"):
            return _FakeResponse(
                302, headers={"Location": "https://annas-archive.pm/search?q=test"}, url=url
            )
        if url.startswith("https://annas-archive.gl/"):
            return _FakeResponse(200, text="OK", url=url)
        raise AssertionError(f"Unexpected URL: {url}")

    monkeypatch.setattr(http.requests, "get", fake_get)

    selector = _DummySelector(["https://annas-archive.li", "https://annas-archive.gl"])
    html = http.html_get_page(
        "https://annas-archive.li/search?q=test",
        selector=selector,
        retry=2,
        allow_bypasser_fallback=False,
    )

    assert html == "OK"
    assert calls[0]["allow_redirects"] is False  # AA redirects handled manually
    assert calls[0]["url"].startswith("https://annas-archive.li/")
    assert calls[1]["url"].startswith(
        "https://annas-archive.gl/"
    )  # rotated away from redirect target


def test_html_get_page_aa_same_host_redirect_is_followed(monkeypatch):
    import shelfmark.download.http as http

    monkeypatch.setattr(http, "_is_cf_bypass_enabled", lambda: False)
    monkeypatch.setattr(http, "get_proxies", lambda _url: {})
    monkeypatch.setattr(http.time, "sleep", lambda _s: None)
    monkeypatch.setattr(http.network, "get_aa_base_url", lambda: "https://annas-archive.li")
    monkeypatch.setattr(http.network, "is_aa_auto_mode", lambda: True)

    calls: list[dict] = []

    def fake_get(url: str, **kwargs):
        calls.append({"url": url, "allow_redirects": kwargs.get("allow_redirects")})
        if url == "https://annas-archive.li/search?q=test":
            return _FakeResponse(302, headers={"Location": "/search?q=test&page=1"}, url=url)
        if url == "https://annas-archive.li/search?q=test&page=1":
            return _FakeResponse(200, text="OK2", url=url)
        raise AssertionError(f"Unexpected URL: {url}")

    monkeypatch.setattr(http.requests, "get", fake_get)

    selector = _DummySelector(["https://annas-archive.li"])
    html = http.html_get_page(
        "https://annas-archive.li/search?q=test",
        selector=selector,
        retry=1,
        allow_bypasser_fallback=False,
    )

    assert html == "OK2"
    assert [c["url"] for c in calls] == [
        "https://annas-archive.li/search?q=test",
        "https://annas-archive.li/search?q=test&page=1",
    ]
    assert all(c["allow_redirects"] is False for c in calls)


def test_html_get_page_locked_aa_does_not_fail_over_on_cross_host_redirect(monkeypatch):
    import shelfmark.download.http as http

    monkeypatch.setattr(http, "_is_cf_bypass_enabled", lambda: False)
    monkeypatch.setattr(http, "get_proxies", lambda _url: {})
    monkeypatch.setattr(http.time, "sleep", lambda _s: None)
    monkeypatch.setattr(http.network, "get_aa_base_url", lambda: "https://annas-archive.li")
    monkeypatch.setattr(http.network, "is_aa_auto_mode", lambda: False)

    calls: list[str] = []

    def fake_get(url: str, **kwargs):
        calls.append(url)
        if url.startswith("https://annas-archive.li/"):
            return _FakeResponse(
                302, headers={"Location": "https://annas-archive.pm/search?q=test"}, url=url
            )
        raise AssertionError(f"Unexpected URL: {url}")

    monkeypatch.setattr(http.requests, "get", fake_get)

    selector = _DummySelector(["https://annas-archive.li", "https://annas-archive.gl"])
    html = http.html_get_page(
        "https://annas-archive.li/search?q=test",
        selector=selector,
        retry=2,
        allow_bypasser_fallback=False,
    )

    assert html == ""
    assert calls == ["https://annas-archive.li/search?q=test"]


class _CookieJarResponse(_FakeResponse):
    """Fake response that also exposes Set-Cookie values like requests does."""

    def __init__(self, status_code: int, *, cookies: dict[str, str], **kwargs) -> None:
        super().__init__(status_code, **kwargs)
        self.cookies = requests.cookies.cookiejar_from_dict(cookies)


def test_html_get_page_carries_rotated_protection_cookies_across_redirects(monkeypatch):
    """DDoS-Guard rotates __ddg8_/__ddg10_ per response; the next hop must send them.

    Replaying the stale snapshot made DDoS-Guard answer 302 forever until
    _MAX_REDIRECTS tripped (TooManyRedirects), which burned retry attempts and
    eventually rotated the mirror -- discarding the cookies a solve had just paid for.
    """
    import shelfmark.download.http as http

    monkeypatch.setattr(http, "_is_cf_bypass_enabled", lambda: False)
    monkeypatch.setattr(http, "get_proxies", lambda _url: {})
    monkeypatch.setattr(http.time, "sleep", lambda _s: None)
    monkeypatch.setattr(http.network, "get_aa_base_url", lambda: "https://annas-archive.gl")
    monkeypatch.setattr(http.network, "is_aa_auto_mode", lambda: True)
    monkeypatch.setattr(http, "_apply_cf_bypass", lambda _url, _headers: {"__ddg2_": "solved"})

    sent_cookies: list[dict] = []

    def fake_get(url: str, **kwargs):
        sent_cookies.append(dict(kwargs.get("cookies") or {}))
        if url == "https://annas-archive.gl/search?q=test":
            return _CookieJarResponse(
                302,
                headers={"Location": "/search?q=test&check=1"},
                url=url,
                cookies={"__ddg8_": "hop1", "__ddg10_": "hop1"},
            )
        return _CookieJarResponse(200, text="OK", url=url, cookies={})

    monkeypatch.setattr(http.requests, "get", fake_get)

    selector = _DummySelector(["https://annas-archive.gl"])
    html = http.html_get_page(
        "https://annas-archive.gl/search?q=test",
        selector=selector,
        retry=1,
        allow_bypasser_fallback=False,
    )

    assert html == "OK"
    assert sent_cookies[0] == {"__ddg2_": "solved"}
    assert sent_cookies[1] == {"__ddg2_": "solved", "__ddg8_": "hop1", "__ddg10_": "hop1"}


def test_html_get_page_retries_canonical_url_not_the_challenge_redirect(monkeypatch):
    """A new attempt starts from the canonical URL, never from "...&check=1"."""
    import shelfmark.download.http as http

    monkeypatch.setattr(http, "_is_cf_bypass_enabled", lambda: False)
    monkeypatch.setattr(http, "get_proxies", lambda _url: {})
    monkeypatch.setattr(http.time, "sleep", lambda _s: None)
    monkeypatch.setattr(http.network, "get_aa_base_url", lambda: "https://annas-archive.gl")
    monkeypatch.setattr(http.network, "is_aa_auto_mode", lambda: True)

    calls: list[str] = []

    def fake_get(url: str, **_kwargs):
        calls.append(url)
        if len(calls) == 1:
            return _FakeResponse(302, headers={"Location": "/search?q=test&check=1"}, url=url)
        if len(calls) == 2:
            # Not retryable, so no mirror rotation happens: only the per-attempt
            # reset can bring the next attempt back to the canonical URL.
            raise requests.exceptions.TooManyRedirects("challenge loop")
        return _FakeResponse(200, text="OK", url=url)

    monkeypatch.setattr(http.requests, "get", fake_get)

    selector = _DummySelector(["https://annas-archive.gl"])
    html = http.html_get_page(
        "https://annas-archive.gl/search?q=test",
        selector=selector,
        retry=2,
        allow_bypasser_fallback=False,
    )

    assert html == "OK"
    assert calls[2] == "https://annas-archive.gl/search?q=test"


def test_html_get_page_writes_rotated_cookies_back_to_the_store(monkeypatch):
    """The store must be refreshed by successful traffic, not only by browser solves.

    The tightened expiry check drops a domain once its stored __ddg8_/__ddg10_ expired
    (~20 min). Without this write-back the stored expiry only ever tracked the last
    solve, so a stream of successful searches - each one rotating the cookies - would
    still end in a 403 and another 20-67s solve.
    """
    import shelfmark.download.http as http

    monkeypatch.setattr(http, "_is_cf_bypass_enabled", lambda: True)
    monkeypatch.setattr(http, "_is_using_external_bypasser", lambda: False)
    monkeypatch.setattr(http, "get_proxies", lambda _url: {})
    monkeypatch.setattr(http.time, "sleep", lambda _s: None)
    monkeypatch.setattr(http.network, "get_aa_base_url", lambda: "https://annas-archive.gl")
    monkeypatch.setattr(http.network, "is_aa_auto_mode", lambda: True)
    monkeypatch.setattr(http, "_apply_cf_bypass", lambda _url, _headers: {"__ddg2_": "solved"})

    refreshed: list[tuple[str, list[str]]] = []

    class _FakeBypasser:
        @staticmethod
        def refresh_cookies_for_domain(url, cookies):
            refreshed.append((url, sorted(c.name for c in cookies)))

    monkeypatch.setattr(http, "_get_internal_bypasser", lambda: _FakeBypasser)

    def fake_get(url: str, **_kwargs):
        if url == "https://annas-archive.gl/search?q=test":
            return _CookieJarResponse(
                302,
                headers={"Location": "/search?q=test&check=1"},
                url=url,
                cookies={"__ddg8_": "hop1"},
            )
        return _CookieJarResponse(200, text="OK", url=url, cookies={"__ddg10_": "hop2"})

    monkeypatch.setattr(http.requests, "get", fake_get)

    selector = _DummySelector(["https://annas-archive.gl"])
    html = http.html_get_page(
        "https://annas-archive.gl/search?q=test",
        selector=selector,
        retry=1,
        allow_bypasser_fallback=False,
    )

    assert html == "OK"
    # Every cookie the server rotated along the chain, written back once on success.
    assert refreshed == [
        ("https://annas-archive.gl/search?q=test&check=1", ["__ddg10_", "__ddg8_"])
    ]


def test_html_get_page_skips_cookie_writeback_with_external_bypasser(monkeypatch):
    """The external bypasser owns no local cookie store; never call into the internal one."""
    import shelfmark.download.http as http

    monkeypatch.setattr(http, "_is_cf_bypass_enabled", lambda: True)
    monkeypatch.setattr(http, "_is_using_external_bypasser", lambda: True)
    monkeypatch.setattr(http, "get_proxies", lambda _url: {})
    monkeypatch.setattr(http.time, "sleep", lambda _s: None)
    monkeypatch.setattr(http.network, "get_aa_base_url", lambda: "https://annas-archive.gl")
    monkeypatch.setattr(http.network, "is_aa_auto_mode", lambda: True)
    monkeypatch.setattr(http, "_apply_cf_bypass", lambda _url, _headers: {})

    def _fail():
        raise AssertionError("internal bypasser must not be touched")

    monkeypatch.setattr(http, "_get_internal_bypasser", _fail)
    monkeypatch.setattr(
        http.requests,
        "get",
        lambda url, **_kwargs: _CookieJarResponse(
            200, text="OK", url=url, cookies={"__ddg8_": "hop"}
        ),
    )

    selector = _DummySelector(["https://annas-archive.gl"])
    assert (
        http.html_get_page(
            "https://annas-archive.gl/search?q=test",
            selector=selector,
            retry=1,
            allow_bypasser_fallback=False,
        )
        == "OK"
    )
