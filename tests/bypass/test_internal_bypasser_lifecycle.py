"""Browser and helper-process lifecycle tests for the internal bypasser.

The suite never starts a real Chrome, so the fakes below carry an explicit
lifecycle protocol instead: every browser start and every close is counted, and
the invariant `started == closed` after teardown is what turns a leaked browser
(the failure mode that once poisoned the whole container) into a red test.
"""

import io
import json
import os
import subprocess

import pytest

import shelfmark.bypass.internal_bypasser as ib
from shelfmark.bypass import BypassCancelledError


class LifecycleLog:
    """Records browser starts/stops so a leak is assertable."""

    def __init__(self) -> None:
        self.events: list[str] = []
        self.started = 0
        self.closed = 0
        self.drivers: list[FakeDriver] = []

    @property
    def live(self) -> int:
        return self.started - self.closed


class FakeConnection:
    def __init__(self, log: LifecycleLog, index: int) -> None:
        self._log = log
        self._index = index

    async def aclose(self) -> None:
        self._log.events.append(f"aclose:{self._index}")


class FakeDriver:
    def __init__(self, log: LifecycleLog, index: int) -> None:
        self._log = log
        self.index = index
        self.connection = FakeConnection(log, index)
        self.targets: list[object] = []
        # A real driver carries the Chrome pid here (cdp_driver/browser.py sets it at
        # start). Our own pid stands in for "that process is alive".
        self._process_pid = os.getpid()
        self.stopped = False

    def stop(self) -> None:
        if self.stopped:
            raise AssertionError(f"driver {self.index} closed twice")
        self.stopped = True
        self._log.closed += 1
        self._log.events.append(f"stop:{self.index}")


@pytest.fixture
def lifecycle(monkeypatch):
    """Fake browser factory plus full reset of the module-level lifecycle state."""
    log = LifecycleLog()

    async def _start_async(*_args, **_kwargs):
        log.started += 1
        driver = FakeDriver(log, log.started)
        log.drivers.append(driver)
        log.events.append(f"start:{driver.index}")
        return driver

    monkeypatch.setattr(ib.cdp_driver, "start_async", _start_async)
    monkeypatch.setattr(ib, "_get_browser_args", lambda: [])
    monkeypatch.setattr(ib, "get_screen_size", lambda: (1280, 800))
    monkeypatch.setattr(ib, "_get_proxy_string", lambda _url: None)
    monkeypatch.setattr(ib, "_BROWSER_SETTLE_SECONDS", 0.0)
    monkeypatch.setattr(ib.app_config, "get", lambda *_a, **_k: False)
    monkeypatch.setattr(ib.env, "DOCKERMODE", False)
    monkeypatch.setattr(ib.logger, "log_resource_usage", lambda *_a, **_k: None)

    ib._WARM_BROWSER.update(driver=None, created_at=0.0, uses=0, proxy=None)
    ib.clear_bypass_method_hints()
    yield log
    ib._WARM_BROWSER.update(driver=None, created_at=0.0, uses=0, proxy=None)
    ib._close_child_event_loop()
    ib.clear_bypass_method_hints()


@pytest.fixture
def child_mode(monkeypatch):
    monkeypatch.setenv(ib._BYPASS_CHILD_ENV, "1")


def _fake_get(results):
    """Build a _get replacement driving one outcome per call."""
    calls: list[int] = []

    async def _get(_url, driver, _cancel_flag=None):
        calls.append(driver.index)
        outcome = results.pop(0) if results else "<html>"
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    _get.calls = calls
    return _get


# --------------------------------------------------------------- warm browser


def test_warm_browser_is_reused_and_closed_on_shutdown(lifecycle, child_mode, monkeypatch):
    monkeypatch.setattr(ib, "_get", _fake_get(["<html>a", "<html>b"]))

    assert ib._run_bypass_in_current_process("https://annas-archive.gl/a", 2) == "<html>a"
    assert ib._run_bypass_in_current_process("https://annas-archive.gl/b", 2) == "<html>b"

    assert lifecycle.started == 1, "second solve must not start a second Chrome"
    assert lifecycle.closed == 0
    assert ib._WARM_BROWSER["uses"] == 2

    ib._shutdown_child_browser()

    assert lifecycle.closed == 1
    assert lifecycle.live == 0
    assert ib._WARM_BROWSER["driver"] is None


def test_browser_is_not_kept_warm_outside_the_helper_child(lifecycle, monkeypatch):
    monkeypatch.delenv(ib._BYPASS_CHILD_ENV, raising=False)
    monkeypatch.setattr(ib, "_get", _fake_get(["<html>a", "<html>b"]))

    ib._run_bypass_in_current_process("https://annas-archive.gl/a", 2)
    ib._run_bypass_in_current_process("https://annas-archive.gl/b", 2)

    assert lifecycle.started == 2
    assert lifecycle.closed == 2
    assert lifecycle.live == 0
    assert ib._WARM_BROWSER["driver"] is None


def test_failed_solve_never_leaves_a_warm_browser(lifecycle, child_mode, monkeypatch):
    monkeypatch.setattr(ib, "_get", _fake_get(["", ""]))

    assert ib._run_bypass_in_current_process("https://annas-archive.gl/a", 2) == ""

    assert lifecycle.started == 1
    assert lifecycle.closed == 1
    assert ib._WARM_BROWSER["driver"] is None


def test_cancellation_closes_the_browser(lifecycle, child_mode, monkeypatch):
    monkeypatch.setattr(ib, "_get", _fake_get([BypassCancelledError("stop")]))

    with pytest.raises(BypassCancelledError):
        ib._run_bypass_in_current_process("https://annas-archive.gl/a", 2)

    assert lifecycle.started == 1
    assert lifecycle.closed == 1
    assert ib._WARM_BROWSER["driver"] is None


def test_broken_warm_browser_falls_back_to_a_fresh_one(lifecycle, child_mode, monkeypatch):
    monkeypatch.setattr(ib, "_get", _fake_get(["<html>a"]))
    assert ib._run_bypass_in_current_process("https://annas-archive.gl/a", 3) == "<html>a"
    assert lifecycle.started == 1

    fake_get = _fake_get([RuntimeError("websocket closed"), "<html>b"])
    monkeypatch.setattr(ib, "_get", fake_get)

    assert ib._run_bypass_in_current_process("https://annas-archive.gl/b", 3) == "<html>b"

    assert lifecycle.started == 2, "a failing warm browser must be replaced by a fresh one"
    assert lifecycle.closed == 1, "the broken warm browser must be closed"
    assert fake_get.calls == [1, 2]

    ib._shutdown_child_browser()
    assert lifecycle.closed == lifecycle.started


def test_driver_reset_error_restarts_and_closes_both_browsers(lifecycle, monkeypatch):
    monkeypatch.delenv(ib._BYPASS_CHILD_ENV, raising=False)
    monkeypatch.setattr(ib, "_get", _fake_get([ib.ProtocolException("boom"), "<html>b"]))

    assert ib._run_bypass_in_current_process("https://annas-archive.gl/a", 3) == "<html>b"

    assert lifecycle.started == 2
    assert lifecycle.closed == 2
    assert lifecycle.live == 0


def test_warm_browser_retires_after_max_uses(lifecycle, child_mode, monkeypatch):
    monkeypatch.setattr(ib, "_WARM_BROWSER_MAX_USES", 2)
    monkeypatch.setattr(ib, "_get", _fake_get(["<a>", "<b>", "<c>"]))

    for path in ("a", "b", "c"):
        ib._run_bypass_in_current_process(f"https://annas-archive.gl/{path}", 2)

    assert lifecycle.started == 2, "the browser must be rebuilt after the use limit"
    assert lifecycle.closed == 1
    ib._shutdown_child_browser()
    assert lifecycle.closed == lifecycle.started


def test_warm_browser_retires_after_max_age(lifecycle, child_mode, monkeypatch):
    monkeypatch.setattr(ib, "_WARM_BROWSER_MAX_AGE_SECONDS", 0.0)
    monkeypatch.setattr(ib, "_get", _fake_get(["<a>", "<b>"]))

    ib._run_bypass_in_current_process("https://annas-archive.gl/a", 2)
    ib._run_bypass_in_current_process("https://annas-archive.gl/b", 2)

    assert lifecycle.started == 2
    assert lifecycle.closed == 1
    ib._shutdown_child_browser()
    assert lifecycle.closed == lifecycle.started


def test_warm_browser_retires_when_chrome_died(lifecycle, child_mode, monkeypatch):
    monkeypatch.setattr(ib, "_get", _fake_get(["<a>", "<b>"]))
    ib._run_bypass_in_current_process("https://annas-archive.gl/a", 2)

    monkeypatch.setattr(ib, "_chrome_process_alive", lambda _driver: False)
    ib._run_bypass_in_current_process("https://annas-archive.gl/b", 2)

    assert lifecycle.started == 2
    assert lifecycle.closed == 1


def test_warm_browser_retires_when_the_proxy_changes(lifecycle, child_mode, monkeypatch):
    monkeypatch.setattr(ib, "_get", _fake_get(["<a>", "<b>"]))
    ib._run_bypass_in_current_process("https://annas-archive.gl/a", 2)

    monkeypatch.setattr(ib, "_get_proxy_string", lambda _url: "http://proxy:8080")
    ib._run_bypass_in_current_process("https://welib.org/b", 2)

    assert lifecycle.started == 2
    assert lifecycle.closed == 1


def test_warm_browser_that_solves_nothing_is_replaced_within_the_same_solve(
    lifecycle, child_mode, monkeypatch
):
    """The common failure is an empty result, not an exception - the fallback must fire.

    _bypass() exhausting its methods returns False and _get() then returns "". Without
    an explicit branch for that, the remaining attempts of the outer loop keep running
    on the very session DDoS-Guard has already judged badly.
    """
    monkeypatch.setattr(ib, "_get", _fake_get(["<html>a"]))
    assert ib._run_bypass_in_current_process("https://annas-archive.gl/a", 3) == "<html>a"
    assert lifecycle.started == 1

    fake_get = _fake_get(["", "<html>b"])
    monkeypatch.setattr(ib, "_get", fake_get)

    assert ib._run_bypass_in_current_process("https://annas-archive.gl/b", 3) == "<html>b"

    assert lifecycle.started == 2, "an empty result on a warm browser must fetch a fresh one"
    assert lifecycle.closed == 1
    assert fake_get.calls == [1, 2], "the retry must not run on the same warm session"

    ib._shutdown_child_browser()
    assert lifecycle.closed == lifecycle.started


def test_fresh_browser_is_not_thrown_away_after_an_empty_result(lifecycle, child_mode, monkeypatch):
    """The fallback is about the *warm* session; a fresh browser keeps its retries."""
    fake_get = _fake_get(["", "<html>b"])
    monkeypatch.setattr(ib, "_get", fake_get)

    assert ib._run_bypass_in_current_process("https://annas-archive.gl/a", 3) == "<html>b"

    assert lifecycle.started == 1
    assert fake_get.calls == [1, 1]


def test_warm_browser_retires_when_chrome_grows_too_big(lifecycle, child_mode, monkeypatch):
    monkeypatch.setattr(ib, "_get", _fake_get(["<a>", "<b>"]))
    monkeypatch.setattr(ib, "_WARM_BROWSER_MAX_RSS_MB", 300.0)
    ib._run_bypass_in_current_process("https://annas-archive.gl/a", 2)

    monkeypatch.setattr(ib, "_chrome_tree_rss_mb", lambda _driver: 512.0)
    ib._run_bypass_in_current_process("https://annas-archive.gl/b", 2)

    assert lifecycle.started == 2, "an oversized Chrome must be retired, not OOM-killed"
    assert lifecycle.closed == 1


def test_warm_browser_is_not_reused_when_the_chrome_pid_is_unknown(
    lifecycle, child_mode, monkeypatch
):
    """No pid, no liveness proof - refusing to reuse only costs one browser start."""
    monkeypatch.setattr(ib, "_get", _fake_get(["<a>", "<b>"]))
    ib._run_bypass_in_current_process("https://annas-archive.gl/a", 2)

    lifecycle.drivers[0]._process_pid = None
    ib._run_bypass_in_current_process("https://annas-archive.gl/b", 2)

    assert lifecycle.started == 2
    assert lifecycle.closed == 1


def test_warm_browser_env_kill_switch_is_readable(monkeypatch):
    """MAX_USES=1 must be reachable from compose ENV, without an image rebuild."""
    monkeypatch.setenv("SHELFMARK_WARM_BROWSER_MAX_USES", "1")
    assert int(ib._env_number("SHELFMARK_WARM_BROWSER_MAX_USES", 12, minimum=1)) == 1

    monkeypatch.setenv("SHELFMARK_WARM_BROWSER_MAX_USES", "nonsense")
    assert int(ib._env_number("SHELFMARK_WARM_BROWSER_MAX_USES", 12, minimum=1)) == 12

    monkeypatch.setenv("SHELFMARK_WARM_BROWSER_MAX_USES", "0")
    assert int(ib._env_number("SHELFMARK_WARM_BROWSER_MAX_USES", 12, minimum=1)) == 12


def test_child_idle_exit_outlives_the_parent_idle_timeout():
    assert ib._CHILD_IDLE_EXIT_SECONDS > ib._HELPER_IDLE_TIMEOUT_SECONDS, (
        "the parent must reap the helper before the child gives up on its own"
    )


def test_browser_start_failure_leaves_no_warm_state(lifecycle, child_mode, monkeypatch):
    async def _fail(*_args, **_kwargs):
        raise Exception("Failed to connect to the browser")

    monkeypatch.setattr(ib.cdp_driver, "start_async", _fail)

    with pytest.raises(RuntimeError, match="Pure CDP browser startup failed"):
        ib._run_bypass_in_current_process("https://annas-archive.gl/a", 1)

    assert ib._WARM_BROWSER["driver"] is None
    assert lifecycle.live == 0


# ---------------------------------------------------------------- child loop


def test_child_serves_several_requests_then_shuts_the_browser_down(
    lifecycle, child_mode, monkeypatch, tmp_path
):
    monkeypatch.setattr(ib, "_get", _fake_get(["<html>a", "<html>b"]))

    first = tmp_path / "one.json"
    second = tmp_path / "two.json"
    lines = "".join(
        json.dumps(
            {"url": f"https://annas-archive.gl/{name}", "retry": 2, "result_path": str(path)}
        )
        + "\n"
        for name, path in (("a", first), ("b", second))
    )
    monkeypatch.setattr(ib.sys, "stdin", io.StringIO(lines))

    assert ib._run_child_process() == 0

    assert json.loads(first.read_text())["html"] == "<html>a"
    assert json.loads(second.read_text())["html"] == "<html>b"
    assert lifecycle.started == 1, "both requests must share one Chrome"
    assert lifecycle.closed == 1, "the browser must be closed when stdin reaches EOF"
    assert ib._WARM_BROWSER["driver"] is None


def test_child_exits_and_closes_the_browser_when_the_parent_goes_quiet(
    lifecycle, child_mode, monkeypatch, tmp_path
):
    monkeypatch.setattr(ib, "_get", _fake_get(["<html>a"]))
    result_path = tmp_path / "one.json"
    request = json.dumps(
        {"url": "https://annas-archive.gl/a", "retry": 2, "result_path": str(result_path)}
    )

    reads = [request + "\n", None]

    def _read(_timeout):
        return reads.pop(0)

    monkeypatch.setattr(ib, "_read_child_request", _read)

    assert ib._run_child_process() == 0
    assert lifecycle.started == 1
    assert lifecycle.closed == 1


def test_child_result_file_is_written_atomically(tmp_path):
    result_path = tmp_path / "result.json"
    ib._write_child_result(result_path, {"ok": True})
    assert json.loads(result_path.read_text()) == {"ok": True}
    assert not (tmp_path / "result.json.part").exists()


def test_child_round_trips_method_hints(lifecycle, child_mode, monkeypatch, tmp_path):
    monkeypatch.setattr(ib, "_get", _fake_get(["<html>a"]))
    result_path = tmp_path / "result.json"
    monkeypatch.setattr(
        ib.sys,
        "stdin",
        io.StringIO(
            json.dumps(
                {
                    "url": "https://annas-archive.gl/a",
                    "retry": 2,
                    "result_path": str(result_path),
                    "method_hints": {"annas-archive.gl|ddos_guard": "_bypass_method_cdp_click"},
                }
            )
        ),
    )

    assert ib._run_child_process() == 0
    result = json.loads(result_path.read_text())
    assert result["method_hints"]["annas-archive.gl|ddos_guard"] == "_bypass_method_cdp_click"


def test_parent_adopts_a_hint_the_child_retired(monkeypatch):
    """The decay in record_bypass_method_result() only runs in the child.

    The parent seeds every child with its full snapshot, so a merge could never remove
    a key: the retired hint would be shipped straight back into the next child, whose
    failure counter starts at zero. That made the hint immortal.
    """
    ib.clear_bypass_method_hints()
    ib.record_bypass_method_result(
        "https://annas-archive.gl/a", "ddos_guard", "_bypass_method_cdp_gui_click", success=True
    )
    assert ib.get_bypass_method_hints() == {
        "annas-archive.gl|ddos_guard": "_bypass_method_cdp_gui_click"
    }

    # The child came back having retired that hint (two failures in a row).
    ib._store_child_bypass_state({"ok": True, "html": "", "method_hints": {}})

    assert ib.get_bypass_method_hints() == {}, "a hint the child retired must stay retired"
    ib.clear_bypass_method_hints()


def test_a_result_without_hints_leaves_the_parents_memory_alone(monkeypatch):
    """An older helper image sends no method_hints - that must not wipe the memory."""
    ib.clear_bypass_method_hints()
    ib.record_bypass_method_result(
        "https://annas-archive.gl/a", "ddos_guard", "_bypass_method_cdp_click", success=True
    )

    ib._store_child_bypass_state({"ok": True, "html": "<html>"})

    assert ib.get_bypass_method_hints() == {
        "annas-archive.gl|ddos_guard": "_bypass_method_cdp_click"
    }
    ib.clear_bypass_method_hints()


# ------------------------------------------------------------- helper process


class StubProc:
    """subprocess.Popen stand-in with an explicit kill protocol."""

    def __init__(self, *, pid=4242, exits_gracefully=True):
        self.pid = pid
        self.stdin = io.StringIO()
        self.returncode = None
        self.events: list[str] = []
        self._exits_gracefully = exits_gracefully
        self.killed = False

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        if self.returncode is None and not self._exits_gracefully and not self.killed:
            raise subprocess.TimeoutExpired("helper", timeout or 0)
        if self.returncode is None:
            self.returncode = 0
        return self.returncode

    def kill(self):
        self.events.append("kill")
        self.killed = True
        self.returncode = -9


# Captured before any monkeypatching replaces the name on the module.
_HELPER_CLS = ib._BypassHelperProcess


def _make_helper(proc, *, pgid=None):
    helper = _HELPER_CLS.__new__(_HELPER_CLS)
    helper.proc = proc
    helper.pgid = proc.pid if pgid is None else pgid
    helper.created_at = ib.time.monotonic()
    helper.last_used_at = helper.created_at
    helper.requests = 0
    return helper


@pytest.fixture(autouse=True)
def _reset_helper_state():
    ib._HELPER_STATE["helper"] = None
    yield
    ib._HELPER_STATE["helper"] = None


def _killpg_recorder(signals, *, group_alive=True):
    """os.killpg stand-in. signal 0 is the liveness probe, everything else is a kill."""

    def _killpg(pgid, sig):
        if sig == 0:
            if not group_alive:
                raise ProcessLookupError(pgid)
            return None
        signals.append((pgid, sig))
        return None

    return _killpg


def test_helper_shutdown_closes_stdin_first(monkeypatch):
    proc = StubProc()
    helper = _make_helper(proc)
    signals: list[tuple[int, int]] = []
    monkeypatch.setattr(ib.os, "killpg", _killpg_recorder(signals, group_alive=False))

    helper.shutdown()

    assert proc.stdin.closed, "graceful stop is closing stdin, which the child sees as EOF"
    assert signals == [], "an empty process group needs no signal"
    assert proc.events == []


def test_helper_shutdown_kills_the_process_group_when_the_child_hangs(monkeypatch):
    proc = StubProc(exits_gracefully=False)
    helper = _make_helper(proc)
    signals: list[tuple[int, int]] = []

    monkeypatch.setattr(ib, "_HELPER_KILL_GRACE_SECONDS", 0.0)
    monkeypatch.setattr(ib.os, "getpgid", lambda _pid: 999)
    monkeypatch.setattr(ib.os, "killpg", _killpg_recorder(signals))

    helper.shutdown()

    assert signals[0] == (proc.pid, ib.signal.SIGTERM)
    assert signals[-1] == (proc.pid, ib.signal.SIGKILL), (
        "a hung helper must be SIGKILLed as a group so chrome/Xvfb go with it"
    )


def test_dead_child_still_gets_its_orphaned_browser_killed(monkeypatch):
    """OOM kill / segfault: the Python child is gone, Chrome and Xvfb are not.

    This is the state that once poisoned the container. shutdown(force=True) must
    still signal the group, even though proc.poll() no longer returns None and
    os.getpgid(pid) would fail on the reaped leader.
    """
    proc = StubProc()
    proc.returncode = -9  # killed without reaching its finally
    helper = _make_helper(proc)
    signals: list[tuple[int, int]] = []

    monkeypatch.setattr(ib, "_HELPER_KILL_GRACE_SECONDS", 0.0)
    monkeypatch.setattr(ib.os, "getpgid", lambda _pid: 999)
    monkeypatch.setattr(ib.os, "killpg", _killpg_recorder(signals))

    helper.shutdown(force=True)

    assert (proc.pid, ib.signal.SIGTERM) in signals, (
        "a reaped helper leader must not stop us from killing its Chrome/Xvfb"
    )
    assert signals[-1] == (proc.pid, ib.signal.SIGKILL)
    assert proc.events == [], "no point in killing the already dead python child"


def test_helper_shutdown_never_kills_our_own_process_group(monkeypatch):
    proc = StubProc(exits_gracefully=False)
    helper = _make_helper(proc, pgid=999)
    signals: list[tuple[int, int]] = []

    # start_new_session did not take effect: the helper shares our group.
    monkeypatch.setattr(ib.os, "getpgid", lambda _pid: 999)
    monkeypatch.setattr(ib.os, "killpg", _killpg_recorder(signals))

    helper.shutdown()

    assert signals == []
    assert proc.events == ["kill"], "fall back to killing just the helper process"


def test_helper_request_times_out_and_is_reported(monkeypatch, tmp_path):
    proc = StubProc()
    helper = _make_helper(proc)
    monkeypatch.setattr(ib, "_BYPASS_SUBPROCESS_TIMEOUT_SECONDS", 0.0)
    monkeypatch.setattr(ib.time, "sleep", lambda _s: None)
    monkeypatch.setattr(ib.tempfile, "gettempdir", lambda: str(tmp_path))

    with pytest.raises(TimeoutError):
        helper.request("https://annas-archive.gl/a", 2)


def test_helper_request_detects_a_dead_child(monkeypatch, tmp_path):
    proc = StubProc()
    proc.returncode = 3
    helper = _make_helper(proc)
    monkeypatch.setattr(ib.tempfile, "gettempdir", lambda: str(tmp_path))

    with pytest.raises(ib._BypassHelperTransportError):
        helper.request("https://annas-archive.gl/a", 2)


def test_get_via_subprocess_retries_on_a_fresh_helper(monkeypatch):
    started: list[object] = []
    shutdowns: list[object] = []

    class FlakyHelper:
        def __init__(self, index):
            self.index = index
            self.pid = 100 + index
            self.requests = 0

        def is_alive(self):
            return True

        def is_expired(self):
            return False

        def age_seconds(self):
            return 0.0

        def request(self, _url, _retry, _cancel_flag=None):
            if self.index == 0:
                msg = "helper died"
                raise ib._BypassHelperTransportError(msg)
            return {"ok": True, "html": "<html>ok", "method_hints": {"x|ddos_guard": "m"}}

        def shutdown(self, *, force=False):
            shutdowns.append(self.index)

    def _new_helper():
        helper = FlakyHelper(len(started))
        started.append(helper)
        return helper

    monkeypatch.setattr(ib, "_BypassHelperProcess", _new_helper)
    monkeypatch.setattr(ib, "_start_helper_reaper", lambda: None)
    ib.clear_bypass_method_hints()

    assert ib._get_via_subprocess("https://annas-archive.gl/a", 2) == "<html>ok"
    assert len(started) == 2, "a broken helper must be replaced, not fail the request"
    assert shutdowns == [0]
    assert ib.get_bypass_method_hints() == {"x|ddos_guard": "m"}
    ib.clear_bypass_method_hints()


def test_reap_idle_helper_stops_an_idle_helper(monkeypatch):
    proc = StubProc()
    helper = _make_helper(proc)
    helper.last_used_at = ib.time.monotonic() - 10_000
    ib._HELPER_STATE["helper"] = helper
    monkeypatch.setattr(ib.os, "killpg", _killpg_recorder([], group_alive=False))

    ib._reap_idle_helper()

    assert ib._HELPER_STATE["helper"] is None
    assert proc.stdin.closed


def test_reap_idle_helper_keeps_hands_off_while_a_solve_runs():
    proc = StubProc()
    helper = _make_helper(proc)
    helper.last_used_at = ib.time.monotonic() - 10_000
    ib._HELPER_STATE["helper"] = helper

    with ib.LOCKED:
        ib._reap_idle_helper()

    assert ib._HELPER_STATE["helper"] is helper, "never reap a helper that is serving a request"


def test_acquire_helper_recycles_an_expired_process(monkeypatch):
    proc = StubProc()
    old = _make_helper(proc)
    old.requests = ib._HELPER_MAX_REQUESTS
    ib._HELPER_STATE["helper"] = old

    created: list[object] = []

    def _new_helper():
        fresh = _make_helper(StubProc(pid=7777))
        created.append(fresh)
        return fresh

    monkeypatch.setattr(ib, "_BypassHelperProcess", _new_helper)
    monkeypatch.setattr(ib, "_start_helper_reaper", lambda: None)
    monkeypatch.setattr(ib.os, "killpg", _killpg_recorder([], group_alive=False))

    helper = ib._acquire_helper()

    assert helper is created[0]
    assert proc.stdin.closed, "the retired helper must be stopped, not leaked"


def test_child_retires_itself_after_a_failed_request(lifecycle, child_mode, monkeypatch, tmp_path):
    """A failure may have poisoned Xvfb, so the child must not serve the next request."""
    first = tmp_path / "one.json"
    second = tmp_path / "two.json"

    def _boom(*_args, **_kwargs):
        msg = "Pure CDP browser startup failed"
        raise RuntimeError(msg)

    monkeypatch.setattr(ib, "get", _boom)
    lines = "".join(
        json.dumps({"url": "https://annas-archive.gl/a", "retry": 2, "result_path": str(path)})
        + "\n"
        for path in (first, second)
    )
    monkeypatch.setattr(ib.sys, "stdin", io.StringIO(lines))

    assert ib._run_child_process() == 1
    assert json.loads(first.read_text())["ok"] is False
    assert not second.exists(), "the child must stop instead of serving on a poisoned state"


def test_helper_child_does_not_repeat_the_parents_cookie_shortcut(child_mode, monkeypatch):
    """The parent tried the same cookies moments ago - a second round trip is pure cost."""
    tried: list[str] = []
    monkeypatch.setattr(
        ib, "_try_with_cached_cookies", lambda url, _host: tried.append(url) or "<cached>"
    )
    monkeypatch.setattr(ib, "_run_bypass_in_current_process", lambda *_a, **_k: "<solved>")

    assert ib.get("https://annas-archive.gl/a", retry=1) == "<solved>"
    assert tried == []


def test_parent_still_uses_the_cookie_shortcut(monkeypatch):
    monkeypatch.delenv(ib._BYPASS_CHILD_ENV, raising=False)
    monkeypatch.setattr(ib, "_try_with_cached_cookies", lambda _url, _host: "<cached>")

    assert ib.get("https://annas-archive.gl/a", retry=1) == "<cached>"


def test_a_failed_helper_result_reaps_the_helper(monkeypatch):
    """The child exits after a failure - the parent must not keep a dead one around."""
    shutdowns: list[bool] = []

    class FailingHelper:
        pid = 5150
        requests = 0

        def is_alive(self):
            return True

        def is_expired(self):
            return False

        def age_seconds(self):
            return 0.0

        def request(self, _url, _retry, _cancel_flag=None):
            return {"ok": False, "error_type": "RuntimeError", "error": "startup failed"}

        def shutdown(self, *, force=False):
            shutdowns.append(force)

    monkeypatch.setattr(ib, "_BypassHelperProcess", FailingHelper)
    monkeypatch.setattr(ib, "_start_helper_reaper", lambda: None)

    with pytest.raises(RuntimeError, match="startup failed"):
        ib._get_via_subprocess("https://annas-archive.gl/a", 2)

    assert shutdowns == [False]
    assert ib._HELPER_STATE["helper"] is None
