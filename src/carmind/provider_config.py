"""Shared, allowlisted local provider configuration; shell values take precedence."""

import os
from math import isfinite
from pathlib import Path

from carmind.manufacturer_knowledge import ROOT


LIVE_ENV_NAMES = frozenset({"TYPESAFE_API_KEY", "XAI_API_KEY", "XAI_MODEL", "TYPESAFE_DEFAULT_MODEL"})


def load_project_env(path=None):
    env_path = Path(path) if path is not None else ROOT / ".env"
    if not env_path.is_file():
        return
    for raw in env_path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        key, value = line.split("=", 1)
        key = key.strip()
        if key not in LIVE_ENV_NAMES:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        os.environ.setdefault(key, value)


def require_live_config(*, confirmed, max_calls_per_turn, timeout_seconds):
    """Validate explicit credit use before constructing either provider."""
    if not confirmed:
        raise ValueError("Live API use requires --confirm-live-api-use")
    if type(max_calls_per_turn) is not int or not 1 <= max_calls_per_turn <= 4:
        raise ValueError("--max-calls-per-turn must be between 1 and 4")
    if type(timeout_seconds) not in (int, float) or not isfinite(timeout_seconds) or not 0 < timeout_seconds <= 180:
        raise ValueError("--timeout-seconds must be between 0 and 180")
    load_project_env()
    missing = [name for name in ("TYPESAFE_API_KEY", "XAI_API_KEY", "XAI_MODEL")
               if not os.environ.get(name, "").strip()]
    if missing:
        raise ValueError("Missing live configuration: " + ", ".join(missing))
