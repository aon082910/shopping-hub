"""Shared path constant, kept separate to avoid a circular import.

``pipeline.freight`` needs the project root; importing ``sourcehub.config`` would
pull settings (and pydantic) into a module that only wants a directory.
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent


def config_path(filename: str):
    """Mirror of sourcehub.config.config_path without importing settings.

    Kept duplicated deliberately: importing sourcehub.config here would create a
    cycle (config -> pipeline -> freight -> config) for the sake of two lines.
    """
    import os

    raw = os.environ.get("SOURCEHUB_CONFIG_DIR", "").strip()
    if raw:
        candidate = Path(raw) / filename
        if candidate.exists():
            return candidate
    return ROOT / filename
