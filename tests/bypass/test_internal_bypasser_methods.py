"""Adaptive bypass-method selection.

Anna's Archive sits behind DDoS-Guard, where SeleniumBase's solve_captcha() can never
succeed; welib.org and z-lib.fm sit behind real Cloudflare, where cdp_solve is the
method that gets through. So the order must depend on the detected challenge and on
what actually worked for that host - never on a static reshuffle of BYPASS_METHODS.

Note what "gets through" means on the Cloudflare path, because it drives the tests
below: on the classic "Just a moment" interstitial there is no widget to click, and
SeleniumBase's solve_captcha() returns a hard False after its ~15 turnstile selectors
all miss (undetected/cdp_driver/tab.py). What actually solves that page is the 3-5s
wait afterwards, during which the CF JS finishes. Skipping the wait on a False return
would therefore break exactly the host where cdp_solve is supposed to work.
"""

import asyncio

import pytest

import shelfmark.bypass.internal_bypasser as ib

AA_URL = "https://annas-archive.gl/md5/abc"
CF_URL = "https://welib.org/book/1"


@pytest.fixture(autouse=True)
def _clean_hints():
    ib.clear_bypass_method_hints()
    yield
    ib.clear_bypass_method_hints()


def _names(methods):
    return [m.__name__ for m in methods]


def test_ddos_guard_pushes_cdp_solve_to_the_back():
    ordered = ib._ordered_bypass_methods("ddos_guard", None)

    assert ordered[-1] is ib._bypass_method_cdp_solve
    assert set(ordered) == set(ib.BYPASS_METHODS), "no method may be dropped"


def test_cloudflare_order_is_untouched():
    assert ib._ordered_bypass_methods("cloudflare", None) == list(ib.BYPASS_METHODS)
    assert ib._ordered_bypass_methods("none", None) == list(ib.BYPASS_METHODS)


def test_a_remembered_method_goes_first():
    ordered = ib._ordered_bypass_methods("cloudflare", "_bypass_method_cdp_click")

    assert ordered[0] is ib._bypass_method_cdp_click
    assert set(ordered) == set(ib.BYPASS_METHODS)


def test_an_unknown_hint_changes_nothing():
    assert ib._ordered_bypass_methods("cloudflare", "_gone_in_a_refactor") == list(
        ib.BYPASS_METHODS
    )


def test_hints_are_scoped_to_host_and_challenge():
    ib.record_bypass_method_result(
        AA_URL, "ddos_guard", "_bypass_method_cdp_gui_click", success=True
    )

    assert ib.get_bypass_method_hint(AA_URL, "ddos_guard") == "_bypass_method_cdp_gui_click"
    assert ib.get_bypass_method_hint(AA_URL, "cloudflare") is None
    assert ib.get_bypass_method_hint(CF_URL, "ddos_guard") is None


def test_a_hint_survives_one_failure_and_dies_on_the_second():
    ib.record_bypass_method_result(AA_URL, "ddos_guard", "_bypass_method_cdp_click", success=True)

    ib.record_bypass_method_result(AA_URL, "ddos_guard", "_bypass_method_cdp_click", success=False)
    assert ib.get_bypass_method_hint(AA_URL, "ddos_guard") == "_bypass_method_cdp_click"

    ib.record_bypass_method_result(AA_URL, "ddos_guard", "_bypass_method_cdp_click", success=False)
    assert ib.get_bypass_method_hint(AA_URL, "ddos_guard") is None, (
        "a protection change must not freeze us on a dead method"
    )


def test_hints_are_not_recorded_without_a_usable_url():
    ib.record_bypass_method_result("", "ddos_guard", "_bypass_method_cdp_click", success=True)
    ib.record_bypass_method_result(AA_URL, "none", "_bypass_method_cdp_click", success=True)

    assert ib.get_bypass_method_hints() == {}


def _install_fake_methods(monkeypatch, calls, winner=None):
    def _make(name):
        async def _method(_page):
            calls.append(name)
            return name == winner

        _method.__name__ = name
        return _method

    methods = [_make(f"f{i}") for i in range(3)]
    monkeypatch.setattr(ib, "BYPASS_METHODS", methods)
    return methods


@pytest.fixture
def _bypass_env(monkeypatch):
    async def _never_bypassed(*_a, **_k):
        return False

    async def _ddos_guard(*_a, **_k):
        return "ddos_guard"

    async def _no_sleep(_seconds):
        return None

    monkeypatch.setattr(ib, "_is_bypassed", _never_bypassed)
    monkeypatch.setattr(ib, "_detect_challenge_type", _ddos_guard)
    monkeypatch.setattr(ib.asyncio, "sleep", _no_sleep)
    monkeypatch.setattr(ib._RNG, "uniform", lambda _a, _b: 0)


def test_bypass_remembers_the_method_that_solved_the_challenge(monkeypatch, _bypass_env):
    calls: list[str] = []
    _install_fake_methods(monkeypatch, calls, winner="f1")

    assert asyncio.run(ib._bypass(object(), max_retries=5, url=AA_URL)) is True
    assert calls == ["f0", "f1"]
    assert ib.get_bypass_method_hint(AA_URL, "ddos_guard") == "f1"

    calls.clear()
    assert asyncio.run(ib._bypass(object(), max_retries=5, url=AA_URL)) is True
    assert calls == ["f1"], "the remembered method must be tried first on the next solve"


def test_bypass_still_walks_every_method_when_the_hint_fails(monkeypatch, _bypass_env):
    calls: list[str] = []
    _install_fake_methods(monkeypatch, calls, winner=None)
    ib.record_bypass_method_result(AA_URL, "ddos_guard", "f2", success=True)

    assert asyncio.run(ib._bypass(object(), max_retries=3, url=AA_URL)) is False
    assert sorted(calls) == ["f0", "f1", "f2"]
    assert calls[0] == "f2"


def test_backoff_between_methods_keeps_escalating(monkeypatch, _bypass_env):
    """Regression guard: the escalating backoff must NOT be shortened.

    An earlier revision cut the pause between two different methods to ~1-2s, arguing
    that only a reload hits the origin. It does not hold: cdp_click, cdp_gui_click and
    cdp_solve all click the challenge widget, and every one of those clicks is a
    verification request to DDoS-Guard - on the only path to Anna's Archive, from a
    browser that now keeps one fingerprint across solves.
    """
    calls: list[str] = []
    _install_fake_methods(monkeypatch, calls, winner=None)

    waits: list[float] = []
    monkeypatch.setattr(ib._RNG, "uniform", lambda _a, b: b)

    async def _record_sleep(seconds):
        waits.append(seconds)

    monkeypatch.setattr(ib.asyncio, "sleep", _record_sleep)

    asyncio.run(ib._bypass(object(), max_retries=3, url=AA_URL))

    # uniform(2, 4) * try_count, capped at 12: 4s before the second method, 8s before
    # the third. The wait is served as whole seconds plus a remainder.
    assert sum(waits) == pytest.approx(12.0), "the 4s + 8s backoff must survive"


class _FakePage:
    def __init__(self, solve_result):
        self._solve_result = solve_result
        self.sleeps: list[float] = []

    async def solve_captcha(self):
        return self._solve_result

    async def is_element_visible(self, _selector):
        return False


def _patch_page_helpers(monkeypatch, sleeps):
    async def _record_sleep(seconds):
        sleeps.append(seconds)

    async def _never_bypassed(*_a, **_k):
        return False

    monkeypatch.setattr(ib.asyncio, "sleep", _record_sleep)
    monkeypatch.setattr(ib, "_is_bypassed", _never_bypassed)
    monkeypatch.setattr(ib._RNG, "uniform", lambda _a, b: b)


def test_cdp_solve_skips_the_settle_wait_only_on_ddos_guard(monkeypatch):
    """solve_captcha() returns a hard False on DDoS-Guard - do not sit out 3-5s for it."""
    sleeps: list[float] = []
    _patch_page_helpers(monkeypatch, sleeps)

    result = ib._bypass_method_cdp_solve(_FakePage(False), challenge_type="ddos_guard")
    assert asyncio.run(result) is False
    assert sleeps == []


def test_cdp_solve_keeps_waiting_on_a_cloudflare_page(monkeypatch):
    """tab.py returns False for a CF challenge with no clickable widget, too.

    That is the "Just a moment" interstitial, where the wait IS the mechanism. Keying
    the shortcut off the return value alone would kill cdp_solve on welib/z-lib.
    """
    sleeps: list[float] = []
    _patch_page_helpers(monkeypatch, sleeps)

    result = ib._bypass_method_cdp_solve(_FakePage(False), challenge_type="cloudflare")
    assert asyncio.run(result) is False
    assert sleeps == [5], "the CF interstitial needs the settle wait to resolve itself"


def test_cdp_solve_waits_when_the_challenge_type_is_unknown(monkeypatch):
    """No detected type (a direct call, an unknown protection) means: stay conservative."""
    sleeps: list[float] = []
    _patch_page_helpers(monkeypatch, sleeps)

    assert asyncio.run(ib._bypass_method_cdp_solve(_FakePage(False))) is False
    assert sleeps == [5]


def test_cdp_solve_still_waits_when_the_outcome_is_unknown(monkeypatch):
    """None comes from the reCAPTCHA/Incapsula paths - there the wait is real work."""
    sleeps: list[float] = []
    _patch_page_helpers(monkeypatch, sleeps)

    result = ib._bypass_method_cdp_solve(_FakePage(None), challenge_type="ddos_guard")
    assert asyncio.run(result) is False
    assert sleeps == [5]


def test_gui_click_skips_its_solve_captcha_preamble_only_on_ddos_guard(monkeypatch):
    sleeps: list[float] = []
    _patch_page_helpers(monkeypatch, sleeps)

    result = ib._bypass_method_cdp_gui_click(_FakePage(False), challenge_type="ddos_guard")
    assert asyncio.run(result) is False
    assert sleeps == []


def test_gui_click_keeps_its_preamble_wait_on_a_cloudflare_page(monkeypatch):
    sleeps: list[float] = []
    _patch_page_helpers(monkeypatch, sleeps)

    result = ib._bypass_method_cdp_gui_click(_FakePage(False), challenge_type="cloudflare")
    assert asyncio.run(result) is False
    assert sleeps == [5]


def test_bypass_hands_the_challenge_type_to_the_methods_that_take_one(monkeypatch):
    """The shortcut is only safe because the method learns what it is looking at."""
    seen: list[str] = []

    async def _method(_page, *, challenge_type=""):
        seen.append(challenge_type)
        return False

    _method.__name__ = "typed"
    _method.wants_challenge_type = True

    async def _plain(_page):
        seen.append("<plain>")
        return False

    _plain.__name__ = "plain"

    async def _never_bypassed(*_a, **_k):
        return False

    async def _ddos_guard(*_a, **_k):
        return "ddos_guard"

    async def _no_sleep(_seconds):
        return None

    monkeypatch.setattr(ib, "BYPASS_METHODS", [_method, _plain])
    monkeypatch.setattr(ib, "_is_bypassed", _never_bypassed)
    monkeypatch.setattr(ib, "_detect_challenge_type", _ddos_guard)
    monkeypatch.setattr(ib.asyncio, "sleep", _no_sleep)
    monkeypatch.setattr(ib._RNG, "uniform", lambda _a, _b: 0)

    asyncio.run(ib._bypass(object(), max_retries=2, url=AA_URL))

    assert seen == ["ddos_guard", "<plain>"], "a method without the kwarg must keep working"
