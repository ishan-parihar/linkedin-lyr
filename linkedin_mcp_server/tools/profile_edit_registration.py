"""Profile-edit MCP tools (Voyager direct-HTTP surface).

Unlike the browser-backed read tools these need no extractor: the Voyager
client reads the flat cookie file and talks REST+CSRF directly, which is the
only LinkedIn surface that survives on headless boxes (#2344).
"""

import logging
from typing import Annotated, Any

from fastmcp import Context, FastMCP
from pydantic import Field

from linkedin_mcp_server.config.schema import DEFAULT_TOOL_TIMEOUT_SECONDS
from linkedin_mcp_server.core.exceptions import (
    AuthenticationError,
    LinkedInScraperException,
)
from linkedin_mcp_server.error_handler import raise_tool_error
from linkedin_mcp_server.tools import profile_edit as pe

logger = logging.getLogger(__name__)


def register_profile_edit_tools(
    mcp: FastMCP, *, tool_timeout: float = DEFAULT_TOOL_TIMEOUT_SECONDS
) -> None:
    """Register all Voyager profile-edit tools with the MCP server."""

    @mcp.tool(
        timeout=tool_timeout,
        title="Profile Edit Status",
        annotations={"readOnlyHint": True, "openWorldHint": True},
        tags={"profile", "voyager", "read-only"},
    )
    async def profile_edit_status(
        ctx: Context,
        public_id: Annotated[str | None, Field(description="LinkedIn public profile identifier (slug)")] = None,
    ) -> dict[str, Any]:
        """
        Check whether a live Voyager session is present for profile editing.

        Read-only; does not touch the browser. Reports session validity and
        the public profile id that writes would target.

        Args:
            public_id: LinkedIn public profile identifier (slug), defaults to own profile

        Returns:
            Dict with session status and the target public_id.
        """
        try:
            return pe.profile_edit_status(public_id=public_id)
        except Exception as e:
            raise_tool_error(e, "profile_edit_status")

    @mcp.tool(
        timeout=tool_timeout,
        title="Update Basic Profile",
        annotations={"writeAccess": True, "openWorldHint": True},
        tags={"profile", "voyager", "write"},
    )
    async def update_basics(
        ctx: Context,
        headline: Annotated[str | None, Field(description="New headline")] = None,
        summary: Annotated[str | None, Field(description="New summary text")] = None,
        first_name: Annotated[str | None, Field(description="New first name")] = None,
        last_name: Annotated[str | None, Field(description="New last name")] = None,
        location_name: Annotated[str | None, Field(description="New location name")] = None,
        profession: Annotated[str | None, Field(description="New industry/position label")] = None,
        public_id: Annotated[str | None, Field(description="LinkedIn public profile identifier (slug)")] = None,
        dry_run: Annotated[bool, Field(description="Build the request without sending it")] = False,
    ) -> dict[str, Any]:
        """
        Update top-level LinkedIn profile fields via Voyager patch.$set.

        Fields are wrapped in LinkedIn's official localized-text shape. Use
        update_profile_patch for raw $set payloads (dates, geo, custom fields).

        Args:
            headline: New headline
            summary: New summary text
            first_name: New first name
            last_name: New last name
            location_name: New location name
            profession: New industry/position label
            public_id: LinkedIn public profile identifier (slug), defaults to own profile
            dry_run: Build the request without sending it

        Returns:
            Dict with the server response (or the built request when dry_run).
        """
        try:
            return pe.update_basics(
                headline=headline,
                summary=summary,
                first_name=first_name,
                last_name=last_name,
                location_name=location_name,
                profession=profession,
                public_id=public_id,
                dry_run=dry_run,
            )
        except Exception as e:
            raise_tool_error(e, "update_basics")

    @mcp.tool(
        timeout=tool_timeout,
        title="Apply Raw Profile Patch",
        annotations={"writeAccess": True, "openWorldHint": True},
        tags={"profile", "voyager", "write"},
    )
    async def update_profile_patch(
        ctx: Context,
        patch: Annotated[dict[str, Any], Field(description='Raw patch object, e.g. {"$set": {"headline": {"localized": {"en_US": "..."}}}, "$delete": ["field"]}')],
        public_id: Annotated[str | None, Field(description="LinkedIn public profile identifier (slug)")] = None,
        dry_run: Annotated[bool, Field(description="Build the request without sending it")] = False,
    ) -> dict[str, Any]:
        """
        Apply a raw patch.$set/$delete to top-level profile fields.

        Passthrough for pre-shaped payloads; the client only adds transport
        and auth, it never fabricates field shapes.

        Args:
            patch: Raw patch object ($set and/or $delete keys)
            public_id: LinkedIn public profile identifier (slug), defaults to own profile
            dry_run: Build the request without sending it

        Returns:
            Dict with the server response (or the built request when dry_run).
        """
        try:
            return pe.update_profile_patch(patch=patch, public_id=public_id, dry_run=dry_run)
        except Exception as e:
            raise_tool_error(e, "update_profile_patch")

    @mcp.tool(
        timeout=tool_timeout,
        title="Add Profile Record",
        annotations={"writeAccess": True, "openWorldHint": True},
        tags={"profile", "voyager", "write"},
    )
    async def add_profile_record(
        ctx: Context,
        section: Annotated[str, Field(description="Section: positions, educations, skills, certifications, publications, volunteering-experiences")],
        fields: Annotated[dict[str, Any], Field(description="Record fields as the official API expects (localized/rawText/date shapes)")],
        x_linkedin_id: Annotated[str | None, Field(description="Entity id for the create (x-linkedin-id header)")] = None,
        public_id: Annotated[str | None, Field(description="LinkedIn public profile identifier (slug)")] = None,
        dry_run: Annotated[bool, Field(description="Build the request without sending it")] = False,
    ) -> dict[str, Any]:
        """
        Create one record in a profile section (positions, educations, skills,
        certifications, publications, volunteering-experiences).

        Args:
            section: Section to add the record to
            fields: Record fields in the official API shape
            x_linkedin_id: Entity id for the create (x-linkedin-id header)
            public_id: LinkedIn public profile identifier (slug), defaults to own profile
            dry_run: Build the request without sending it

        Returns:
            Dict with the server response (or the built request when dry_run).
        """
        try:
            return pe.add_profile_record(
                section=section,
                fields=fields,
                x_linkedin_id=x_linkedin_id,
                public_id=public_id,
                dry_run=dry_run,
            )
        except Exception as e:
            raise_tool_error(e, "add_profile_record")

    @mcp.tool(
        timeout=tool_timeout,
        title="Update Profile Record",
        annotations={"writeAccess": True, "openWorldHint": True},
        tags={"profile", "voyager", "write"},
    )
    async def update_profile_record(
        ctx: Context,
        section: Annotated[str, Field(description="Section: positions, educations, skills, certifications, publications, volunteering-experiences")],
        record_id: Annotated[str, Field(description="Record id within the section")],
        patch: Annotated[dict[str, Any], Field(description='Raw patch object: {"$set": {...}, "$delete": [...]}')],
        public_id: Annotated[str | None, Field(description="LinkedIn public profile identifier (slug)")] = None,
        dry_run: Annotated[bool, Field(description="Build the request without sending it")] = False,
    ) -> dict[str, Any]:
        """
        Partial-update one section record (POST {section}/{id} patch.$set).

        Args:
            section: Section the record belongs to
            record_id: Record id within the section
            patch: Raw patch object ($set and/or $delete keys)
            public_id: LinkedIn public profile identifier (slug), defaults to own profile
            dry_run: Build the request without sending it

        Returns:
            Dict with the server response (or the built request when dry_run).
        """
        try:
            return pe.update_profile_record(
                section=section,
                record_id=record_id,
                patch=patch,
                public_id=public_id,
                dry_run=dry_run,
            )
        except Exception as e:
            raise_tool_error(e, "update_profile_record")

    @mcp.tool(
        timeout=tool_timeout,
        title="Delete Profile Record",
        annotations={"writeAccess": True, "openWorldHint": True},
        tags={"profile", "voyager", "write"},
    )
    async def delete_profile_record(
        ctx: Context,
        section: Annotated[str, Field(description="Section: positions, educations, skills, certifications, publications, volunteering-experiences")],
        record_id: Annotated[str, Field(description="Record id within the section")],
        public_id: Annotated[str | None, Field(description="LinkedIn public profile identifier (slug)")] = None,
        dry_run: Annotated[bool, Field(description="Build the request without sending it")] = False,
    ) -> dict[str, Any]:
        """
        Delete one section record (DELETE {section}/{id}).

        Args:
            section: Section the record belongs to
            record_id: Record id within the section
            public_id: LinkedIn public profile identifier (slug), defaults to own profile
            dry_run: Build the request without sending it

        Returns:
            Dict with the server response (or the built request when dry_run).
        """
        try:
            return pe.delete_profile_record(
                section=section,
                record_id=record_id,
                public_id=public_id,
                dry_run=dry_run,
            )
        except Exception as e:
            raise_tool_error(e, "delete_profile_record")
