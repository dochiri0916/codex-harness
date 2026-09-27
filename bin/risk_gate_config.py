"""Resolve the machine-local Risk Gate installation."""

from __future__ import annotations

import json
import getpass
import os
from pathlib import Path
import subprocess
import sys


class ConfigurationError(ValueError):
    """Raised when a configured Risk Gate home is invalid."""


def load_machine_config() -> dict[str, object]:
    config_path = Path.home() / ".config" / "codex-harness" / "config.json"
    if not config_path.exists():
        return {}
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ConfigurationError(f"Could not read {config_path}: {type(exc).__name__}.") from exc
    if not isinstance(config, dict):
        raise ConfigurationError(f"{config_path} must contain a JSON object.")
    validate_pr_checks(config)
    return config


def validate_pr_checks(config: dict[str, object]) -> list[str]:
    """Validate and return configured required GitHub check names."""
    if "prChecks" not in config:
        return []
    pr_checks = config["prChecks"]
    if not isinstance(pr_checks, dict):
        raise ConfigurationError("prChecks must be a JSON object.")
    required = pr_checks.get("required")
    if not isinstance(required, list) or not required:
        raise ConfigurationError("prChecks.required must be a non-empty array of check names.")
    if any(not isinstance(name, str) or not name.strip() for name in required):
        raise ConfigurationError("prChecks.required must contain non-empty check names.")
    normalized = [name.strip() for name in required]
    if len(set(normalized)) != len(normalized):
        raise ConfigurationError("prChecks.required must not contain duplicate check names.")
    return normalized


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

    config = load_machine_config()
    if "riskGateHome" not in config:
        if config:
            raise ConfigurationError("Machine config must contain a riskGateHome path.")
        return None
    return _validated_home(config["riskGateHome"], "riskGateHome")


def configure_jev_environment(environment: dict[str, str]) -> None:
    """Enable Jev for Risk Gate, resolving a missing key from macOS Keychain."""
    config = load_machine_config()
    jev = config.get("jev", {})
    if not isinstance(jev, dict):
        raise ConfigurationError("jev must be a JSON object.")
    enabled = jev.get("enabled", False)
    if not isinstance(enabled, bool):
        raise ConfigurationError("jev.enabled must be a boolean.")
    if not enabled:
        environment.pop("TYPESAFE_API_KEY", None)
        environment["TYPESAFE_JEV_ENABLED"] = "false"
        return
    environment["TYPESAFE_JEV_ENABLED"] = "true"
    if environment.get("TYPESAFE_API_KEY"):
        return
    service = jev.get("keychainService")
    if not isinstance(service, str) or not service.strip():
        raise ConfigurationError("Jev is enabled but no Keychain service is configured.")
    if sys.platform != "darwin":
        raise ConfigurationError("Jev API key is unavailable; configure TYPESAFE_API_KEY or a macOS Keychain entry.")
    try:
        result = subprocess.run(
            ["security", "find-generic-password", "-a", getpass.getuser(), "-s", service, "-w"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=True, text=True,
        )
        key = result.stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        key = ""
    if not key:
        raise ConfigurationError("Jev API key is unavailable; check the configured macOS Keychain entry.")
    environment["TYPESAFE_API_KEY"] = key
