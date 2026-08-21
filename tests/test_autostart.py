"""Tests for the autostart entry.

An autostart entry that is written but never runs is the worst outcome here,
because nothing reports it: the user logs in, no watcher starts, and the first
they hear of it is an empty restore. So these cover what goes in the ``Exec``
line rather than just that a file appeared.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from kontinue import autostart


@pytest.fixture(autouse=True)
def config_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    return tmp_path / "config"


def test_install_creates_the_entry(config_home: Path) -> None:
    path = autostart.install()

    assert path == config_home / "autostart" / "kontinue-watch.desktop"
    assert path.exists()
    body = path.read_text()
    assert body.startswith("[Desktop Entry]")
    assert "Type=Application" in body
    assert "watch" in body


def test_the_entry_is_readable_by_the_session(config_home: Path) -> None:
    """0600 would be wrong here: the desktop has to read it to launch it."""
    path = autostart.install()
    assert path.stat().st_mode & 0o777 == 0o644


def test_install_is_idempotent(config_home: Path) -> None:
    first = autostart.install()
    second = autostart.install()
    assert first == second
    assert len(list((config_home / "autostart").iterdir())) == 1


def test_interval_is_baked_in(config_home: Path) -> None:
    body = autostart.install(interval=120).read_text()
    exec_line = next(line for line in body.splitlines() if line.startswith("Exec="))
    assert exec_line.endswith("watch --interval 120")


def test_no_scrollback_is_baked_in(config_home: Path) -> None:
    body = autostart.install(no_scrollback=True).read_text()
    exec_line = next(line for line in body.splitlines() if line.startswith("Exec="))
    assert exec_line.endswith("watch --no-scrollback")


def test_exec_prefers_the_installed_binary(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(autostart.shutil, "which", lambda name: "/usr/bin/kontinue")
    assert autostart.command() == "/usr/bin/kontinue"


def test_exec_falls_back_to_the_running_interpreter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pip --user install without ~/.local/bin on PATH still has to work.

    Writing a bare `kontinue` there would produce an entry the desktop cannot
    launch, and nothing would report it until a restore came up empty.
    """
    monkeypatch.setattr(autostart.shutil, "which", lambda name: None)
    monkeypatch.setattr(autostart.sys, "executable", "/opt/py/bin/python3")
    assert autostart.command() == "/opt/py/bin/python3 -m kontinue"


def test_uninstall_removes_it(config_home: Path) -> None:
    autostart.install()
    assert autostart.uninstall() is not None
    assert autostart.installed() is None


def test_uninstall_when_absent_is_not_an_error(config_home: Path) -> None:
    assert autostart.uninstall() is None


def test_installed_reports_the_path(config_home: Path) -> None:
    assert autostart.installed() is None
    path = autostart.install()
    assert autostart.installed() == path


# -- will the entry actually run -----------------------------------------


def fake_proc(root: Path, processes: dict[int, tuple[str, list[str]]]) -> Path:
    """A stand-in /proc: pid -> (comm, argv)."""
    root.mkdir(parents=True, exist_ok=True)
    for pid, (comm, argv) in processes.items():
        entry = root / str(pid)
        entry.mkdir()
        (entry / "comm").write_text(comm + "\n")
        (entry / "cmdline").write_bytes(b"\0".join(part.encode() for part in argv) + b"\0")
    (root / "uptime").write_text("1 1")  # a non-numeric entry, as /proc really has
    return root


def test_processor_finds_a_session_manager(tmp_path: Path) -> None:
    proc = fake_proc(tmp_path / "proc", {
        1: ("systemd", ["/sbin/init"]),
        42: ("ksmserver", ["/usr/bin/ksmserver"]),
    })
    assert autostart.processor(proc) == "ksmserver"


def test_processor_reports_none_on_a_hand_rolled_session(tmp_path: Path) -> None:
    """The case that matters: kwin plus plasmashell and no session manager.

    Nothing reads ~/.config/autostart there, so an installed entry is inert.
    That is the same missing piece that stops Konsole restoring itself, which
    is why this has to be reported rather than assumed to work.
    """
    proc = fake_proc(tmp_path / "proc", {
        7: ("kwin_wayland", ["kwin_wayland", "--xwayland"]),
        9: ("plasmashell", ["plasmashell"]),
    })
    assert autostart.processor(proc) is None


def test_processor_survives_a_process_vanishing(tmp_path: Path) -> None:
    """Scanning /proc races with processes exiting; that must not raise."""
    proc = fake_proc(tmp_path / "proc", {5: ("bash", ["bash"])})
    (proc / "6").mkdir()  # a pid with no readable comm, as an exiting one has
    assert autostart.processor(proc) is None


def test_watcher_pids_finds_a_running_watcher(tmp_path: Path) -> None:
    proc = fake_proc(tmp_path / "proc", {
        11: ("kontinue", ["/usr/bin/kontinue", "watch", "--interval", "45"]),
        12: ("bash", ["bash"]),
    })
    assert autostart.watcher_pids(proc, uid=os.getuid()) == [11]


def test_watcher_pids_matches_the_module_form(tmp_path: Path) -> None:
    """`python -m kontinue watch` is a watcher too, and status must say so."""
    proc = fake_proc(tmp_path / "proc", {
        13: ("python3", ["/usr/bin/python3", "-m", "kontinue", "watch"]),
    })
    assert autostart.watcher_pids(proc, uid=os.getuid()) == [13]


def test_watcher_pids_ignores_other_subcommands(tmp_path: Path) -> None:
    proc = fake_proc(tmp_path / "proc", {
        14: ("kontinue", ["/usr/bin/kontinue", "save"]),
        15: ("kontinue", ["/usr/bin/kontinue", "show"]),
    })
    assert autostart.watcher_pids(proc, uid=os.getuid()) == []


def test_watcher_pids_ignores_other_users(tmp_path: Path) -> None:
    proc = fake_proc(tmp_path / "proc", {
        16: ("kontinue", ["/usr/bin/kontinue", "watch"]),
    })
    assert autostart.watcher_pids(proc, uid=os.getuid() + 1) == []
