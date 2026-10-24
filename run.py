#!/usr/bin/env python3
"""Standalone launcher for Bookwright.

    python run.py                 # http://127.0.0.1:8000, opens your browser
    python run.py --port 9000
    python run.py --reload        # auto-restart on code changes (development)
    python run.py --host 0.0.0.0  # reachable from other devices on your network

Works on Windows, macOS and Linux with nothing but Python 3.10+ installed.
"""

from __future__ import annotations

import argparse
import contextlib
import importlib.util
import os
import socket
import sys
import threading
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent
MIN_PYTHON = (3, 10)

# Binding to every interface is only ever done on explicit request (`--host`),
# and it is announced in the banner. Kept in one place so the intent is visible.
ANY_INTERFACE = "0.0.0.0"  # noqa: S104
LOOPBACK = "127.0.0.1"

REQUIRED = {
    "fastapi": "fastapi",
    "uvicorn": "uvicorn[standard]",
    "httpx": "httpx",
    "reportlab": "reportlab",
    "pydantic": "pydantic",
    "docx": "python-docx",
    "openpyxl": "openpyxl",
}


def fail(message: str) -> None:
    print(f"\n  ✖  {message}\n", file=sys.stderr)
    raise SystemExit(1)


def check_python() -> None:
    if sys.version_info < MIN_PYTHON:
        fail(
            f"Bookwright needs Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]} or newer; "
            f"this is {sys.version.split()[0]} ({sys.executable})."
        )


def check_dependencies(install: bool) -> None:
    missing = [pkg for name, pkg in REQUIRED.items() if importlib.util.find_spec(name) is None]
    if not missing:
        return

    requirements = ROOT / "requirements-bookwright.txt"
    print(f"\n  Missing packages: {', '.join(missing)}")
    print(f"  Install them with:\n\n    {Path(sys.executable).name} -m pip install -r \"{requirements}\"\n")
    print("  Or just use the start script, which creates a virtual environment for you:")
    print("    bash start-mac-linux.sh        (macOS/Linux)")
    print("    start-windows.bat              (Windows)\n")

    if not install:
        fail("dependencies are not installed (re-run with --install to attempt it automatically)")

    print("  Installing now…\n")
    import subprocess

    # Fixed argv built from sys.executable: no shell, no untrusted input.
    result = subprocess.run(  # noqa: S603 - fixed argv from sys.executable, no shell
        [sys.executable, "-m", "pip", "install", "-r", str(requirements)],
        check=False,
    )
    if result.returncode != 0:
        fail(
            "pip could not install the dependencies. If your Python is managed by the OS "
            "(Debian/Ubuntu), create a virtual environment first:\n\n"
            "    python3 -m venv .venv && source .venv/bin/activate\n"
            "    python -m pip install -r requirements-bookwright.txt\n"
        )
    for name in REQUIRED:
        importlib.invalidate_caches()
        if importlib.util.find_spec(name) is None:
            fail(f"{name} still cannot be imported after installing; check the pip output above")


def port_free(host: str, port: int) -> bool:
    probe_host = LOOPBACK if host in {ANY_INTERFACE, "::"} else host
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((probe_host, port))
        except OSError:
            return False
    return True


def open_when_ready(url: str, host: str, port: int, timeout: float = 20.0) -> None:
    import time
    import urllib.error
    import urllib.request

    probe_host = LOOPBACK if host in {ANY_INTERFACE, "::"} else host
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"http://{probe_host}:{port}/api/health", timeout=1):
                webbrowser.open(url)
                return
        except (urllib.error.URLError, ConnectionError, OSError):
            time.sleep(0.4)


def banner(mode: str, url: str, models: dict[str, str], env_file: Path) -> None:
    line = "─" * 62
    engine = (
        "Groq API — live generation"
        if mode == "live"
        else "demo mode — fixture prose, no key needed"
    )
    print(f"\n  {line}")
    print("   Bookwright")
    print(f"   {engine}")
    for role, model in models.items():
        print(f"   {role:<10} {model}")
    if mode == "demo":
        print(f"\n   Add a key to write real books:  {env_file}")
        print("   (copy example.env to .env and set GROQ_API_KEY)")
    print(f"\n   Open  {url}")
    print("   Stop with Ctrl+C")
    print(f"  {line}\n")


def main() -> None:
    # Line-buffer stdout so the banner appears even when output is redirected
    # to a file or a service log (Windows/CI do not use a TTY).
    with contextlib.suppress(AttributeError, ValueError):
        sys.stdout.reconfigure(line_buffering=True)

    parser = argparse.ArgumentParser(description="Run Bookwright locally.")
    parser.add_argument("--host", default=LOOPBACK, help="bind address (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8000, help="port (default: 8000)")
    parser.add_argument("--reload", action="store_true", help="restart when files change")
    parser.add_argument("--no-browser", action="store_true", help="do not open a browser")
    parser.add_argument(
        "--install", action="store_true", help="install missing dependencies automatically"
    )
    args = parser.parse_args()

    check_python()
    check_dependencies(install=args.install)

    # Make `server` importable no matter which directory the launcher was run from.
    os.chdir(ROOT)
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))

    import uvicorn  # imported after the dependency check

    from bookwright import config
    from bookwright.env import ENV_FILE

    if not port_free(args.host, args.port):
        fail(
            f"port {args.port} is already in use — Bookwright may already be running, "
            f"or another app has it. Try:  python run.py --port {args.port + 1}"
        )

    mode = "live" if config.has_server_key() else "demo"
    exposed = args.host in {ANY_INTERFACE, "::"}
    visible_host = "localhost" if args.host in {ANY_INTERFACE, "::", LOOPBACK} else args.host
    url = f"http://{visible_host}:{args.port}"
    if exposed:
        print("  Note: bound to all interfaces — other devices on your network can reach this.")

    banner(mode, url, {
        "architect": config.ARCHITECT_MODEL,
        "writer": config.WRITER_MODEL,
        "editor": config.EDITOR_MODEL,
    }, ENV_FILE)

    if not args.no_browser and not args.reload:
        threading.Thread(
            target=open_when_ready, args=(url, args.host, args.port), daemon=True
        ).start()

    try:
        uvicorn.run(
            "server:app",
            host=args.host,
            port=args.port,
            reload=args.reload,
            reload_dirs=[str(ROOT)] if args.reload else None,
            log_level="info",
        )
    except KeyboardInterrupt:
        print("\n  Stopped.\n")


if __name__ == "__main__":
    main()
