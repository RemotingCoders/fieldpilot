"""Configuration loading.

This module exists because of a bug that was invisible: every module read its
settings with `os.getenv`, the README told people to write a `.env`, and
nothing loaded it. The geocoder found no key, silently fell back to its offline
stand-in, and printed coordinates that looked real.

These tests are here so that failure mode cannot come back.
"""

from __future__ import annotations

from fieldpilot.config import describe, load_env


def test_a_env_file_actually_reaches_the_environment(tmp_path, monkeypatch) -> None:
    env = tmp_path / ".env"
    env.write_text("FIELDPILOT_MAPS_API_KEY=secret123\nGOOGLE_CLOUD_LOCATION=global\n")
    monkeypatch.delenv("FIELDPILOT_MAPS_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_CLOUD_LOCATION", raising=False)

    loaded = load_env(env)

    import os

    assert os.environ["FIELDPILOT_MAPS_API_KEY"] == "secret123"
    assert os.environ["GOOGLE_CLOUD_LOCATION"] == "global"
    assert set(loaded) == {"FIELDPILOT_MAPS_API_KEY", "GOOGLE_CLOUD_LOCATION"}


def test_the_real_environment_wins_over_the_file(tmp_path, monkeypatch) -> None:
    """`FIELDPILOT_MODEL=x fieldpilot ...` has to keep working."""
    env = tmp_path / ".env"
    env.write_text("FIELDPILOT_MODEL=from-file\n")
    monkeypatch.setenv("FIELDPILOT_MODEL", "from-shell")

    load_env(env)

    import os

    assert os.environ["FIELDPILOT_MODEL"] == "from-shell"


def test_comments_blank_lines_and_quotes_are_handled(tmp_path, monkeypatch) -> None:
    env = tmp_path / ".env"
    env.write_text(
        '# a comment\n\nA_QUOTED="value"\nB_SINGLE=\'other\'\n'
        "  C_PADDED  =  spaced  \nnot a pair\n"
    )
    for key in ("A_QUOTED", "B_SINGLE", "C_PADDED"):
        monkeypatch.delenv(key, raising=False)

    load_env(env)

    import os

    assert os.environ["A_QUOTED"] == "value"
    assert os.environ["B_SINGLE"] == "other"
    assert os.environ["C_PADDED"] == "spaced"


def test_a_missing_file_is_not_an_error(tmp_path) -> None:
    assert load_env(tmp_path / "nope.env") == {}


def test_describe_never_prints_the_secret(monkeypatch) -> None:
    """A config line that leaks a key into a terminal recording is how keys end
    up in demo videos."""
    monkeypatch.setenv("FIELDPILOT_MAPS_API_KEY", "AIzaSyVERYSECRET")
    line = describe()
    assert "AIzaSyVERYSECRET" not in line
    assert "set" in line


def test_describe_says_when_geocoding_will_be_offline(monkeypatch) -> None:
    monkeypatch.delenv("FIELDPILOT_MAPS_API_KEY", raising=False)
    assert "offline" in describe()


def test_config_flag_needs_no_subcommand(tmp_path, monkeypatch, capsys):
    """`fieldpilot --config` is a whole command on its own.

    It was not, at first: the subparser was declared required, so the flag that
    exists to diagnose a broken configuration could not run without naming a
    subcommand to diagnose it with.
    """
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("GOOGLE_CLOUD_PROJECT=proj-from-file\n")
    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)

    from fieldpilot.cli import main

    assert main(["--config"]) == 0
    out = capsys.readouterr().out
    assert "proj-from-file" in out
    assert ".env" in out


def test_no_command_prints_help_and_fails(capsys):
    from fieldpilot.cli import main

    assert main([]) == 2
    assert "usage:" in capsys.readouterr().out


def test_describe_never_prints_the_key(monkeypatch):
    """The whole point of reporting presence instead of value."""
    from fieldpilot import config

    monkeypatch.setenv("FIELDPILOT_MAPS_API_KEY", "AIzaSy-secret-do-not-print")
    assert "AIzaSy-secret-do-not-print" not in config.describe()
    assert "set" in config.describe()


def test_afc_notice_is_dropped_but_other_warnings_survive(caplog):
    """The filter has to be narrow enough to still let real trouble through."""
    import logging

    from fieldpilot import config

    config.quiet_known_noise()
    logger = logging.getLogger("google_genai.models")

    with caplog.at_level(logging.WARNING, logger="google_genai.models"):
        logger.warning(
            "Direct use of automatic function calling (AFC) in "
            "AsyncModels.generate_content is not recommended."
        )
        logger.warning("Quota exceeded for requests per minute")

    messages = [r.getMessage() for r in caplog.records]
    assert not any("automatic function calling" in m for m in messages)
    assert any("Quota exceeded" in m for m in messages)
