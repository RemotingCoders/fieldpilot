"""Loading .env, without a dependency.

This file exists because of a real bug. Every module read its configuration
with `os.getenv`, the README told people to put their settings in `.env`, and
nothing ever loaded that file. The failure was silent in the worst way: the
geocoder found no API key, fell back to its offline stand-in, printed
coordinates, and looked like it was working.

Anything already set in the real environment wins, so `FIELDPILOT_MODEL=x
fieldpilot ...` still overrides the file.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

# Where the last load_env() call found its file, and which keys came from it.
# Reported by describe(), so "maps key=set" can say whether the key came from
# the file or from the shell — the two fail in different ways.
LOADED_FROM: Path | None = None
LOADED_KEYS: set[str] = set()


def load_env(path: str | Path = ".env", override: bool = False) -> dict[str, str]:
    """Read a .env file into the process environment.

    Returns what was loaded, so a caller can report it. Never raises: a missing
    or malformed .env is a configuration problem to report, not a crash.
    """
    global LOADED_FROM, LOADED_KEYS

    loaded: dict[str, str] = {}
    env_path = Path(path)

    if not env_path.exists():
        # Also look beside the package, so running from a subdirectory works.
        for parent in Path.cwd().resolve().parents:
            candidate = parent / ".env"
            if candidate.exists():
                env_path = candidate
                break
        else:
            return loaded

    try:
        lines = env_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return loaded

    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if not key:
            continue
        if override or key not in os.environ:
            os.environ[key] = value
            loaded[key] = value

    LOADED_FROM = env_path
    LOADED_KEYS = set(loaded)
    return loaded


def _origin(key: str) -> str:
    if key in LOADED_KEYS:
        return ".env"
    return "environment"


def describe() -> str:
    """How this process is configured, for checking before a run.

    Secrets are reported as present or absent, never printed.
    """
    project = os.getenv("GOOGLE_CLOUD_PROJECT") or "unset"
    location = os.getenv("GOOGLE_CLOUD_LOCATION") or "unset"
    model = os.getenv("FIELDPILOT_MODEL") or "default"

    if os.getenv("FIELDPILOT_MAPS_API_KEY"):
        maps = f"set (from {_origin('FIELDPILOT_MAPS_API_KEY')})"
    else:
        maps = "unset — geocoding will use the offline stand-in"

    where = str(LOADED_FROM) if LOADED_FROM is not None else "none found"
    return (
        f".env: {where}\n"
        f"project={project}  location={location}  model={model}\n"
        f"maps key={maps}"
    )


# The genai SDK logs a recommendation on every generate_content call telling us
# to use AsyncChat.send_message instead. It does not apply here — ADK owns the
# call and there is no chat session to move to — and it prints above every
# result, which on a recorded demo reads as an error. Only this one message is
# dropped; anything else that logger has to say still comes through.
_AFC_NOISE = "automatic function calling"


class _DropAFCNotice(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return _AFC_NOISE not in record.getMessage().lower()


def quiet_known_noise() -> None:
    """Silence third-party log lines that are advisory and not about us.

    Deliberately narrow. A blanket `logging.disable` here would also hide the
    quota and authentication warnings that are the only warning anyone gets
    before a run starts costing money or silently degrading.
    """
    for name in ("google_genai.models", "google.genai.models"):
        logging.getLogger(name).addFilter(_DropAFCNotice())
