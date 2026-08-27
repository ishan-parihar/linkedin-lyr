"""
Browser backend configuration — Obscura (default) and BrowseFleet (remote CloakBrowser pool).

BrowseFleet is selected when ``LINKEDIN_BROWSER_BACKEND=browsefleet`` or
``BROWSEFLEET_URL`` is set; otherwise Obscura is used. This keeps the default
behaviour unchanged for existing installs while allowing a zero-RAM VPS to
offload browsing to ``https://browsefleet.ishanparihar.com`` via HTTP + CDP.
"""

import logging
import os

logger = logging.getLogger(__name__)

_VALID_BACKENDS = frozenset({"obscura", "browsefleet"})


class BackendConfig:
    """Configuration for browser backend.

    Mirror of the historical :class:`ObscuraBrowserManager` config: holds the
    active backend name plus boolean toggles for callers that want to test
    both branches without re-resolving the env / CLI precedence.
    """

    def __init__(
        self,
        backend: str = "obscura",
        use_obscura: bool = True,
        use_browsefleet: bool = False,
    ):
        self.backend = backend
        self.use_obscura = use_obscura
        self.use_browsefleet = use_browsefleet


def get_browser_backend() -> str:
    """Get the current browser backend (``obscura`` or ``browsefleet``)."""
    raw = os.environ.get("LINKEDIN_BROWSER_BACKEND", "").strip().lower()
    if raw in _VALID_BACKENDS:
        return raw
    # Implicit opt-in: any BrowseFleet URL implies browsefleet unless overridden.
    if os.environ.get("BROWSEFLEET_URL", "").strip():
        return "browsefleet"
    return "obscura"


def get_backend_config() -> BackendConfig:
    """Get the current backend configuration.

    Precedence: ``LINKEDIN_BROWSER_BACKEND`` env > ``BROWSEFLEET_URL`` set >
    default ``obscura``. See :func:`get_browser_backend` for the resolution
    rule; this class is the snapshot callers branch on.
    """
    backend = get_browser_backend()
    if backend == "browsefleet":
        return BackendConfig(backend="browsefleet", use_obscura=False, use_browsefleet=True)
    return BackendConfig(backend="obscura", use_obscura=True, use_browsefleet=False)


def should_use_obscura() -> bool:
    """Check if Obscura should be used."""
    return get_browser_backend() == "obscura"


def should_fallback_to_playwright() -> bool:
    """Check if Playwright fallback is enabled (always False)."""
    return False


def is_obscura_enabled() -> bool:
    """Check if Obscura is enabled."""
    return should_use_obscura()


def is_playwright_enabled() -> bool:
    """Check if Playwright is enabled (always False)."""
    return False


def should_use_browsefleet() -> bool:
    """Check if BrowseFleet should be used."""
    return get_browser_backend() == "browsefleet"


def is_browsefleet_enabled() -> bool:
    """Check if BrowseFleet is enabled."""
    return should_use_browsefleet()
