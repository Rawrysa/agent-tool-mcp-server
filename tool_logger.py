"""
Local logger — no framework dependency.
All tools in the MCP server import from here.
"""
import logging
import os

_level = getattr(logging, os.getenv("LOG_LEVEL", "INFO").upper(), logging.INFO)

logging.basicConfig(
    level=_level,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
)

logger = logging.getLogger("mcp_server")
