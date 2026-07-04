import argparse
import logging
import os
import subprocess
from os import geteuid
from pathlib import Path

import uvicorn
from playwright._impl._driver import compute_driver_executable, get_driver_env

from .mcp_server import mcp

logger = logging.getLogger(__name__)


def chromium_installed() -> bool:
    """Return True if a Playwright chromium build is already on disk."""
    cache = Path(os.getenv("PLAYWRIGHT_BROWSERS_PATH") or Path.home() / ".cache" / "ms-playwright")
    if not cache.exists():
        return False
    try:
        return any(p.name.startswith("chromium") for p in cache.iterdir())
    except OSError:
        return False


def install_playwright_drivers() -> None:
    try:
        driver_executable, driver_cli = compute_driver_executable()
        playwright_command = [driver_executable, driver_cli, "install"]
        if geteuid() == 0:
            playwright_command.extend(["--with-deps", "chromium"])
        completed_process = subprocess.run(
            playwright_command,
            env=get_driver_env(),
            check=True,
        )
        if completed_process.returncode == 0:
            print("Playwright drivers installed.")
            return
    except Exception as e:
        logger.warning("Playwright driver install failed: %s", e)
    print("Failed to install Playwright drivers.")


def cli() -> None:
    parser = argparse.ArgumentParser(description="Run Torrent Search Server.")
    parser.add_argument(
        "--mode",
        type=str,
        choices=["stdio", "sse", "streamable-http", "fastapi"],
        default="stdio",
        help="Mode to run the server in. Default: stdio.",
    )
    parser.add_argument("--host", type=str, default="0.0.0.0", help="Host to bind the server to.")
    parser.add_argument(
        "--port",
        type=int,
        default=8000,
        help="Port to bind the server to. Default: 8000.",
    )
    parser.add_argument(
        "--reload",
        action="store_true",
        help="Enable auto-reload for development.",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=1,
        help="Number of worker processes to use for the FastAPI server.",
    )
    parser.add_argument(
        "--force-install-browsers",
        action="store_true",
        help="Force a Playwright chromium install even if a cached build is present.",
    )

    args = parser.parse_args()

    if args.force_install_browsers or not chromium_installed():
        install_playwright_drivers()

    if args.mode == "fastapi":
        print(f"Starting FastAPI server on {args.host}:{args.port}")
        uvicorn.run(
            "torrent_search.fastapi_server:app",
            host=args.host,
            port=args.port,
            reload=args.reload,
            workers=args.workers,
        )
    else:
        print(f"Starting MCP server on {args.host}:{args.port}")
        mcp.run(
            transport=args.mode,
            **({} if args.mode == "stdio" else {"host": args.host, "port": args.port}),
        )


if __name__ == "__main__":
    cli()
