"""Logging setup: console + rotating file, level from config, UTC timestamps, safe to repeat."""

import logging
import re
from collections.abc import Iterator
from logging.handlers import RotatingFileHandler
from pathlib import Path

import pytest

from ledgermind.config import LoggingSettings
from ledgermind.logging_setup import LOG_FILE_NAME, setup_logging

LINE_PATTERN = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}\+00:00 (?P<level>[A-Z]+) +(?P<name>\S+): "
    r"(?P<message>.*)$"
)


@pytest.fixture(autouse=True)
def _restore_root_logger() -> Iterator[None]:
    """Undo whatever setup_logging did to the root logger, and close its files."""
    root = logging.getLogger()
    handlers_before = list(root.handlers)
    level_before = root.level
    yield
    for handler in list(root.handlers):
        if handler not in handlers_before:
            root.removeHandler(handler)
            handler.close()
    root.setLevel(level_before)


def read_log(log_dir: Path) -> str:
    for handler in logging.getLogger().handlers:
        handler.flush()
    return (log_dir / LOG_FILE_NAME).read_text(encoding="utf-8")


# --- where it writes -------------------------------------------------------------------


def test_creates_missing_log_dir_and_returns_log_file(tmp_path: Path) -> None:
    log_dir = tmp_path / "nested" / "logs"

    log_file = setup_logging(LoggingSettings(), log_dir)

    assert log_file == log_dir / "ledgermind.log"
    assert log_file.is_file()


def test_writes_to_file(tmp_path: Path) -> None:
    setup_logging(LoggingSettings(), tmp_path)

    logging.getLogger("ledgermind.test").info("hello file")

    assert "hello file" in read_log(tmp_path)


def test_writes_to_console_on_stderr(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    setup_logging(LoggingSettings(), tmp_path)

    logging.getLogger("ledgermind.test").info("hello console")

    captured = capsys.readouterr()
    assert "hello console" in captured.err
    assert "hello console" not in captured.out


# --- console only ----------------------------------------------------------------------


def test_console_only_writes_no_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)  # so a stray default logs/ folder would show up here

    result = setup_logging(LoggingSettings(), None)
    logging.getLogger("ledgermind.test").info("console only")

    assert result is None
    assert "console only" in capsys.readouterr().err
    assert list(tmp_path.iterdir()) == []
    assert not any(
        isinstance(handler, RotatingFileHandler) for handler in logging.getLogger().handlers
    )


def test_console_only_after_full_setup_drops_the_file(tmp_path: Path) -> None:
    setup_logging(LoggingSettings(), tmp_path)
    setup_logging(LoggingSettings(), None)

    logging.getLogger("ledgermind.test").info("not in the file")

    assert "not in the file" not in read_log(tmp_path)


def test_console_only_uses_level_from_config(capsys: pytest.CaptureFixture[str]) -> None:
    setup_logging(LoggingSettings(level="WARNING"), None)
    log = logging.getLogger("ledgermind.test")

    log.info("quiet info")
    log.warning("loud warning")

    err = capsys.readouterr().err
    assert "quiet info" not in err
    assert "loud warning" in err


# --- level from config -----------------------------------------------------------------


def test_level_filters_file_and_console(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    setup_logging(LoggingSettings(level="WARNING"), tmp_path)
    log = logging.getLogger("ledgermind.test")

    log.info("quiet info")
    log.warning("loud warning")

    text = read_log(tmp_path)
    err = capsys.readouterr().err
    assert "quiet info" not in text
    assert "quiet info" not in err
    assert "loud warning" in text
    assert "loud warning" in err


def test_default_level_is_info(tmp_path: Path) -> None:
    setup_logging(LoggingSettings(), tmp_path)
    log = logging.getLogger("ledgermind.test")

    log.debug("hidden debug")
    log.info("shown info")

    text = read_log(tmp_path)
    assert "hidden debug" not in text
    assert "shown info" in text


def test_debug_level_lets_debug_through(tmp_path: Path) -> None:
    setup_logging(LoggingSettings(level="DEBUG"), tmp_path)

    logging.getLogger("ledgermind.test").debug("shown debug")

    assert "shown debug" in read_log(tmp_path)


# --- rotation --------------------------------------------------------------------------


def test_file_rotates_at_configured_size(tmp_path: Path) -> None:
    setup_logging(LoggingSettings(max_file_bytes=2048, backup_count=2), tmp_path)
    log = logging.getLogger("ledgermind.test")

    for i in range(200):
        log.info("line %03d %s", i, "x" * 40)
    read_log(tmp_path)  # flush

    assert (tmp_path / "ledgermind.log").stat().st_size <= 2048
    assert (tmp_path / "ledgermind.log.1").is_file()
    assert (tmp_path / "ledgermind.log.2").is_file()
    assert not (tmp_path / "ledgermind.log.3").exists()
    assert "line 199" in read_log(tmp_path)


# --- format ----------------------------------------------------------------------------


def test_line_format_has_utc_timestamp_level_and_logger(tmp_path: Path) -> None:
    setup_logging(LoggingSettings(), tmp_path)

    logging.getLogger("ledgermind.test").warning("formatted")

    lines = read_log(tmp_path).splitlines()
    assert len(lines) == 1
    match = LINE_PATTERN.match(lines[0])
    assert match is not None, lines[0]
    assert match["level"] == "WARNING"
    assert match["name"] == "ledgermind.test"
    assert match["message"] == "formatted"


# --- safe to call again ----------------------------------------------------------------


def test_calling_twice_does_not_duplicate_lines(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    setup_logging(LoggingSettings(), tmp_path)
    setup_logging(LoggingSettings(), tmp_path)

    logging.getLogger("ledgermind.test").info("only once")

    assert read_log(tmp_path).count("only once") == 1
    assert capsys.readouterr().err.count("only once") == 1


def test_calling_again_applies_new_level(tmp_path: Path) -> None:
    setup_logging(LoggingSettings(level="WARNING"), tmp_path)
    setup_logging(LoggingSettings(level="DEBUG"), tmp_path)

    logging.getLogger("ledgermind.test").debug("now visible")

    assert "now visible" in read_log(tmp_path)


def test_leaves_other_handlers_alone(tmp_path: Path) -> None:
    root = logging.getLogger()
    other = logging.NullHandler()
    root.addHandler(other)
    try:
        setup_logging(LoggingSettings(), tmp_path)
        setup_logging(LoggingSettings(), tmp_path)

        assert other in root.handlers
    finally:
        root.removeHandler(other)
