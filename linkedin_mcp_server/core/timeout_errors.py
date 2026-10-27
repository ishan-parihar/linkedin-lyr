"""Playwright timeout-error compatibility across the patchright/playwright split.

The runtime drives LinkedIn through Obscura's CDP server using **vanilla**
playwright — ``linkedin_mcp_server/core/obscura_browser.py`` imports
``playwright.async_api`` — while most of the codebase imports its timeout
error from ``patchright.async_api``.

Those two packages ship **separate** ``TimeoutError`` classes. Verified on
2026-10-24 against the live venv:

    patchright.async_api.TimeoutError is playwright.async_api.TimeoutError  -> False
    issubclass(playwright.TO, patchright.TO)                             -> False
    issubclass(patchright.TO, playwright.TO)                             -> False

So ``except PlaywrightTimeoutError:`` (patchright) does **not** catch the
``TimeoutError`` that a vanilla-playwright page raises. Every
graceful-degrade clause written that way was dead code against the live
browser: a benign wait timeout escaped as an unhandled tool error.

Impact (ISH-1777): on 2026-10-24 ten ``/messaging/`` navigations rendered
an empty JS shell (``body_length: 0``), so the content wait never
satisfied its predicate and burned the full 60s budget
(``_content_wait_ms`` -> ``LINKEDIN_NAV_TIMEOUT_MS=240000`` // 4). The
timeout escaped ``_wait_for_main_text``'s ``except`` clause and surfaced
as::

    Unexpected error in search_conversations: TimeoutError:
    Page.wait_for_function: Timeout 60000ms exceeded.

Use ``TIMEOUT_ERRORS`` in ``except`` clauses instead of a single class::

    from linkedin_mcp_server.core.timeout_errors import TIMEOUT_ERRORS
    try:
        await page.wait_for_function(...)
    except TIMEOUT_ERRORS:
        ...

``ObscuraPage.wait_for_function`` already normalises its *evaluate-fallback*
path to the patchright class (see the t_7fa793c3 note in
``obscura_browser.py``); this module closes the same gap for the native
path and for every other driver.
"""

from __future__ import annotations

from patchright.async_api import TimeoutError as PatchrightTimeoutError

try:  # pragma: no cover - vanilla playwright is a hard dependency, but the
    # import must not be able to break the whole package if it is absent.
    from playwright.async_api import TimeoutError as PlaywrightTimeoutError
except Exception:  # pragma: no cover - defensive: keep patchright's class.
    PlaywrightTimeoutError = None  # type: ignore[assignment]


def _build_timeout_errors() -> tuple[type[BaseException], ...]:
    errors: list[type[BaseException]] = [PatchrightTimeoutError]
    if PlaywrightTimeoutError is not None and PlaywrightTimeoutError not in errors:
        errors.append(PlaywrightTimeoutError)
    return tuple(errors)


#: Every timeout exception the live driver stack can raise. Catch this
#: tuple, never a single class, or the timeout escapes as a tool failure.
TIMEOUT_ERRORS: tuple[type[BaseException], ...] = _build_timeout_errors()

__all__ = ["TIMEOUT_ERRORS", "PatchrightTimeoutError", "PlaywrightTimeoutError"]
