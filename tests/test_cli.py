"""The command line: ``python -m ledgermind run | status``."""

import logging
import signal
import sqlite3
import subprocess
import sys
import time
from collections.abc import Iterator
from logging.handlers import RotatingFileHandler
from pathlib import Path

import pytest

from ledgermind import __version__, app
from ledgermind.cli import main
from ledgermind.db.migrate import open_database
from ledgermind.db.repo import finish_job_run, start_job_run
from ledgermind.jobs import utc_now

TIMEOUT = 10.0
POSIX_ONLY = pytest.mark.skipif(sys.platform == "win32", reason="POSIX signals")


@pytest.fixture(autouse=True)
def _restore_root_logger() -> Iterator[None]:
    """The CLI sets up logging; undo it and close its files after each test."""
    root = logging.getLogger()
    handlers_before = list(root.handlers)
    level_before = root.level
    yield
    for handler in list(root.handlers):
        if handler not in handlers_before:
            root.removeHandler(handler)
            handler.close()
    root.setLevel(level_before)


@pytest.fixture
def paths(tmp_path: Path) -> dict[str, Path]:
    config_path = tmp_path / "config.toml"
    config_path.write_text('mode = "paper"\n', encoding="utf-8")
    return {
        "config_path": config_path,
        "env_path": tmp_path / ".env",
        "db_path": tmp_path / "data" / "ledgermind.db",
        "log_dir": tmp_path / "logs",
    }


# --- choosing a command ----------------------------------------------------------------


def test_run_starts_the_app_with_the_given_paths(
    paths: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[Path, ...]] = []
    monkeypatch.setattr(app, "main", lambda *args: calls.append(args))

    code = main(["run"], **paths)

    assert code == 0
    assert calls == [(paths["config_path"], paths["env_path"], paths["db_path"], paths["log_dir"])]


def test_run_passes_on_the_apps_exit(
    paths: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    def failing(*_args: Path) -> None:
        raise SystemExit(1)

    monkeypatch.setattr(app, "main", failing)

    with pytest.raises(SystemExit) as excinfo:
        main(["run"], **paths)

    assert excinfo.value.code == 1


def test_no_command_prints_help_and_exits_2(capsys: pytest.CaptureFixture[str]) -> None:
    assert main([]) == 2

    err = capsys.readouterr().err
    assert "run" in err
    assert "status" in err


def test_unknown_command_exits_2(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as excinfo:
        main(["yolo"])

    assert excinfo.value.code == 2
    assert "yolo" in capsys.readouterr().err


def test_help_names_the_program(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as excinfo:
        main(["--help"])

    assert excinfo.value.code == 0
    out = capsys.readouterr().out
    assert out.startswith("usage: ledgermind")


def test_version(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as excinfo:
        main(["--version"])

    assert excinfo.value.code == 0
    assert __version__ in capsys.readouterr().out


# --- status ----------------------------------------------------------------------------


def test_status_prints_the_report_on_stdout(
    paths: dict[str, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    conn = open_database(paths["db_path"])
    run_id = start_job_run(conn, "heartbeat", utc_now())
    finish_job_run(conn, run_id, utc_now(), ok=True, items_processed=0)
    conn.close()

    code = main(["status"], **paths)

    assert code == 0
    out = capsys.readouterr().out
    assert "paper" in out
    assert str(paths["db_path"]) in out
    assert "Last heartbeat:  never" not in out
    assert "heartbeat" in out


def test_status_does_not_open_the_log_file(paths: dict[str, Path]) -> None:
    open_database(paths["db_path"]).close()

    assert main(["status"], **paths) == 0

    assert not paths["log_dir"].exists()
    assert not any(
        isinstance(handler, RotatingFileHandler) for handler in logging.getLogger().handlers
    )


def test_status_without_database_exits_1_with_message(
    paths: dict[str, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    code = main(["status"], **paths)

    assert code == 1
    captured = capsys.readouterr()
    assert str(paths["db_path"]) in captured.err
    assert captured.out == ""
    assert not paths["db_path"].exists()


@pytest.mark.parametrize("command", ["run", "status"])
def test_bad_config_exits_with_message(paths: dict[str, Path], command: str) -> None:
    paths["config_path"].write_text('mode = "yolo"\n', encoding="utf-8")

    with pytest.raises(SystemExit) as excinfo:
        main([command], **paths)

    assert isinstance(excinfo.value.code, str)
    assert "mode" in excinfo.value.code
    assert not paths["log_dir"].exists()


# --- the real processes, end to end ----------------------------------------------------


def last_heartbeat_finished(db_path: Path) -> bool:
    if not db_path.exists():
        return False
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT count(*) FROM job_runs WHERE job = 'heartbeat' AND ok = 1"
        ).fetchone()
    except sqlite3.OperationalError:  # table not created yet
        return False
    finally:
        conn.close()
    return row[0] > 0


def ledgermind(*args: str) -> list[str]:
    return [sys.executable, "-m", "ledgermind", *args]


@POSIX_ONLY
def test_status_works_while_run_is_running(tmp_path: Path) -> None:
    (tmp_path / "config.toml").write_text('mode = "paper"\n', encoding="utf-8")
    db_path = tmp_path / "data" / "ledgermind.db"
    log_file = tmp_path / "logs" / "ledgermind.log"
    bot = subprocess.Popen(
        ledgermind("run"),
        cwd=tmp_path,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + TIMEOUT
        while not last_heartbeat_finished(db_path):
            assert bot.poll() is None, bot.communicate()
            assert time.monotonic() < deadline, "no heartbeat recorded in time"
            time.sleep(0.05)

        status = subprocess.run(
            ledgermind("status"),
            cwd=tmp_path,
            capture_output=True,
            text=True,
            timeout=TIMEOUT,
            check=False,
        )
        assert bot.poll() is None, "run stopped while status was reading"

        bot.send_signal(signal.SIGTERM)
        _, bot_stderr = bot.communicate(timeout=TIMEOUT)
    finally:
        if bot.poll() is None:
            bot.kill()
            bot.communicate()

    assert status.returncode == 0, status.stderr
    assert "Traceback" not in status.stderr
    assert "paper" in status.stdout
    assert "data/ledgermind.db" in status.stdout.replace("\\", "/")
    assert "Last heartbeat:  never" not in status.stdout
    assert "s ago" in status.stdout
    assert "heartbeat" in status.stdout

    assert bot.returncode == 0, bot_stderr
    assert "Traceback" not in bot_stderr
    # `run` (and only `run`) writes the log file.
    log_text = log_file.read_text(encoding="utf-8")
    assert "heartbeat: ok" in log_text
    assert "stopped cleanly" in log_text
    assert sorted(path.name for path in log_file.parent.iterdir()) == ["ledgermind.log"]


def test_status_from_the_command_line_without_config(tmp_path: Path) -> None:
    result = subprocess.run(
        ledgermind("status"),
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=TIMEOUT,
        check=False,
    )

    assert result.returncode == 1
    assert "config.toml" in result.stderr
    assert "Traceback" not in result.stderr
    assert not (tmp_path / "data").exists()
    assert not (tmp_path / "logs").exists()
