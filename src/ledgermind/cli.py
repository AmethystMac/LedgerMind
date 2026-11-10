"""The command line: ``python -m ledgermind run | status``.

- ``run`` starts the bot (scheduler and all jobs) until Ctrl+C or SIGTERM.
- ``status`` prints the mode, the database path, the last heartbeat and the last run of each
  job. It only reads the database and logs to the console only, so it's safe to run while the
  bot is running in another terminal.

Run both from the project folder: ``config.toml``, ``.env``, ``data/`` and ``logs/`` are found
relative to it.
"""

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from ledgermind import __version__, app
from ledgermind.config import DEFAULT_CONFIG_PATH, DEFAULT_ENV_PATH, load_config_or_exit
from ledgermind.db.migrate import DEFAULT_DB_PATH
from ledgermind.jobs import utc_now
from ledgermind.logging_setup import DEFAULT_LOG_DIR, setup_logging
from ledgermind.status import StatusError, format_status, read_status

PROG = "ledgermind"


def main(
    argv: Sequence[str] | None = None,
    *,
    config_path: Path = DEFAULT_CONFIG_PATH,
    env_path: Path = DEFAULT_ENV_PATH,
    db_path: Path = DEFAULT_DB_PATH,
    log_dir: Path = DEFAULT_LOG_DIR,
) -> int:
    """Run the command in ``argv`` (default: ``sys.argv[1:]``) and return the exit code."""
    parser = _parser()
    args = parser.parse_args(argv)
    if args.command == "run":
        app.main(config_path, env_path, db_path, log_dir)
        return 0
    if args.command == "status":
        return _status(config_path, env_path, db_path)
    parser.print_help(sys.stderr)
    return 2


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=PROG,
        description="Hands-off crypto trading bot. Run from the project folder.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    commands = parser.add_subparsers(dest="command", metavar="command")
    commands.add_parser("run", help="run the bot until Ctrl+C or SIGTERM")
    commands.add_parser("status", help="show mode, database, last heartbeat and last run per job")
    return parser


def _status(config_path: Path, env_path: Path, db_path: Path) -> int:
    config = load_config_or_exit(config_path, env_path)
    setup_logging(config.settings.logging, None)  # console only: never touch the log file
    try:
        report = read_status(config, db_path, utc_now())
    except StatusError as exc:
        print(f"Status error: {exc}", file=sys.stderr)
        return 1
    print(format_status(report))
    return 0
