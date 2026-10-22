"""Allow `python -m sahakaram` as well as the installed CLI."""

from sahakaram.cli import app

if __name__ == "__main__":
    app()
