"""Tests for BrowseFleet browser manager and backend selection."""

import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from linkedin_mcp_server.config import reset_config
from linkedin_mcp_server.config.schema import BrowserConfig, ConfigurationError
from linkedin_mcp_server.core.browser_backend import (
    get_browser_backend,
    should_use_browsefleet,
    should_use_obscura,
)


class TestBrowserBackend:
    def test_default_is_obscura(self, monkeypatch):
        monkeypatch.delenv("LINKEDIN_BROWSER_BACKEND", raising=False)
        monkeypatch.delenv("BROWSEFLEET_URL", raising=False)
        assert get_browser_backend() == "obscura"
        assert should_use_obscura() is True
        assert should_use_browsefleet() is False

    def test_explicit_browsefleet(self, monkeypatch):
        monkeypatch.setenv("LINKEDIN_BROWSER_BACKEND", "browsefleet")
        assert get_browser_backend() == "browsefleet"
        assert should_use_browsefleet() is True

    def test_implicit_browsefleet_via_url(self, monkeypatch):
        monkeypatch.delenv("LINKEDIN_BROWSER_BACKEND", raising=False)
        monkeypatch.setenv("BROWSEFLEET_URL", "https://browsefleet.example.com")
        assert get_browser_backend() == "browsefleet"

    def test_case_insensitive(self, monkeypatch):
        monkeypatch.setenv("LINKEDIN_BROWSER_BACKEND", "BrowseFleet")
        assert get_browser_backend() == "browsefleet"

    def test_invalid_falls_back_to_obscura(self, monkeypatch):
        monkeypatch.setenv("LINKEDIN_BROWSER_BACKEND", "unknown")
        monkeypatch.delenv("BROWSEFLEET_URL", raising=False)
        assert get_browser_backend() == "obscura"


class TestBrowserConfigBrowseFleet:
    def test_defaults(self):
        cfg = BrowserConfig()
        assert cfg.browsefleet_url is None
        assert cfg.browsefleet_token is None
        assert cfg.browsefleet_profile_id is None
        assert cfg.browsefleet_timeout_ms == 30000

    def test_valid_url(self):
        cfg = BrowserConfig(browsefleet_url="https://browsefleet.ishanparihar.com")
        cfg.validate()  # should not raise

    def test_invalid_url_raises(self):
        cfg = BrowserConfig(browsefleet_url="not-a-url")
        with pytest.raises(ConfigurationError, match="browsefleet_url"):
            cfg.validate()

    def test_invalid_timeout_raises(self):
        cfg = BrowserConfig(browsefleet_timeout_ms=0)
        with pytest.raises(ConfigurationError):
            cfg.validate()


class TestBrowseFleetBrowserManager:
    @pytest.mark.asyncio
    async def test_requires_url(self, monkeypatch, tmp_path):
        monkeypatch.setenv("LINKEDIN_MCP_TOOL_MODE", "1")
        monkeypatch.delenv("BROWSEFLEET_URL", raising=False)
        monkeypatch.delenv("LINKEDIN_BROWSER_BACKEND", raising=False)
        monkeypatch.setenv("HOME", str(tmp_path))
        reset_config()
        from linkedin_mcp_server.core.browsefleet_browser import BrowseFleetBrowserManager

        m = BrowseFleetBrowserManager()
        with pytest.raises(RuntimeError, match="BROWSEFLEET_URL is not set"):
            await m.start()

    @pytest.mark.asyncio
    async def test_start_creates_session_and_connects(self, monkeypatch, tmp_path):
        # Mock httpx and playwright
        monkeypatch.setenv("LINKEDIN_MCP_TOOL_MODE", "1")
        monkeypatch.setenv("BROWSEFLEET_URL", "https://browsefleet.example.com")
        monkeypatch.setenv("BROWSEFLEET_TOKEN", "test-token")
        monkeypatch.setenv("HOME", str(tmp_path))
        reset_config()
        # Ensure no portable cookies interfere
        (tmp_path / ".linkedin-lyr").mkdir(parents=True)

        from linkedin_mcp_server.core.browsefleet_browser import BrowseFleetBrowserManager

        mock_resp = MagicMock()
        mock_resp.status_code = 201
        mock_resp.json.return_value = {
            "id": "test-id",
            "websocketUrl": "wss://browsefleet.example.com/cdp/test-id",
            "viewerUrl": "https://example.com/live",
        }

        mock_client = AsyncMock()
        mock_client.__aenter__.return_value = mock_client
        mock_client.__aexit__.return_value = False
        mock_client.post.return_value = mock_resp

        mock_page = AsyncMock()
        mock_page.url = "https://example.com"
        mock_context = AsyncMock()
        mock_context.new_page.return_value = mock_page
        mock_context.add_cookies = AsyncMock()
        mock_context.cookies = AsyncMock(return_value=[])
        mock_browser = MagicMock()
        mock_browser.contexts = [mock_context]
        mock_browser.close = AsyncMock()

        mock_pw = MagicMock()
        mock_pw.chromium.connect_over_cdp = AsyncMock(return_value=mock_browser)
        mock_pw.stop = AsyncMock()
        mock_pw_obj = MagicMock()
        mock_pw_obj.start = AsyncMock(return_value=mock_pw)

        with patch("httpx.AsyncClient", return_value=mock_client):
            with patch("playwright.async_api.async_playwright", return_value=mock_pw_obj):
                m = BrowseFleetBrowserManager(viewport={"width": 1280, "height": 720})
                await m.start()
                assert m.session_id == "test-id"
                assert m.is_authenticated is False  # no cookies
                # goto
                mock_page.goto = AsyncMock()
                await m.goto("https://example.com")
                mock_page.goto.assert_called_once()
                # close
                mock_client.post.reset_mock()
                mock_client.post.return_value = MagicMock(status_code=200)
                await m.close()
                assert m.close_confirmed is True

    @pytest.mark.asyncio
    async def test_cdp_token_appended(self, monkeypatch, tmp_path):
        monkeypatch.setenv("LINKEDIN_MCP_TOOL_MODE", "1")
        monkeypatch.setenv("BROWSEFLEET_URL", "https://browsefleet.example.com")
        monkeypatch.setenv("BROWSEFLEET_TOKEN", "tok123")
        monkeypatch.setenv("HOME", str(tmp_path))
        reset_config()
        (tmp_path / ".linkedin-lyr").mkdir(parents=True)

        from linkedin_mcp_server.core.browsefleet_browser import BrowseFleetBrowserManager

        mock_resp = MagicMock()
        mock_resp.status_code = 201
        mock_resp.json.return_value = {
            "id": "sid-1",
            "websocketUrl": "wss://browsefleet.example.com/cdp/sid-1",
        }

        mock_client = AsyncMock()
        mock_client.__aenter__.return_value = mock_client
        mock_client.__aexit__.return_value = False
        mock_client.post.return_value = mock_resp

        captured = {}

        async def fake_connect(url):
            captured["url"] = url
            mock_browser = MagicMock()
            mock_browser.contexts = []
            mock_browser.new_context = AsyncMock()
            # Provide a mock context with new_page
            ctx = AsyncMock()
            ctx.new_page = AsyncMock(return_value=AsyncMock())
            ctx.add_cookies = AsyncMock()
            mock_browser.new_context.return_value = ctx
            mock_browser.close = AsyncMock()
            return mock_browser

        mock_pw = MagicMock()
        mock_pw.chromium.connect_over_cdp.side_effect = fake_connect
        mock_pw.stop = AsyncMock()
        mock_pw_obj = MagicMock()
        mock_pw_obj.start = AsyncMock(return_value=mock_pw)

        with patch("httpx.AsyncClient", return_value=mock_client):
            with patch("playwright.async_api.async_playwright", return_value=mock_pw_obj):
                m = BrowseFleetBrowserManager()
                await m.start()
                # Token should be appended as ?apiKey=
                assert "apiKey=tok123" in captured["url"]
                await m.close()

    def test_load_proxy_env_reads_bf_files(self, monkeypatch, tmp_path):
        monkeypatch.setenv("HOME", str(tmp_path))
        # Create bf.env
        bf_dir = tmp_path / ".linkedin-lyr"
        bf_dir.mkdir(parents=True)
        (bf_dir / "bf.env").write_text("LINKEDIN_BROWSER_BACKEND=browsefleet\nBROWSEFLEET_URL=https://bf.example.com\n")
        (tmp_path / ".browsefleet.env").write_text("BROWSEFLEET_TOKEN=tok-from-file\n")
        # Ensure env is clean
        monkeypatch.delenv("LINKEDIN_BROWSER_BACKEND", raising=False)
        monkeypatch.delenv("BROWSEFLEET_URL", raising=False)
        monkeypatch.delenv("BROWSEFLEET_TOKEN", raising=False)

        from linkedin_mcp_server.common_utils import load_proxy_env

        load_proxy_env()
        assert os.environ["LINKEDIN_BROWSER_BACKEND"] == "browsefleet"
        assert os.environ["BROWSEFLEET_URL"] == "https://bf.example.com"
        assert os.environ["BROWSEFLEET_TOKEN"] == "tok-from-file"
