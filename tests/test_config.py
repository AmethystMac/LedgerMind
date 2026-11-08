"""Config loading: defaults, validation, clear errors, and secrets from the environment."""

from pathlib import Path

import pytest

from ledgermind.config import (
    AppConfig,
    ConfigError,
    LoggingSettings,
    Secrets,
    Settings,
    load_config,
    load_config_or_exit,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
SECRET_ENV_VARS = (
    "BINANCE_API_KEY",
    "BINANCE_API_SECRET",
    "NOTION_TOKEN",
    "NOTION_DATABASE_ID",
    "COINGECKO_API_KEY",
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the developer's real environment out of every test."""
    for name in (*SECRET_ENV_VARS, "MODE"):
        monkeypatch.delenv(name, raising=False)


def write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


def load(tmp_path: Path, toml: str, env: str | None = None) -> AppConfig:
    config_path = write(tmp_path / "config.toml", toml)
    env_path = tmp_path / ".env"
    if env is not None:
        write(env_path, env)
    return load_config(config_path, env_path)


# --- defaults and valid values ---------------------------------------------------------


def test_empty_config_defaults_to_paper(tmp_path: Path) -> None:
    config = load(tmp_path, "")

    assert config.settings.mode == "paper"


@pytest.mark.parametrize("mode", ["paper", "live"])
def test_valid_modes_load(tmp_path: Path, mode: str) -> None:
    config = load(tmp_path, f'mode = "{mode}"\n')

    assert config.settings.mode == mode


def test_config_path_is_recorded(tmp_path: Path) -> None:
    config = load(tmp_path, "")

    assert config.config_path == tmp_path / "config.toml"


def test_mode_env_var_does_not_override_config_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MODE", "live")

    config = load(tmp_path, "")

    assert config.settings.mode == "paper"


# --- validation and clear errors -------------------------------------------------------


def test_invalid_mode_raises_clear_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError) as excinfo:
        load(tmp_path, 'mode = "yolo"\n')

    message = str(excinfo.value)
    assert "config.toml" in message
    assert "mode" in message
    assert "yolo" in message
    assert "paper" in message
    assert "live" in message


@pytest.mark.parametrize("mode", ["Paper", "LIVE", " paper", ""])
def test_mode_is_case_and_whitespace_exact(tmp_path: Path, mode: str) -> None:
    with pytest.raises(ConfigError):
        load(tmp_path, f'mode = "{mode}"\n')


def test_wrong_type_raises_clear_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError) as excinfo:
        load(tmp_path, "mode = 1\n")

    assert "mode" in str(excinfo.value)


def test_unknown_key_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ConfigError) as excinfo:
        load(tmp_path, 'mod = "live"\n')

    message = str(excinfo.value)
    assert "mod" in message
    assert "unknown" in message.lower()


def test_broken_toml_raises_clear_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError) as excinfo:
        load(tmp_path, 'mode = "paper\n')

    message = str(excinfo.value)
    assert "config.toml" in message
    assert "TOML" in message


def test_missing_config_file_is_fatal(tmp_path: Path) -> None:
    with pytest.raises(ConfigError) as excinfo:
        load_config(tmp_path / "config.toml", tmp_path / ".env")

    message = str(excinfo.value)
    assert "config.toml" in message
    assert "config.example.toml" in message


def test_load_or_exit_exits_with_message(tmp_path: Path) -> None:
    config_path = write(tmp_path / "config.toml", 'mode = "yolo"\n')

    with pytest.raises(SystemExit) as excinfo:
        load_config_or_exit(config_path, tmp_path / ".env")

    # SystemExit with a string argument prints it to stderr and exits with code 1.
    assert isinstance(excinfo.value.code, str)
    assert "yolo" in excinfo.value.code


def test_load_or_exit_returns_config_when_valid(tmp_path: Path) -> None:
    config_path = write(tmp_path / "config.toml", 'mode = "live"\n')

    config = load_config_or_exit(config_path, tmp_path / ".env")

    assert config.settings.mode == "live"


# --- logging section -------------------------------------------------------------------


def test_logging_defaults(tmp_path: Path) -> None:
    config = load(tmp_path, "")

    assert config.settings.logging == LoggingSettings()
    assert config.settings.logging.level == "INFO"
    assert config.settings.logging.max_file_bytes == 5_000_000
    assert config.settings.logging.backup_count == 5


def test_logging_section_loads(tmp_path: Path) -> None:
    config = load(
        tmp_path,
        '[logging]\nlevel = "DEBUG"\nmax_file_bytes = 1_000_000\nbackup_count = 3\n',
    )

    assert config.settings.logging.level == "DEBUG"
    assert config.settings.logging.max_file_bytes == 1_000_000
    assert config.settings.logging.backup_count == 3


@pytest.mark.parametrize("level", ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"])
def test_valid_log_levels_load(tmp_path: Path, level: str) -> None:
    config = load(tmp_path, f'[logging]\nlevel = "{level}"\n')

    assert config.settings.logging.level == level


def test_invalid_log_level_raises_clear_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError) as excinfo:
        load(tmp_path, '[logging]\nlevel = "LOUD"\n')

    message = str(excinfo.value)
    assert "logging.level" in message
    assert "LOUD" in message
    assert "INFO" in message


@pytest.mark.parametrize("level", ["info", "Warning", "WARN", ""])
def test_log_level_is_exact(tmp_path: Path, level: str) -> None:
    with pytest.raises(ConfigError):
        load(tmp_path, f'[logging]\nlevel = "{level}"\n')


@pytest.mark.parametrize("size", [0, -1, 5, 1023])
def test_too_small_max_file_bytes_is_rejected(tmp_path: Path, size: int) -> None:
    with pytest.raises(ConfigError) as excinfo:
        load(tmp_path, f"[logging]\nmax_file_bytes = {size}\n")

    assert "logging.max_file_bytes" in str(excinfo.value)


def test_smallest_max_file_bytes_is_accepted(tmp_path: Path) -> None:
    config = load(tmp_path, "[logging]\nmax_file_bytes = 1024\n")

    assert config.settings.logging.max_file_bytes == 1024


@pytest.mark.parametrize("count", [0, -1])
def test_backup_count_below_one_is_rejected(tmp_path: Path, count: int) -> None:
    # A count of 0 would silently disable rotation and let the file grow forever.
    with pytest.raises(ConfigError) as excinfo:
        load(tmp_path, f"[logging]\nbackup_count = {count}\n")

    assert "logging.backup_count" in str(excinfo.value)


def test_unknown_logging_key_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ConfigError) as excinfo:
        load(tmp_path, '[logging]\nlevl = "DEBUG"\n')

    message = str(excinfo.value)
    assert "logging.levl" in message
    assert "unknown" in message.lower()


# --- committed templates stay in sync with the code ------------------------------------


def test_example_config_loads_and_matches_defaults(tmp_path: Path) -> None:
    config = load_config(REPO_ROOT / "config.example.toml", tmp_path / ".env")

    assert config.settings == Settings()


def test_example_env_lists_exactly_the_known_secrets() -> None:
    lines = (REPO_ROOT / ".env.example").read_text(encoding="utf-8").splitlines()
    names = {
        line.split("=", 1)[0].strip()
        for line in lines
        if line.strip() and not line.lstrip().startswith("#")
    }

    assert names == {name.upper() for name in Secrets.model_fields}
    assert names == set(SECRET_ENV_VARS)


def test_example_env_has_no_values() -> None:
    lines = (REPO_ROOT / ".env.example").read_text(encoding="utf-8").splitlines()
    for line in lines:
        if line.strip() and not line.lstrip().startswith("#"):
            assert line.split("=", 1)[1].strip() == "", line


# --- secrets ---------------------------------------------------------------------------


def test_secrets_are_optional(tmp_path: Path) -> None:
    config = load(tmp_path, "")

    assert config.secrets.binance_api_key is None
    assert config.secrets.binance_api_secret is None
    assert config.secrets.notion_token is None
    assert config.secrets.notion_database_id is None
    assert config.secrets.coingecko_api_key is None


def test_secrets_are_read_from_env_file(tmp_path: Path) -> None:
    config = load(tmp_path, "", env="BINANCE_API_KEY=key-from-file\nNOTION_TOKEN=tok\n")

    assert config.secrets.binance_api_key is not None
    assert config.secrets.binance_api_key.get_secret_value() == "key-from-file"
    assert config.secrets.notion_token is not None
    assert config.secrets.notion_token.get_secret_value() == "tok"


def test_real_env_var_overrides_env_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BINANCE_API_KEY", "key-from-env")

    config = load(tmp_path, "", env="BINANCE_API_KEY=key-from-file\n")

    assert config.secrets.binance_api_key is not None
    assert config.secrets.binance_api_key.get_secret_value() == "key-from-env"


def test_empty_secret_counts_as_unset(tmp_path: Path) -> None:
    config = load(tmp_path, "", env="BINANCE_API_KEY=\nNOTION_TOKEN=\n")

    assert config.secrets.binance_api_key is None
    assert config.secrets.notion_token is None


def test_unrelated_env_file_entries_are_ignored(tmp_path: Path) -> None:
    config = load(tmp_path, "", env="SOMETHING_ELSE=1\n")

    assert config.secrets.binance_api_key is None


def test_secret_values_never_appear_in_repr(tmp_path: Path) -> None:
    config = load(tmp_path, "", env="BINANCE_API_SECRET=supersecret123\n")

    assert "supersecret123" not in repr(config)
    assert "supersecret123" not in str(config)
