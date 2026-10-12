# src/linkedin_mcp_server/__init__.py
"""
LinkedIn MCP Server package.

A Model Context Protocol (MCP) server that provides LinkedIn integration capabilities
for AI assistants. This package enables secure LinkedIn profile, company, and job
data scraping through a standardized MCP interface.

Key Features:
- Secure LinkedIn authentication via session files
- LinkedIn profile, company, and job data scraping
- MCP-compliant server implementation using FastMCP
- Playwright browser automation with session persistence
- Layered configuration system with secure credential storage
- Docker containerization for easy deployment
- Claude Desktop MCP Bundle (MCPB, formerly DXT) support

Architecture:
- Clean separation between authentication, driver management, and MCP server
- Singleton pattern for browser session management
- Comprehensive error handling and logging
- Cross-platform compatibility (macOS, Windows, Linux)
"""

# -- t_5d395ac7: foreign-PYTHONPATH site-packages isolation --
# Cron-agent runtimes can export PYTHONPATH pointing at a FOREIGN venv
# site-packages (e.g. the hermes tool env at
# ~/.hermes/installs/*/venv/lib/python3.14/site-packages). sys.path
# PYTHONPATH entries sit BEFORE this venv (python3.13) site-packages, so
# import pydantic resolves to the foreign copy whose binary
# pydantic_core._pydantic_core (cp314 wheel) fails under cp313 with
# ModuleNotFoundError - killing every CLI invocation. Scrub sys.path
# entries (and PYTHONPATH, so spawned children inherit a clean env)
# that are site-packages dirs NOT under this interpreter prefix or
# base_prefix. Project-root PYTHONPATH entries are untouched.
import os as _os
import sys as _sys

def _foreign_site_packages(path):
    try:
        rp = _os.path.realpath(path or ".")
    except Exception:
        return False
    if not rp.endswith(_os.sep + "site-packages"):
        return False
    prefixes = tuple(
        _os.path.realpath(x) for x in (_sys.prefix, _sys.base_prefix)
    )
    return not rp.startswith(prefixes)

_sys.path[:] = [p for p in _sys.path if not _foreign_site_packages(p)]
_pp = _os.environ.get("PYTHONPATH")
if _pp:
    _kept = [p for p in _pp.split(_os.pathsep) if p and not _foreign_site_packages(p)]
    if _kept:
        _os.environ["PYTHONPATH"] = _os.pathsep.join(_kept)
    else:
        _os.environ.pop("PYTHONPATH", None)
del _foreign_site_packages

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("linkedin-lyr")
except PackageNotFoundError:
    __version__ = "0.0.0.dev"  # Running from source without install
