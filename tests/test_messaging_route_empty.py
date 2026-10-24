"""ISH-1777 — the /messaging/ route must fail fast and diagnosably.

Two independent defects were fixed together; both are guarded here.

1. **Wrong timeout class.** ``extractor.py`` imported its timeout error from
   ``patchright`` while the live Obscura driver raises **vanilla**
   ``playwright``'s. The two packages ship separate ``TimeoutError``
   classes, so every ``except PlaywrightTimeoutError:`` clause was dead
   code against the live browser and a benign wait timeout escaped as an
   unhandled tool error::

       Unexpected error in search_conversations: TimeoutError:
       Page.wait_for_function: Timeout 60000ms exceeded.

2. **No empty-shell verdict.** ``/messaging/`` served a permanently empty
   JS shell on 2026-10-24 (1.2 MB of markup, ``readyState=complete``, but
   ``document.body.innerText.length === 0``, no ``<main>``, no conversation
   rows). The content predicate could never become true, so the wait was a
   guaranteed dead wait of 60s (``LINKEDIN_NAV_TIMEOUT_MS=240000`` // 4)
   per call, and the caller could not tell a broken surface from a slow
   LinkedIn.

The route now has its own bounded budgets and returns an explicit
``status: "messaging_route_empty"`` verdict instead.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from patchright.async_api import TimeoutError as PatchrightTimeoutError
from playwright.async_api import TimeoutError as VanillaTimeoutError

from linkedin_mcp_server.core.timeout_errors import TIMEOUT_ERRORS
from linkedin_mcp_server.scraping.extractor import (
    LinkedInExtractor,
    _messaging_content_wait_ms,
    _messaging_shell_wait_ms,
)

SHELL_TIMEOUT_MESSAGE = "Page.wait_for_function: Timeout 60000ms exceeded."


@pytest.fixture
def mock_page():
    """A mock page (same shape as the fixture in test_scraping.py)."""
    page = MagicMock()
    page.goto = AsyncMock()
    page.title = AsyncMock(return_value="LinkedIn")
    page.wait_for_selector = AsyncMock()
    page.wait_for_function = AsyncMock()
    page.evaluate = AsyncMock(
        return_value={"source": "root", "text": "Sample page text", "references": []}
    )
    page.url = "https://www.linkedin.com/messaging/?searchTerm=test"
    page.locator = MagicMock()
    mock_locator = MagicMock()
    mock_locator.count = AsyncMock(return_value=0)
    mock_locator.is_visible = AsyncMock(return_value=False)
    mock_locator.first = mock_locator
    mock_locator.inner_text = AsyncMock(return_value="normal page content")
    mock_locator.filter = MagicMock(return_value=mock_locator)
    page.locator.return_value = mock_locator
    page.main_frame = object()
    page.on = MagicMock()
    page.remove_listener = MagicMock()
    return page


class TestTimeoutErrorCompatibility:
    """The two playwright families ship DIFFERENT TimeoutError classes."""

    def test_tuple_covers_both_families(self):
        assert PatchrightTimeoutError in TIMEOUT_ERRORS
        assert VanillaTimeoutError in TIMEOUT_ERRORS

    def test_classes_are_distinct(self):
        # The whole bug: a single-class except clause cannot catch both.
        assert PatchrightTimeoutError is not VanillaTimeoutError

    @pytest.mark.parametrize(
        "error_class", [PatchrightTimeoutError, VanillaTimeoutError]
    )
    def test_tuple_catches_each_family(self, error_class):
        with pytest.raises(TIMEOUT_ERRORS):
            raise error_class("boom")

    async def test_wait_for_main_text_swallows_vanilla_timeout(self, mock_page):
        """The live driver's timeout must degrade, not escape."""
        extractor = LinkedInExtractor(mock_page)
        mock_page.wait_for_function = AsyncMock(
            side_effect=VanillaTimeoutError(SHELL_TIMEOUT_MESSAGE)
        )

        # Must not raise.
        await extractor._wait_for_main_text(log_context="Messaging search")
        mock_page.wait_for_function.assert_awaited_once()

    async def test_wait_for_main_text_swallows_patchright_timeout(self, mock_page):
        extractor = LinkedInExtractor(mock_page)
        mock_page.wait_for_function = AsyncMock(
            side_effect=PatchrightTimeoutError(SHELL_TIMEOUT_MESSAGE)
        )

        await extractor._wait_for_main_text(log_context="Messaging search")
        mock_page.wait_for_function.assert_awaited_once()


class TestMessagingBudgets:
    """The messaging route must never inherit the 60s generic content wait."""

    def test_shell_budget_is_short(self, monkeypatch):
        monkeypatch.delenv("LINKEDIN_MESSAGING_SHELL_WAIT_MS", raising=False)
        assert _messaging_shell_wait_ms() <= 10000

    def test_content_budget_is_bounded(self, monkeypatch):
        monkeypatch.delenv("LINKEDIN_MESSAGING_CONTENT_WAIT_MS", raising=False)
        assert _messaging_content_wait_ms() <= 30000

    def test_budgets_are_overridable(self, monkeypatch):
        monkeypatch.setenv("LINKEDIN_MESSAGING_SHELL_WAIT_MS", "3000")
        monkeypatch.setenv("LINKEDIN_MESSAGING_CONTENT_WAIT_MS", "4500")
        assert _messaging_shell_wait_ms() == 3000
        assert _messaging_content_wait_ms() == 4500

    def test_invalid_override_falls_back_to_default(self, monkeypatch):
        monkeypatch.setenv("LINKEDIN_MESSAGING_SHELL_WAIT_MS", "not-a-number")
        monkeypatch.setenv("LINKEDIN_MESSAGING_CONTENT_WAIT_MS", "not-a-number")
        assert _messaging_shell_wait_ms() == 8000
        assert _messaging_content_wait_ms() == 20000

    def test_generic_content_budget_is_the_60s_one(self, monkeypatch):
        """Guard the premise: on this deployment the generic wait is 60s."""
        monkeypatch.setenv("LINKEDIN_NAV_TIMEOUT_MS", "240000")
        from linkedin_mcp_server.scraping.extractor import _content_wait_ms

        assert _content_wait_ms(10000) == 60000


class TestMessagingPaintDetection:
    async def test_paint_false_when_shell_stays_empty(self, mock_page):
        extractor = LinkedInExtractor(mock_page)
        mock_page.wait_for_function = AsyncMock(
            side_effect=VanillaTimeoutError(SHELL_TIMEOUT_MESSAGE)
        )

        assert await extractor._wait_for_messaging_paint(log_context="Messaging") is False
        # The wait must use the SHORT shell budget, not the 60s content one.
        timeout = mock_page.wait_for_function.call_args.kwargs["timeout"]
        assert timeout == _messaging_shell_wait_ms()
        assert timeout <= 10000

    async def test_paint_true_when_body_painted(self, mock_page):
        extractor = LinkedInExtractor(mock_page)
        mock_page.wait_for_function = AsyncMock(return_value=True)

        assert await extractor._wait_for_messaging_paint(log_context="Messaging") is True


class TestMessagingRouteEmptyVerdict:
    async def test_search_conversations_returns_route_empty(self, mock_page):
        extractor = LinkedInExtractor(mock_page)
        mock_page.wait_for_function = AsyncMock(
            side_effect=VanillaTimeoutError(SHELL_TIMEOUT_MESSAGE)
        )

        with (
            patch.object(extractor, "_navigate_to_page", new_callable=AsyncMock),
            patch(
                "linkedin_mcp_server.scraping.extractor.detect_rate_limit",
                new_callable=AsyncMock,
            ),
            patch(
                "linkedin_mcp_server.scraping.extractor.handle_modal_close",
                new_callable=AsyncMock,
            ),
            patch.object(
                extractor, "_wait_for_main_text", new_callable=AsyncMock
            ) as content_wait,
            patch.object(
                extractor, "_extract_root_content", new_callable=AsyncMock
            ) as extract_root,
        ):
            result = await extractor.search_conversations("test")

        assert result["status"] == "messaging_route_empty"
        assert "messaging_route_empty" in result["sections"]["search_results"]
        assert result["diagnostic"]["route"] == "messaging"
        # The 60s content wait must never run on an empty shell, and no
        # extraction may be attempted.
        content_wait.assert_not_awaited()
        extract_root.assert_not_awaited()

    async def test_get_inbox_returns_route_empty(self, mock_page):
        extractor = LinkedInExtractor(mock_page)
        mock_page.url = "https://www.linkedin.com/messaging/"
        mock_page.wait_for_function = AsyncMock(
            side_effect=VanillaTimeoutError(SHELL_TIMEOUT_MESSAGE)
        )

        with (
            patch.object(extractor, "_navigate_to_page", new_callable=AsyncMock),
            patch(
                "linkedin_mcp_server.scraping.extractor.detect_rate_limit",
                new_callable=AsyncMock,
            ),
            patch(
                "linkedin_mcp_server.scraping.extractor.handle_modal_close",
                new_callable=AsyncMock,
            ),
            patch.object(
                extractor, "_wait_for_main_text", new_callable=AsyncMock
            ) as content_wait,
        ):
            result = await extractor.get_inbox()

        assert result["status"] == "messaging_route_empty"
        assert "messaging_route_empty" in result["sections"]["inbox"]
        content_wait.assert_not_awaited()

    async def test_painted_route_still_uses_the_content_path(self, mock_page):
        """A healthy route must be unaffected: content extraction still runs."""
        extractor = LinkedInExtractor(mock_page)
        mock_page.wait_for_function = AsyncMock(return_value=True)

        with (
            patch.object(extractor, "_navigate_to_page", new_callable=AsyncMock),
            patch(
                "linkedin_mcp_server.scraping.extractor.detect_rate_limit",
                new_callable=AsyncMock,
            ),
            patch(
                "linkedin_mcp_server.scraping.extractor.handle_modal_close",
                new_callable=AsyncMock,
            ),
            patch.object(
                extractor, "_wait_for_main_text", new_callable=AsyncMock
            ) as content_wait,
            patch.object(
                extractor,
                "_extract_root_content",
                new_callable=AsyncMock,
                return_value={"text": "Result 1", "references": []},
            ),
            patch(
                "linkedin_mcp_server.scraping.extractor.strip_linkedin_noise",
                return_value="Result 1",
            ),
            patch(
                "linkedin_mcp_server.scraping.extractor.build_references",
                return_value=[],
            ),
            patch.object(
                extractor,
                "_extract_conversation_thread_refs",
                new_callable=AsyncMock,
                return_value=[],
            ),
        ):
            result = await extractor.search_conversations("test")

        assert "status" not in result
        assert result["sections"]["search_results"] == "Result 1"
        content_wait.assert_awaited_once()
        # The messaging content wait must be the bounded one.
        assert content_wait.call_args.kwargs["timeout"] == _messaging_content_wait_ms()
