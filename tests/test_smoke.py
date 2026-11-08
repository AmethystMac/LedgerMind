"""Smoke tests: the package is installed and importable."""

from importlib.metadata import version


def test_package_imports() -> None:
    import ledgermind

    assert ledgermind.__name__ == "ledgermind"


def test_version_matches_installed_metadata() -> None:
    import ledgermind

    assert isinstance(ledgermind.__version__, str)
    assert ledgermind.__version__
    assert ledgermind.__version__ == version("ledgermind")
