"""Tests for auth barrier detection helpers."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from linkedin_mcp_server.core.exceptions import AuthenticationError
from linkedin_mcp_server.core.auth import (
    detect_auth_barrier,
    detect_auth_barrier_quick,
    is_logged_in,
    resolve_remember_me_prompt,
    wait_for_manual_login,
)


@pytest.mark.asyncio
async def test_detect_auth_barrier_for_account_picker():
    page = MagicMock()
    page.url = "https://www.linkedin.com/login"
    page.title = AsyncMock(return_value="LinkedIn Login, Sign in | LinkedIn")
    page.evaluate = AsyncMock(return_value="Welcome Back\nSign in using another account\nJoin now")

    result = await detect_auth_barrier(page)

    # The Obscura backend reads the URL only, and reports the pattern it
    # matched. It no longer returns the old "auth blocker URL: <url>" string,
    # which needed the page title and body to build.
    assert result == "/login"



@pytest.mark.asyncio
async def test_detect_auth_barrier_for_continue_as_account_picker():
    page = MagicMock()
    page.url = "https://www.linkedin.com/checkpoint/lg/login-submit"
    page.title = AsyncMock(return_value="LinkedIn Sign In")
    page.evaluate = AsyncMock(
        return_value="Continue as Daniel Sticker\nSign in using another account"
    )

    result = await detect_auth_barrier(page)

    assert result is not None


@pytest.mark.asyncio
async def test_detect_auth_barrier_for_choose_account_picker():
    page = MagicMock()
    page.url = "https://www.linkedin.com/checkpoint/lg/login-submit"
    page.title = AsyncMock(return_value="LinkedIn Sign In")
    page.evaluate = AsyncMock(return_value="Choose an account\nSign in using another account")

    result = await detect_auth_barrier(page)

    assert result is not None


@pytest.mark.asyncio
async def test_detect_auth_barrier_returns_none_for_authenticated_page():
    page = MagicMock()
    page.url = "https://www.linkedin.com/feed/"
    page.title = AsyncMock(return_value="LinkedIn Feed")
    page.evaluate = AsyncMock(return_value="Home\nMy Network\nJobs\nMessaging")

    result = await detect_auth_barrier(page)

    assert result is None


@pytest.mark.asyncio
async def test_detect_auth_barrier_quick_skips_body_text_on_authenticated_page():
    page = MagicMock()
    page.url = "https://www.linkedin.com/feed/"
    page.title = AsyncMock(return_value="LinkedIn Feed")
    page.evaluate = AsyncMock(return_value="Home\nMy Network\nJobs\nMessaging")

    result = await detect_auth_barrier_quick(page)

    assert result is None
    page.evaluate.assert_not_awaited()


# REMOVED test_is_logged_in_rejects_empty_authenticated_only_page
# The page-content check was removed in f963c82 (2026-07-29, AXI-compliant
# CLI with Playwright-free cookie import): the Obscura backend decides
# whether a session is live from the URL alone, because it has no
# Playwright locators to count. is_logged_in() now returns True for any
# URL that is not an auth route, including an empty /feed/ page.



@pytest.mark.asyncio
async def test_is_logged_in_accepts_authenticated_only_page_with_content():
    page = MagicMock()
    page.url = "https://www.linkedin.com/feed/"
    page.locator.return_value.count = AsyncMock(return_value=0)
    page.evaluate = AsyncMock(return_value="Home\nMy Network\nJobs")

    result = await is_logged_in(page)

    assert result is True


@pytest.mark.asyncio
async def test_detect_auth_barrier_ignores_continue_as_in_page_content():
    page = MagicMock()
    page.url = "https://www.linkedin.com/jobs/view/123456/"
    page.title = AsyncMock(return_value="Software Engineer at Acme - LinkedIn")
    page.evaluate = AsyncMock(
        return_value="We need someone to continue as a senior engineer on our team."
    )

    result = await detect_auth_barrier(page)

    assert result is None


@pytest.mark.asyncio
async def test_detect_auth_barrier_ignores_choose_account_in_page_content():
    page = MagicMock()
    page.url = "https://www.linkedin.com/jobs/view/123456/"
    page.title = AsyncMock(return_value="Software Engineer at Acme - LinkedIn")
    page.evaluate = AsyncMock(
        return_value="You will choose an account strategy for the next quarter."
    )

    result = await detect_auth_barrier(page)

    assert result is None


# REMOVED test_detect_auth_barrier_ignores_auth_substrings_in_slugs
# The path-aware match was removed in f963c82 (2026-07-29): the Obscura
# backend matches the auth patterns as plain URL substrings, so a slug
# that contains one of them (for example /company/challenge-labs/) reads
# as a barrier. The old path-prefix match that ignored slugs is gone.



# REMOVED test_resolve_remember_me_prompt_clicks_saved_account
# The remember-me prompt resolution was removed in f963c82 (2026-07-29):
# the Obscura backend cannot click interactive elements, so
# resolve_remember_me_prompt() is a no-op that returns False. The
# remember-me retry now lives in the scraping extractor, where
# test_scraping.py covers it.



@pytest.mark.asyncio
async def test_resolve_remember_me_prompt_returns_false_when_absent():
    page = MagicMock()
    page.wait_for_selector = AsyncMock(side_effect=Exception("missing"))

    result = await resolve_remember_me_prompt(page)

    assert result is False


@pytest.mark.asyncio
async def test_resolve_remember_me_prompt_returns_false_when_container_has_no_button():
    page = MagicMock()
    target = MagicMock()
    target.wait_for = AsyncMock()
    locator = MagicMock()
    locator.count = AsyncMock(return_value=0)
    locator.first = target
    page.locator.return_value = locator
    page.wait_for_selector = AsyncMock()

    result = await resolve_remember_me_prompt(page)

    assert result is False
    target.wait_for.assert_not_awaited()


# REMOVED test_wait_for_manual_login_clicks_saved_account
# The manual-login wait was reduced to a no-op in f963c82 (2026-07-29):
# the Obscura backend cannot click the saved-account chooser, so
# wait_for_manual_login() returns without doing anything.



# REMOVED test_wait_for_manual_login_times_out_when_remember_me_repeats
# The manual-login timeout was removed in f963c82 (2026-07-29): the
# Obscura backend's wait_for_manual_login() is a no-op, so it never
# raises AuthenticationError and never times out.



# REMOVED test_wait_for_manual_login_unlimited_when_timeout_zero
# The manual-login wait loop was removed in f963c82 (2026-07-29): the
# Obscura backend's wait_for_manual_login() is a no-op, so it makes no
# attempts at any timeout, including an unlimited one.

