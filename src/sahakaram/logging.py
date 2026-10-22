"""Application logging must never write into the MCP stdout transport."""

import logging
import sys


def configure_logging(level: str) -> None:
    """Install one stderr handler at the CLI boundary, not during module imports."""
    logging.basicConfig(
        level=level,
        stream=sys.stderr,
        format="%(levelname)s %(name)s: %(message)s",
        force=True,
    )
