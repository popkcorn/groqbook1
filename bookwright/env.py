"""Read a local `.env` file without adding a dependency.

The original app shipped an `example.env` but nothing ever loaded it, so the
only way to supply a key was to export it in the shell first. A twelve-line
parser removes that papercut and keeps the dependency list honest.
"""

from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = ROOT / ".env"

_TRUE = {"1", "true", "yes", "on"}


def load_env(path: Path | str | None = None) -> dict[str, str]:
    """Load KEY=VALUE pairs into os.environ.

    Real environment variables always win, so `export GROQ_API_KEY=...` in a
    shell overrides the file. Returns the values that were applied.
    """
    env_path = Path(path) if path else ENV_FILE
    if not env_path.is_file():
        return {}

    applied: dict[str, str] = {}
    try:
        text = env_path.read_text(encoding="utf-8")
    except OSError:
        return {}

    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.lower().startswith("export "):
            line = line[7:].lstrip()
        key, sep, value = line.partition("=")
        if not sep:
            continue
        key = key.strip()
        if not key or not key.replace("_", "").isalnum():
            continue
        value = value.strip()
        # Strip matching quotes; keep everything else verbatim.
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        # Allow trailing "  # comment" on unquoted values.
        elif " #" in value:
            value = value.split(" #", 1)[0].rstrip()
        if key not in os.environ:
            os.environ[key] = value
            applied[key] = value
    return applied


def is_true(value: str | None) -> bool:
    return (value or "").strip().lower() in _TRUE
