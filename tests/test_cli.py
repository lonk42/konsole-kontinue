"""Tests for argument parsing, at the seam where a flag reaches a handler.

``-v`` is shared by every subcommand through a parent parser and defaults to
``argparse.SUPPRESS``, so the attribute is simply absent unless the flag was
given. A handler that reads ``args.verbose`` directly therefore works in every
manual test, where the flag gets passed to see what is happening, and crashes
on the one invocation that matters: the bare ``kontinue watch`` that the
autostart entry and the startup line both use.
"""

from __future__ import annotations

import argparse

import pytest

from kontinue import cli


SUBCOMMANDS = [
    ["save"],
    ["restore"],
    ["prune"],
    ["watch"],
    ["show"],
    ["install"],
    ["uninstall"],
    ["status"],
]


@pytest.mark.parametrize("argv", SUBCOMMANDS, ids=lambda a: a[0])
def test_verbose_is_absent_without_the_flag(argv: list[str]) -> None:
    """SUPPRESS is deliberate, so handlers must not assume the attribute."""
    args = cli.build_parser().parse_args(argv)
    assert not hasattr(args, "verbose")


@pytest.mark.parametrize("argv", [["-v", "watch"], ["watch", "-v"]])
def test_verbose_survives_on_either_side_of_the_subcommand(
    argv: list[str],
) -> None:
    """The reason for SUPPRESS: a subparser default would erase the outer -v."""
    args = cli.build_parser().parse_args(argv)
    assert args.verbose is True


def test_watch_starts_without_verbose(monkeypatch: pytest.MonkeyPatch) -> None:
    """``kontinue watch``, exactly as the autostart entry spells it."""
    from kontinue import watch

    seen: list[watch.Config] = []

    class StubWatcher:
        def __init__(self, config: watch.Config) -> None:
            seen.append(config)

        def run(self) -> int:
            return 0

    monkeypatch.setattr(watch, "Watcher", StubWatcher)

    args = cli.build_parser().parse_args(["watch"])
    assert cli.cmd_watch(args) == 0
    assert seen and seen[0].auto_restore is True


def test_watch_accepts_verbose(monkeypatch: pytest.MonkeyPatch) -> None:
    from kontinue import watch

    class StubWatcher:
        def __init__(self, config: watch.Config) -> None:
            pass

        def run(self) -> int:
            return 0

    monkeypatch.setattr(watch, "Watcher", StubWatcher)

    assert cli.cmd_watch(cli.build_parser().parse_args(["-v", "watch"])) == 0
