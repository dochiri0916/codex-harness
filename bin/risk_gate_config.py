"""Resolve the machine-local Risk Gate installation."""

from __future__ import annotations

import json
import os
from pathlib import Path


class ConfigurationError(ValueError):
    """Raised when a configured Risk Gate home is invalid."""


def _validated_home(value: object, source: str) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise ConfigurationError(f"{source} must be a non-empty absolute path.")
    home = Path(value).expanduser()
    if not home.is_absolute():
        raise ConfigurationError(f"{source} must be an absolute path.")
    wrapper = home / "gradlew"
    if not wrapper.is_file() or not os.access(wrapper, os.X_OK):
        raise ConfigurationError(f"{source} does not contain an executable gradlew.")
    return home


def resolve_risk_gate_home() -> Path | None:
    """Return a validated home, or None when no source is configured."""
    environment_value = os.environ.get("RISK_GATE_HOME")
    if environment_value:
        return _validated_home(environment_value, "RISK_GATE_HOME")

    config_path = Path.home() / ".config" / "codex-harness" / "config.json"
    if not config_path.exists():
        return None
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ConfigurationError(f"Could not read {config_path}: {type(exc).__name__}.") from exc
    if not isinstance(config, dict) or "riskGateHome" not in config:
        raise ConfigurationError(f"{config_path} must contain a riskGateHome path.")
    return _validated_home(config["riskGateHome"], "riskGateHome")
