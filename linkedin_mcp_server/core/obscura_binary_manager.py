"""
Obscura binary downloader and updater.

Automatically downloads and manages the latest Obscura binary from GitHub releases.
"""

import asyncio
import hashlib
import logging
import os
import platform
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Optional
import json
import zipfile
import tarfile

import aiohttp
from packaging import version

logger = logging.getLogger(__name__)

# Obscura GitHub repository
OBSURA_REPO = "ishan-parihar/obscura-core"
OBSURA_API_URL = f"https://api.github.com/repos/{OBSURA_REPO}/releases/latest"
OBSURA_RELEASES_URL = f"https://github.com/{OBSURA_REPO}/releases"

# Binary installation paths
# Prefer ~/.local/bin (persistent across reboots) over /tmp (tmpfs, wiped at boot).
DEFAULT_BINARY_PATH = Path.home() / ".local" / "bin" / "obscura"
FALLBACK_BINARY_PATHS = [
    DEFAULT_BINARY_PATH,
    Path("/usr/local/bin/obscura"),
    Path("/usr/bin/obscura"),
    Path("/opt/obscura/obscura"),
    Path.home() / "obscura-bin" / "obscura",
]
METADATA_FILE = Path.home() / ".linkedin-lyr" / "obscura_metadata.json"


class ObscuraBinaryManager:
    """Manage Obscura binary download and updates."""

    def __init__(
        self,
        binary_path: Optional[Path] = None,
        auto_update: bool = True,
    ):
        self.binary_path = binary_path or DEFAULT_BINARY_PATH
        self.auto_update = auto_update
        self._metadata: dict = {}

        logger.info("Obscura binary manager initialized with path: %s", self.binary_path)

    async def get_latest_version(self) -> str:
        """Get the latest Obscura version from GitHub releases."""
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(OBSURA_API_URL) as response:
                    if response.status == 200:
                        data = await response.json()
                        version = data.get("tag_name", "").lstrip("v")
                        logger.info("Latest Obscura version: %s", version)
                        return version
                    else:
                        logger.error("Failed to fetch latest version: HTTP %d", response.status)
                        # Return a fallback version if API fails
                        return "0.1.11"  # Known working version
        except Exception as e:
            logger.error("Error fetching latest Obscura version: %s", e)
            # Return a fallback version if API fails
            return "0.1.11"  # Known working version

    def _find_existing_binary(self) -> Path | None:
        """Walk the fallback locations and return the first existing binary.

        /tmp is wiped on reboot, so a missing binary there is not a reason to
        re-download; the binary may already live at ~/.local/bin or any other
        stable location. Returns None only when truly not installed anywhere.
        """
        for path in FALLBACK_BINARY_PATHS:
            try:
                if path.exists() and path.is_file() and os.access(path, os.X_OK):
                    return path
            except Exception:
                continue
        # Last-ditch: PATH lookup
        path_on_path = shutil.which("obscura")
        if path_on_path:
            return Path(path_on_path)
        return None

    async def download_latest_binary(self, force: bool = False) -> Path:
        """Download the latest Obscura binary for the current platform."""
        # If an existing binary is present anywhere (not just DEFAULT path),
        # prefer it. Only download when truly missing or `force=True`.
        existing = self._find_existing_binary()
        if existing and not force:
            # If the discovered binary is NOT our default path, sync it there
            # so subsequent runs find it without re-searching.
            if existing != self.binary_path:
                try:
                    self.binary_path.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(existing, self.binary_path)
                    os.chmod(self.binary_path, 0o755)
                    logger.info(
                        "Obscura binary already present at %s, mirrored to %s",
                        existing, self.binary_path,
                    )
                except Exception as e:
                    logger.warning("Could not mirror existing binary: %s", e)
                    # Still okay — caller can use the original location
                    return existing
            if await self.is_up_to_date():
                logger.info("Obscura binary is up to date")
                return self.binary_path
            # Fallthrough: stale, but present. Update in background-ish.

        logger.info("Downloading latest Obscura binary...")

        try:
            # Get latest release info
            latest_version = await self.get_latest_version()

            # Determine platform-specific download URL
            download_url = self._get_download_url(latest_version)
            if not download_url:
                raise Exception(f"No download URL found for platform: {platform.system()}")

            # Download to temporary file
            temp_dir = Path(tempfile.mkdtemp(prefix="obscura_download_"))
            download_file = temp_dir / self._get_archive_name()

            try:
                await self._download_file(download_url, download_file)

                # Extract the binary
                binary = await self._extract_binary(download_file, temp_dir)

                # Make executable and install
                await self._install_binary(binary)

                # Update metadata
                self._metadata = {
                    "version": latest_version,
                    "installed_at": time.time(),
                    "download_url": download_url,
                }
                await self._save_metadata()

                logger.info("Successfully installed Obscura %s", latest_version)
                return self.binary_path
            finally:
                # Cleanup temp directory
                shutil.rmtree(temp_dir, ignore_errors=True)
        except Exception as e:
            logger.warning(
                "Obscura download/update failed (using existing binary if present): %s", e
            )
            # If ANY binary exists, use it — never blow up on a 404 from
            # the releases endpoint. LinkedIn login should still work with
            # the previous binary version.
            fallback = self._find_existing_binary()
            if fallback:
                return fallback
            raise

    def _get_download_url(self, version: str) -> Optional[str]:
        """Get the download URL for the current platform."""
        system = platform.system().lower()
        machine = platform.machine().lower()

        # Map platform to Obscura asset names
        if system == "linux":
            if machine in ("x86_64", "amd64"):
                return f"{OBSURA_RELEASES_URL}/download/v{version}/obscura-linux-x86_64"
            elif machine in ("aarch64", "arm64"):
                return f"{OBSURA_RELEASES_URL}/download/v{version}/obscura-linux-aarch64"
        elif system == "darwin":
            if machine in ("x86_64", "amd64"):
                return f"{OBSURA_RELEASES_URL}/download/v{version}/obscura-darwin-x86_64"
            elif machine in ("aarch64", "arm64"):
                return f"{OBSURA_RELEASES_URL}/download/v{version}/obscura-darwin-aarch64"
        elif system == "windows":
            if machine in ("x86_64", "amd64"):
                return f"{OBSURA_RELEASES_URL}/download/v{version}/obscura-windows-x86_64.exe"

        logger.error("Unsupported platform: %s %s", system, machine)
        return None

    def _get_archive_name(self) -> str:
        """Get the expected archive name for the current platform."""
        system = platform.system().lower()
        machine = platform.machine().lower()

        if system == "linux":
            return f"obscura-{machine}.tar.gz"
        elif system == "darwin":
            return f"obscura-{machine}.tar.gz"
        elif system == "windows":
            return f"obscura-{machine}.zip"

        return "obscura-archive"

    async def _download_file(self, url: str, destination: Path) -> None:
        """Download a file from URL to destination."""
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as response:
                if response.status == 200:
                    with open(destination, "wb") as f:
                        async for chunk in response.content.iter_chunked(8192):
                            f.write(chunk)
                    logger.info("Downloaded %s to %s", url, destination)
                else:
                    raise Exception(f"Download failed with HTTP {response.status}")

    async def _extract_binary(self, archive: Path, temp_dir: Path) -> Path:
        """Extract the binary from the archive."""
        if archive.suffix == ".zip":
            with zipfile.ZipFile(archive, "r") as zip_ref:
                zip_ref.extractall(temp_dir)
        elif archive.suffix in (".tar", ".gz", ".tgz"):
            with tarfile.open(archive, "r:*") as tar_ref:
                tar_ref.extractall(temp_dir)
        else:
            # Assume it's a direct binary
            return archive

        # Find the extracted binary
        for file in temp_dir.rglob("obscura*"):
            if file.is_file() and not file.suffix:
                return file
            if file.name == "obscura" or file.name == "obscura.exe":
                return file

        raise Exception("Could not find extracted binary")

    async def _install_binary(self, source: Path) -> None:
        """Install the binary to the target location."""
        # Ensure parent directory exists
        self.binary_path.parent.mkdir(parents=True, exist_ok=True)

        # Copy binary
        shutil.copy2(source, self.binary_path)

        # Make executable on Unix-like systems
        if platform.system() != "Windows":
            os.chmod(self.binary_path, 0o755)

        logger.info("Installed Obscura binary to %s", self.binary_path)

    async def is_up_to_date(self) -> bool:
        """Check if the installed binary is up to date."""
        if not self.binary_path.exists():
            return False

        try:
            # Load metadata
            await self._load_metadata()

            if not self._metadata.get("version"):
                return False

            # Get latest version
            latest_version = await self.get_latest_version()
            current_version = self._metadata["version"]

            # Compare versions
            return version.parse(current_version) >= version.parse(latest_version)

        except Exception as e:
            logger.warning("Error checking version: %s", e)
            return False

    async def _load_metadata(self) -> None:
        """Load metadata from file."""
        if METADATA_FILE.exists():
            try:
                self._metadata = json.loads(METADATA_FILE.read_text())
            except Exception as e:
                logger.warning("Failed to load metadata: %s", e)

    async def _save_metadata(self) -> None:
        """Save metadata to file."""
        try:
            METADATA_FILE.parent.mkdir(parents=True, exist_ok=True)
            METADATA_FILE.write_text(json.dumps(self._metadata, indent=2))
        except Exception as e:
            logger.warning("Failed to save metadata: %s", e)

    async def ensure_binary(self) -> Path:
        """Ensure the Obscura binary is available and up to date."""
        if not self.binary_path.exists():
            logger.info("Obscura binary not found, downloading...")
            return await self.download_latest_binary()

        # If the binary exists, skip auto-update by default. The upstream
        # GitHub release URL (graphite-ng/obscura) may be unavailable (404),
        # and re-downloading on every start wastes resources when a working
        # binary is already installed. Set OBSCURA_AUTO_UPDATE=1 to force.
        if self.auto_update and os.environ.get("OBSCURA_AUTO_UPDATE", "").lower() in (
            "1",
            "true",
            "yes",
        ):
            logger.info("Checking for Obscura updates...")
            try:
                if await self.is_up_to_date():
                    logger.info("Obscura binary is up to date")
                    return self.binary_path
                else:
                    logger.info("Updating Obscura binary...")
                    return await self.download_latest_binary()
            except Exception as e:
                logger.warning("Failed to update Obscura binary: %s", e)
                # Return existing binary even if update failed
                return self.binary_path

        logger.debug("Using existing Obscura binary at %s", self.binary_path)
        return self.binary_path

    def get_version(self) -> Optional[str]:
        """Get the version of the installed binary."""
        try:
            result = subprocess.run(
                [str(self.binary_path), "--version"], capture_output=True, text=True, timeout=5
            )
            if result.returncode == 0:
                return result.stdout.strip()
        except Exception as e:
            logger.warning("Failed to get Obscura version: %s", e)

        return None


# Global instance
_binary_manager: ObscuraBinaryManager | None = None


def get_binary_manager() -> ObscuraBinaryManager:
    """Get the global binary manager instance."""
    global _binary_manager

    if _binary_manager is None:
        _binary_manager = ObscuraBinaryManager()

    return _binary_manager


async def ensure_obscura_binary() -> Path:
    """Ensure the Obscura binary is available and up to date."""
    manager = get_binary_manager()
    return await manager.ensure_binary()


async def download_obscura_binary(force: bool = False) -> Path:
    """Force download the latest Obscura binary."""
    manager = get_binary_manager()
    return await manager.download_latest_binary(force=force)
