"""Middleware for Intervals.icu MCP server.

This module provides middleware components that run before tool execution.
"""

from collections.abc import Callable
from typing import Any

from fastmcp.exceptions import ToolError
from fastmcp.server.middleware import Middleware, MiddlewareContext

from .auth import load_config, validate_credentials

PUBLIC_READ_ONLY_PREFIXES = ("icu_get_", "icu_list_", "icu_search_", "icu_download_")


class PublicReadOnlyMiddleware(Middleware):
    """Deny every mutating tool call when the MCP is exposed over HTTP.

    Public HTTP access deliberately permits data retrieval only. Local STDIO
    clients retain the existing configuration and tool surface.
    """

    async def on_call_tool(self, context: MiddlewareContext, call_next: Callable[..., Any]):
        from fastmcp.server.context import _current_transport

        if _current_transport.get() != "stdio" and not context.message.name.startswith(
            PUBLIC_READ_ONLY_PREFIXES
        ):
            raise ToolError("Public HTTP MCP permits read-only tools only.")
        return await call_next(context)


class ConfigMiddleware(Middleware):
    """Middleware that loads and validates Intervals.icu configuration for all tool calls.

    This middleware:
    1. Loads the ICU config from environment variables
    2. Validates that credentials are properly configured
    3. Injects the config into the context state for tools to access via ctx.get_state("config")
    4. Raises ToolError if authentication is not configured
    """

    async def on_call_tool(self, context: MiddlewareContext, call_next: Callable[..., Any]):
        """Load and validate config before every tool call."""
        # Load configuration from environment
        config = load_config()

        # Validate credentials are properly configured
        if not validate_credentials(config):
            raise ToolError(
                "Intervals.icu credentials not configured. "
                "Please run 'icu-mcp-auth' to set up authentication."
            )

        # Inject config into context state for tools to access
        if context.fastmcp_context:
            await context.fastmcp_context.set_state("config", config, serializable=False)

        # Continue to the tool execution
        return await call_next(context)
