import sys

# Force UTF-8 on stdio before the MCP library wraps the streams. Without this,
# Windows defaults stdin/stdout to the active code page (typically cp1252),
# which mangles non-ASCII bytes in the JSON-RPC payloads. See issue #135.
if hasattr(sys.stdin, "reconfigure"):
    sys.stdin.reconfigure(encoding="utf-8")
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from . import server
import asyncio

def main():
    """Main entry point for the package."""
    asyncio.run(server.main())

# Optionally expose other important items at package level
__all__ = ['main', 'server']