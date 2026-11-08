"""Load and validate the bot's configuration.

Two sources, kept apart on purpose:

- ``config.toml``: tunables (mode, thresholds, schedules). Read only from that file, so a stray
  environment variable can never change how the bot trades. Unknown keys are rejected so typos
  fail at startup instead of being silently ignored.
- ``.env`` plus real environment variables: secrets (API keys and tokens). A real environment
  variable wins over ``.env``. Empty values count as unset. Values are ``SecretStr`` so they
  never show up in reprs or logs.
"""

import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, SecretStr, ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_CONFIG_PATH = Path("config.toml")
DEFAULT_ENV_PATH = Path(".env")
EXAMPLE_CONFIG_NAME = "config.example.toml"

Mode = Literal["paper", "live"]


class ConfigError(Exception):
    """The configuration is missing or invalid. The message is meant for the user."""


class Settings(BaseModel):
    """Tunables from ``config.toml``."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    mode: Mode = "paper"


class Secrets(BaseSettings):
    """Secrets from the environment or ``.env``. All optional until a feature needs them."""

    model_config = SettingsConfigDict(
        env_file_encoding="utf-8",
        env_ignore_empty=True,
        extra="ignore",
        frozen=True,
    )

    binance_api_key: SecretStr | None = None
    binance_api_secret: SecretStr | None = None
    notion_token: SecretStr | None = None
    notion_database_id: SecretStr | None = None
    coingecko_api_key: SecretStr | None = None


@dataclass(frozen=True)
class AppConfig:
    """Everything the app needs to start: tunables, secrets, and where they came from."""

    settings: Settings
    secrets: Secrets
    config_path: Path


def load_config(
    config_path: Path = DEFAULT_CONFIG_PATH, env_path: Path = DEFAULT_ENV_PATH
) -> AppConfig:
    """Load ``config_path`` and secrets from ``env_path``; raise ``ConfigError`` if invalid."""
    raw = _read_toml(config_path)
    try:
        settings = Settings.model_validate(raw)
    except ValidationError as exc:
        raise ConfigError(_format_errors(config_path, exc)) from None
    try:
        secrets = Secrets(_env_file=env_path)
    except ValidationError as exc:
        raise ConfigError(_format_errors(env_path, exc)) from None
    return AppConfig(settings=settings, secrets=secrets, config_path=config_path)


def load_config_or_exit(
    config_path: Path = DEFAULT_CONFIG_PATH, env_path: Path = DEFAULT_ENV_PATH
) -> AppConfig:
    """Like ``load_config``, but exit with the error message (code 1, no traceback)."""
    try:
        return load_config(config_path, env_path)
    except ConfigError as exc:
        raise SystemExit(f"Config error: {exc}") from None


def _read_toml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise ConfigError(
            f"{path} not found. Copy {EXAMPLE_CONFIG_NAME} to {path.name} and edit it, "
            "or run from the project folder."
        )
    try:
        with path.open("rb") as file:
            return tomllib.load(file)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path}: invalid TOML: {exc}") from None
    except OSError as exc:
        raise ConfigError(f"{path}: cannot read file: {exc.strerror}") from None


def _format_errors(path: Path, exc: ValidationError) -> str:
    lines = []
    for error in exc.errors():
        location = ".".join(str(part) for part in error["loc"]) or "(top level)"
        if error["type"] == "extra_forbidden":
            lines.append(f"{path}: {location}: unknown setting (check for a typo)")
        else:
            lines.append(f"{path}: {location}: {error['msg']} (got {error['input']!r})")
    return "\n".join(lines)
