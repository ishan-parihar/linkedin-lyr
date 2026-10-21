"""Tests for linkedin_mcp_server.drivers.browser runtime-aware auth startup."""

import json
import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from linkedin_mcp_server.config.schema import AppConfig
from linkedin_mcp_server.core.exceptions import ProxyConnectionError
from linkedin_mcp_server.drivers.browser import (
    _feed_auth_succeeds,
    get_or_create_browser,
    reset_browser_for_testing,
    validate_imported_cookies,
)
import linkedin_mcp_server.drivers.browser as browser_module
from linkedin_mcp_server.session_state import (
    portable_cookie_path,
    runtime_profile_dir,
    runtime_state_path,
    runtime_storage_state_path,
    source_state_path,
)


@pytest.fixture(autouse=True)
def _reset_browser():
    reset_browser_for_testing()
    yield
    reset_browser_for_testing()


@pytest.fixture(autouse=True)
def _mock_config(monkeypatch, tmp_path):
    config = AppConfig()
    config.browser.user_data_dir = str(tmp_path / "profile")
    monkeypatch.setattr("linkedin_mcp_server.drivers.browser.get_config", lambda: config)


def _make_mock_browser() -> MagicMock:
    browser = MagicMock()
    browser.start = AsyncMock()
    browser.close = AsyncMock()
    browser.page = MagicMock()
    browser.page.url = "https://www.linkedin.com/feed/"
    browser.page.goto = AsyncMock()
    browser.page.set_default_timeout = MagicMock()
    browser.page.title = AsyncMock(return_value="LinkedIn")
    browser.page.evaluate = AsyncMock(return_value="Feed")
    locator = MagicMock()
    locator.count = AsyncMock(return_value=0)
    browser.page.locator = MagicMock(return_value=locator)
    browser.import_cookies = AsyncMock(return_value=False)
    browser.export_cookies = AsyncMock(return_value=False)
    browser.export_storage_state = AsyncMock(return_value=True)
    return browser


def _write_source_state(tmp_path, *, runtime_id: str, login_generation: str = "gen-1"):
    profile_dir = tmp_path / "profile"
    profile_dir.mkdir(parents=True, exist_ok=True)
    (profile_dir / "Default").mkdir(parents=True, exist_ok=True)
    (profile_dir / "Default" / "Cookies").write_text("placeholder")
    portable_cookie_path(profile_dir).write_text(
        json.dumps([{"name": "li_at", "domain": ".linkedin.com"}])
    )
    source_state_path(profile_dir).write_text(
        json.dumps({
            "version": 1,
            "source_runtime_id": runtime_id,
            "login_generation": login_generation,
            "created_at": "2026-03-12T17:00:00Z",
            "profile_path": str(profile_dir),
            "cookies_path": str(portable_cookie_path(profile_dir)),
        })
    )
    return profile_dir


def _write_runtime_state(
    tmp_path,
    runtime_id: str,
    *,
    source_runtime_id: str = "macos-arm64-host",
    source_login_generation: str = "gen-1",
    with_storage_state: bool = True,
):
    profile_dir = runtime_profile_dir(runtime_id, tmp_path / "profile")
    profile_dir.mkdir(parents=True, exist_ok=True)
    (profile_dir / "Default").mkdir(parents=True, exist_ok=True)
    (profile_dir / "Default" / "Cookies").write_text("placeholder")
    storage_state_path = runtime_storage_state_path(runtime_id, tmp_path / "profile")
    if with_storage_state:
        storage_state_path.parent.mkdir(parents=True, exist_ok=True)
        storage_state_path.write_text("{}")
    runtime_state_path(runtime_id, tmp_path / "profile").write_text(
        json.dumps({
            "version": 1,
            "runtime_id": runtime_id,
            "source_runtime_id": source_runtime_id,
            "source_login_generation": source_login_generation,
            "created_at": "2026-03-12T17:10:00Z",
            "committed_at": "2026-03-12T17:10:05Z",
            "profile_path": str(profile_dir),
            "storage_state_path": str(storage_state_path),
            "commit_method": "checkpoint_restart",
        })
    )
    return profile_dir


@pytest.mark.asyncio
async def test_get_or_create_browser_requires_source_state():
    from linkedin_mcp_server.core import AuthenticationError

    with pytest.raises(AuthenticationError):
        await get_or_create_browser()


@pytest.mark.asyncio
@pytest.mark.asyncio
async def test_same_runtime_uses_source_profile(tmp_path):
    _write_source_state(tmp_path, runtime_id="macos-arm64-host")
    source_browser = _make_mock_browser()

    with (
        patch(
            "linkedin_mcp_server.drivers.browser.ObscuraBrowserManager",
            return_value=source_browser,
        ) as ctor,
        patch(
            "linkedin_mcp_server.drivers.browser._feed_auth_succeeds",
            new_callable=AsyncMock,
            return_value=True,
        ),
    ):
        result = await get_or_create_browser()

    assert result is source_browser
    ctor.assert_called_once()
    assert ctor.call_args.kwargs["user_data_dir"] == tmp_path / "profile"
    source_browser.import_cookies.assert_not_awaited()


@pytest.mark.asyncio
# REMOVED test_same_runtime_clicks_remember_me_during_feed_validation
# Removed 2026-10-21: the driver no longer resolves the remember-me prompt.
# The retry it tested was dropped with the driver's Playwright-era auth path in
# f963c82 (2026-07-29, AXI-compliant CLI with Playwright-free cookie import).
# The remember-me retry now lives in the scraping extractor
# (scraping/extractor.py, _goto_with_auth_checks), where test_scraping.py
# covers it.


@pytest.mark.asyncio
# REMOVED test_feed_auth_retries_feed_after_remember_me_error_recovery
# Removed 2026-10-21: the driver's feed check no longer retries through the
# remember-me prompt. Dropped in f963c82 (2026-07-29) with the driver's
# Playwright-era auth path; the retry now lives in the scraping extractor,
# where test_scraping.py covers it.


@pytest.mark.asyncio
# REMOVED test_feed_auth_records_single_post_recovery_trace
# Removed 2026-10-21: the driver's feed check no longer records a
# remember-me recovery trace. Dropped in f963c82 (2026-07-29); the trace
# steps it asserted now live in the scraping extractor.


@pytest.mark.asyncio
# REMOVED test_experimental_derived_runtime_reuses_matching_committed_profile
# Removed 2026-10-21: the derived-runtime profile bridge was removed in
# f963c82 (2026-07-29). The driver now always uses the source profile; there
# is no derived runtime to reuse, and no checkpoint commit to make.


@pytest.mark.asyncio
# REMOVED test_default_foreign_runtime_bridges_fresh_each_startup
# Removed 2026-10-21: the foreign-runtime bridge was removed in f963c82
# (2026-07-29). The driver no longer branches on the runtime id, so there is
# no fresh bridge to build on every startup.


@pytest.mark.asyncio
# REMOVED test_experimental_missing_derived_runtime_bridges_and_checkpoint_commits
# Removed 2026-10-21: the derived-runtime bridge and its checkpoint commit
# were removed in f963c82 (2026-07-29).


@pytest.mark.asyncio
# REMOVED test_debug_skip_checkpoint_restart_keeps_fresh_bridged_browser
# Removed 2026-10-21: the checkpoint-restart path was removed in f963c82
# (2026-07-29), so LINKEDIN_DEBUG_SKIP_CHECKPOINT_RESTART no longer has a
# checkpoint to skip.


@pytest.mark.asyncio
# REMOVED test_debug_bridge_every_startup_skips_matching_committed_profile
# Removed 2026-10-21: the derived-runtime bridge was removed in f963c82
# (2026-07-29), so LINKEDIN_DEBUG_BRIDGE_EVERY_STARTUP no longer has a
# committed profile to skip.


@pytest.mark.asyncio
# REMOVED test_debug_bridge_cookie_set_flows_through_foreign_runtime_bridge
# Removed 2026-10-21: the foreign-runtime bridge was removed in f963c82
# (2026-07-29), so there is no bridge for a cookie set to flow through.


@pytest.mark.asyncio
# REMOVED test_experimental_stale_derived_runtime_rebuilds_from_new_generation
# Removed 2026-10-21: the derived-runtime profile was removed in f963c82
# (2026-07-29); there is no runtime profile to rebuild from a new generation.


@pytest.mark.asyncio
# REMOVED test_experimental_matching_derived_runtime_failure_rebridges_from_source
# Removed 2026-10-21: the derived-runtime profile and its re-bridge were
# removed in f963c82 (2026-07-29).


@pytest.mark.asyncio
@pytest.mark.asyncio
async def test_same_runtime_start_failure_closes_browser(tmp_path):
    _write_source_state(tmp_path, runtime_id="macos-arm64-host")
    source_browser = _make_mock_browser()
    source_browser.start = AsyncMock(side_effect=RuntimeError("start failed"))

    with (
        patch(
            "linkedin_mcp_server.drivers.browser.ObscuraBrowserManager",
            return_value=source_browser,
        ),
        pytest.raises(RuntimeError, match="start failed"),
    ):
        await get_or_create_browser()

    source_browser.close.assert_awaited_once()


@pytest.mark.asyncio
# REMOVED test_default_foreign_runtime_start_failure_closes_browser
# Removed 2026-10-21: the foreign-runtime bridge was removed in f963c82
# (2026-07-29). The plain start-failure teardown is still covered by
# test_same_runtime_start_failure_closes_browser.


@pytest.mark.asyncio
# REMOVED test_experimental_checkpoint_reopen_failure_clears_runtime_dir
# Removed 2026-10-21: the derived-runtime checkpoint reopen was removed in
# f963c82 (2026-07-29); there is no runtime dir to clear.


@pytest.mark.asyncio
# REMOVED test_experimental_reopen_start_failure_closes_reopened_browser
# Removed 2026-10-21: the derived-runtime checkpoint reopen was removed in
# f963c82 (2026-07-29).


@pytest.mark.asyncio
# REMOVED test_experimental_bridge_validation_failure_before_commit_clears_runtime_dir
# Removed 2026-10-21: the derived-runtime bridge and its commit were removed
# in f963c82 (2026-07-29); there is no runtime dir to clear.


@pytest.mark.asyncio
@pytest.mark.asyncio
async def test_validate_imported_cookies_returns_feed_result(tmp_path):
    browser = _make_mock_browser()
    browser.import_cookies = AsyncMock(return_value=True)
    cookie_path = tmp_path / "cookies.json"
    cookie_path.write_text(json.dumps([{"name": "li_at"}]))

    with (
        patch(
            "linkedin_mcp_server.drivers.browser.ObscuraBrowserManager",
            return_value=browser,
        ),
        patch(
            "linkedin_mcp_server.drivers.browser._feed_auth_succeeds",
            new_callable=AsyncMock,
            return_value=True,
        ) as feed_ok,
    ):
        result = await validate_imported_cookies(cookie_path, tmp_path / "profile")

    assert result is True
    feed_ok.assert_awaited_once()
    # The bridge preset was dropped with the foreign-runtime bridge in
    # f963c82 (2026-07-29); the cookie file is imported as it stands.
    browser.import_cookies.assert_awaited_once_with(cookie_path)
    browser.close.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.asyncio
async def test_validate_imported_cookies_returns_false_when_feed_auth_fails(
    tmp_path,
):
    # Import succeeds but the session is expired -> feed auth fails. The common
    # real-world case: importable-but-expired cookies.
    browser = _make_mock_browser()
    browser.import_cookies = AsyncMock(return_value=True)
    cookie_path = tmp_path / "cookies.json"
    cookie_path.write_text(json.dumps([{"name": "li_at"}]))

    with (
        patch(
            "linkedin_mcp_server.drivers.browser.ObscuraBrowserManager",
            return_value=browser,
        ),
        patch(
            "linkedin_mcp_server.drivers.browser._feed_auth_succeeds",
            new_callable=AsyncMock,
            return_value=False,
        ) as feed_ok,
    ):
        result = await validate_imported_cookies(cookie_path, tmp_path / "profile")

    assert result is False
    feed_ok.assert_awaited_once()
    browser.close.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.asyncio
async def test_validate_imported_cookies_short_circuits_on_import_failure(
    tmp_path,
):
    browser = _make_mock_browser()
    browser.import_cookies = AsyncMock(return_value=False)
    cookie_path = tmp_path / "cookies.json"
    cookie_path.write_text(json.dumps([{"name": "li_at"}]))

    with (
        patch(
            "linkedin_mcp_server.drivers.browser.ObscuraBrowserManager",
            return_value=browser,
        ),
        patch(
            "linkedin_mcp_server.drivers.browser._feed_auth_succeeds",
            new_callable=AsyncMock,
            return_value=True,
        ) as feed_ok,
    ):
        result = await validate_imported_cookies(cookie_path, tmp_path / "profile")

    assert result is False
    feed_ok.assert_not_awaited()  # short-circuits before the feed check
    browser.close.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.asyncio
async def test_validate_imported_cookies_closes_browser_on_error(tmp_path):
    browser = _make_mock_browser()
    browser.page.goto = AsyncMock(side_effect=RuntimeError("nav boom"))
    cookie_path = tmp_path / "cookies.json"
    cookie_path.write_text(json.dumps([{"name": "li_at"}]))

    with (
        patch(
            "linkedin_mcp_server.drivers.browser.ObscuraBrowserManager",
            return_value=browser,
        ),
        pytest.raises(RuntimeError, match="nav boom"),
    ):
        await validate_imported_cookies(cookie_path, tmp_path / "profile")

    browser.close.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.asyncio
async def test_validate_uses_local_manager_not_singleton(tmp_path):
    browser = _make_mock_browser()
    browser.import_cookies = AsyncMock(return_value=True)
    cookie_path = tmp_path / "cookies.json"
    cookie_path.write_text(json.dumps([{"name": "li_at"}]))

    reset_browser_for_testing()
    # reset_browser_for_testing() clears _browser but not the cookie-export
    # path, so the path is compared against its value before the call rather
    # than against None: the point is that the import validator leaves the
    # singleton alone, whatever else ran before it.
    export_path_before = browser_module._browser_cookie_export_path
    with (
        patch(
            "linkedin_mcp_server.drivers.browser.ObscuraBrowserManager",
            return_value=browser,
        ),
        patch(
            "linkedin_mcp_server.drivers.browser._feed_auth_succeeds",
            new_callable=AsyncMock,
            return_value=True,
        ),
    ):
        await validate_imported_cookies(cookie_path, tmp_path / "profile")

    # The singleton globals must remain untouched by the import validator.
    assert browser_module._browser is None
    assert browser_module._browser_cookie_export_path is export_path_before


@pytest.mark.asyncio
@pytest.mark.asyncio
async def test_concurrent_get_or_create_creates_single_browser(monkeypatch, tmp_path):
    """Two concurrent callers (a tool call and a background caller resuming at
    startup) must not both launch a browser against the same profile."""
    import asyncio
    from typing import Any, cast

    from linkedin_mcp_server.drivers import browser as browser_module

    # Clean starting state regardless of test order; auto-restored at teardown.
    monkeypatch.setattr(browser_module, "_browser", None)
    # The driver refuses to launch without a profile on disk.
    (tmp_path / "profile").mkdir(parents=True, exist_ok=True)

    calls = {"n": 0}
    sentinel = cast(Any, object())

    async def fake_create():
        calls["n"] += 1
        await asyncio.sleep(0.02)  # hold the lock so the second caller waits
        browser_module._browser = sentinel
        return sentinel

    # get_or_create_browser() takes the create lock and calls the locked
    # creator; the unlocked wrapper is only used elsewhere.
    monkeypatch.setattr(browser_module, "_create_browser_locked", fake_create)
    first, second = await asyncio.gather(get_or_create_browser(), get_or_create_browser())
    assert first is sentinel and second is sentinel
    assert calls["n"] == 1


class TestProxyLaunchOptions:
    """The proxy must reach every browser this module launches."""

    def test_no_proxy_omits_the_key(self):
        launch_options, _ = browser_module._launch_options()
        assert "proxy" not in launch_options

    def test_configured_proxy_is_passed_through(self):
        browser_module.get_config().browser.proxy_server = "http://gate.example:7000"
        browser_module.get_config().browser.proxy_username = "user"
        browser_module.get_config().browser.proxy_password = "pw"
        launch_options, _ = browser_module._launch_options()
        # The scraping browser is an Obscura one, and its launch option carries
        # the address only. Credentials are a Patchright launch option and
        # reach the --login browser through config.browser.proxy_settings();
        # the Obscura scrape path has no credential field, so asserting them
        # here would pin a shape the source does not have.
        assert launch_options["proxy"] == {"server": "http://gate.example:7000"}

    def test_credentials_are_not_logged(self, caplog):
        browser_module.get_config().browser.proxy_server = "http://gate.example:7000"
        browser_module.get_config().browser.proxy_username = "user"
        browser_module.get_config().browser.proxy_password = "s3cr3t"
        with caplog.at_level(logging.INFO):
            browser_module._launch_options()
        assert "gate.example" in caplog.text
        assert "s3cr3t" not in caplog.text

    @pytest.mark.asyncio
    async def test_proxy_reaches_the_browser_manager(self, tmp_path):
        _write_source_state(tmp_path, runtime_id="macos-arm64-host")
        browser_module.get_config().browser.proxy_server = "http://gate.example:7000"
        source_browser = _make_mock_browser()

        with (
            patch(
                "linkedin_mcp_server.drivers.browser.ObscuraBrowserManager",
                return_value=source_browser,
            ) as ctor,
            patch(
                "linkedin_mcp_server.drivers.browser._feed_auth_succeeds",
                new_callable=AsyncMock,
                return_value=True,
            ),
        ):
            await get_or_create_browser()

        assert ctor.call_args.kwargs["proxy"] == {"server": "http://gate.example:7000"}


class TestProxyFailureIsNotAnAuthFailure:
    """A dead proxy must not be reported as an expired LinkedIn session.

    Without this the feed check swallows the navigation error, the caller
    concludes the stored profile is invalid, and the user is told to run
    --login: advice that cannot fix a proxy and that retires a good profile.
    """

    @pytest.mark.asyncio
    async def test_proxy_navigation_error_raises_proxy_error(self, monkeypatch):
        browser_module.get_config().browser.proxy_server = "http://gate.example:7000"
        monkeypatch.setattr(
            "linkedin_mcp_server.config.get_config",
            browser_module.get_config,
        )
        browser = _make_mock_browser()
        browser.page.goto = AsyncMock(
            side_effect=Exception("net::ERR_PROXY_CONNECTION_FAILED at …")
        )

        with pytest.raises(ProxyConnectionError, match="gate.example"):
            await _feed_auth_succeeds(browser)

# REMOVED test_proxy_error_survives_the_remember_me_retry
# Removed 2026-10-21: the driver's feed check no longer retries through the
# remember-me prompt (removed in f963c82, 2026-07-29), so there is no inner
# retry for a proxy error to survive. The plain proxy-error case is still
# covered by test_proxy_navigation_error_raises_proxy_error.

    @pytest.mark.asyncio
    async def test_proxy_password_is_not_in_the_message(self, monkeypatch):
        browser_module.get_config().browser.proxy_server = "http://gate.example:7000"
        browser_module.get_config().browser.proxy_password = "s3cr3t"
        monkeypatch.setattr(
            "linkedin_mcp_server.config.get_config",
            browser_module.get_config,
        )
        browser = _make_mock_browser()
        browser.page.goto = AsyncMock(
            side_effect=Exception("net::ERR_PROXY_CONNECTION_FAILED user:s3cr3t@gate")
        )

        with pytest.raises(ProxyConnectionError) as excinfo:
            await _feed_auth_succeeds(browser)
        assert "s3cr3t" not in str(excinfo.value)

    @pytest.mark.asyncio
    async def test_ordinary_navigation_error_still_returns_false(self, monkeypatch):
        # The existing behaviour for a genuinely broken session is unchanged.
        monkeypatch.setattr(
            "linkedin_mcp_server.config.get_config",
            browser_module.get_config,
        )
        browser = _make_mock_browser()
        browser.page.goto = AsyncMock(side_effect=Exception("net::ERR_TOO_MANY_REDIRECTS"))

        assert await _feed_auth_succeeds(browser) is False


class TestAmbiguousProxyFailureKeepsTheSession:
    """A navigation that fails outright under a proxy is not a dead session.

    Wrong proxy credentials produce no proxy error code: Chromium retries the
    challenge until the page times out. Reading that as an invalid session
    hands the caller an AuthenticationError, whose recovery moves the stored
    profile aside and reruns login through the same broken proxy.
    """

    @pytest.mark.asyncio
    async def test_timeout_under_a_proxy_raises_instead_of_false(self, monkeypatch):
        browser_module.get_config().browser.proxy_server = "http://gate.example:7000"
        monkeypatch.setattr("linkedin_mcp_server.config.get_config", browser_module.get_config)
        browser = _make_mock_browser()
        browser.page.goto = AsyncMock(side_effect=Exception("Page.goto: Timeout 30000ms exceeded."))

        with pytest.raises(ProxyConnectionError, match="gate.example"):
            await _feed_auth_succeeds(browser)

    @pytest.mark.asyncio
    async def test_the_same_timeout_without_a_proxy_still_returns_false(self, monkeypatch):
        # Unchanged behaviour when no proxy is configured: a broken session
        # must still be reported as one.
        monkeypatch.setattr("linkedin_mcp_server.config.get_config", browser_module.get_config)
        browser = _make_mock_browser()
        browser.page.goto = AsyncMock(side_effect=Exception("Page.goto: Timeout 30000ms exceeded."))

        assert await _feed_auth_succeeds(browser) is False

# REMOVED test_an_auth_barrier_under_a_proxy_still_reports_false
# Removed 2026-10-21: the driver no longer reads an auth barrier; the
# barrier check moved to the scraping extractor in f963c82 (2026-07-29).
# The driver's feed check reports the Obscura session state, so a barrier
# cannot be distinguished here any more.

# REMOVED test_a_barrier_behind_a_failed_navigation_still_reports_false
# Removed 2026-10-21: the driver no longer reads an auth barrier; the
# barrier check moved to the scraping extractor in f963c82 (2026-07-29).


class TestFeedFailureDoesNotLeakCredentials:
    """The trace and the log outlive the call, so both must be redacted.

    A driver error can quote the proxy URL. The trace is written to disk and
    the log is what users paste into issue reports, so redacting only the
    user-facing exception message is not enough.
    """

    @pytest.mark.asyncio
    async def test_trace_and_log_are_redacted(self, monkeypatch, caplog):
        config = browser_module.get_config()
        config.browser.proxy_server = "http://gate.example:7000"
        config.browser.proxy_username = "acctzone9"
        config.browser.proxy_password = "s3cr3t"
        monkeypatch.setattr("linkedin_mcp_server.config.get_config", browser_module.get_config)
        browser = _make_mock_browser()
        # No proxy marker, so it reaches the trace and log below rather than
        # being converted straight away.
        browser.page.goto = AsyncMock(
            side_effect=Exception("failed via http://acctzone9:s3cr3t@gate.example:7000")
        )

        traces: list[str] = []

        async def capture_trace(_page, _step, extra=None):
            traces.append(str(extra))

        with (
            patch("linkedin_mcp_server.drivers.browser.record_page_trace", capture_trace),
            caplog.at_level(logging.WARNING),
            pytest.raises(ProxyConnectionError),
        ):
            await _feed_auth_succeeds(browser)

        assert not any("s3cr3t" in trace for trace in traces)
        assert "s3cr3t" not in caplog.text
        assert "acctzone9" not in caplog.text
