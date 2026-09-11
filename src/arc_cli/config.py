"""Side-effect-free Arc configuration loading and precedence rules."""

from __future__ import annotations

import os
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

DEFAULT_BASE_URL = "https://api.openai.com/v1"


@dataclass(frozen=True)
class ProviderConfig:
    """Non-secret model provider settings."""

    model: str
    base_url: str


@dataclass(frozen=True)
class ProviderSecrets:
    """Provider credentials kept separate and omitted from representations."""

    api_key: str = field(repr=False)


@dataclass(frozen=True)
class ArcConfig:
    """Resolved configuration with an explicit secret boundary."""

    provider: ProviderConfig
    secrets: ProviderSecrets


def _read_toml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    with path.open("rb") as handle:
        data = tomllib.load(handle)
    secret_keys = {"api_key", "key", "secret", "token", "password", "credentials"}

    def contains_secret(value: object) -> bool:
        return isinstance(value, dict) and any(
            str(key).lower() in secret_keys or contains_secret(child) for key, child in value.items()
        )

    if contains_secret(data):
        raise ValueError(f"Secrets are not allowed in {path}; use the ARC_API_KEY environment variable")
    provider = data.get("provider", data)
    if not isinstance(provider, dict):
        raise ValueError(f"Invalid provider config in {path}")
    unknown = set(provider) - {"model", "base_url"}
    if unknown:
        raise ValueError(f"Unknown provider settings in {path}: {', '.join(sorted(unknown))}")
    result: dict[str, Any] = {}
    for key in ("model", "base_url"):
        if key in provider:
            value = provider[key]
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{key} in {path} must be a non-empty string")
            result[key] = value
    return result


def _provider_values(values: Mapping[str, str | None]) -> dict[str, str]:
    """Resolve Arc's namespaced provider settings from one environment layer."""

    result: dict[str, str] = {}
    names = {
        "model": "ARC_MODEL",
        "base_url": "ARC_BASE_URL",
        "api_key": "ARC_API_KEY",
    }
    for target, name in names.items():
        value = values.get(name)
        if value:
            result[target] = value
    return result


def load_config(
    cwd: Path,
    *,
    cli_model: str | None = None,
    cli_base_url: str | None = None,
    environ: Mapping[str, str] | None = None,
    user_config_path: Path | None = None,
) -> ArcConfig:
    """Resolve configuration without mutating ``os.environ``.

    Precedence, highest first: CLI, ``.arc/config.toml``, user config,
    process environment, defaults. Only namespaced ``ARC_*`` provider
    environment variables are recognized. Arc never reads a project ``.env``.
    """

    environment = os.environ if environ is None else environ
    if user_config_path is None:
        config_home = environment.get("XDG_CONFIG_HOME")
        root = Path(config_home) if config_home else Path.home() / ".config"
        user_config_path = root / "arc" / "config.toml"

    resolved: dict[str, str] = {"base_url": DEFAULT_BASE_URL, "api_key": ""}
    resolved.update(_provider_values(environment))
    resolved.update(_read_toml(user_config_path))
    resolved.update(_read_toml(cwd / ".arc" / "config.toml"))
    if cli_model:
        resolved["model"] = cli_model
    if cli_base_url:
        resolved["base_url"] = cli_base_url
    return ArcConfig(
        provider=ProviderConfig(
            model=resolved.get("model", ""),
            base_url=resolved["base_url"],
        ),
        secrets=ProviderSecrets(api_key=resolved["api_key"]),
    )
