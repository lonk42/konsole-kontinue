"""Tests for the watcher's two decisions.

Both are about not acting: the watcher must restore on the zero-to-one
transition and at no other time, and it must never restore into a Konsole
somebody is already using. Everything here is driven with stand-ins, so no
Konsole and no session bus are needed.
"""

from __future__ import annotations

from pathlib import Path

import dbus
import pytest

from kontinue import konsole, lock, watch


class FakeInstance:
    """A Konsole that answers the handful of questions is_untouched asks."""

    def __init__(
        self,
        windows: list[int] | None = None,
        tabs: int = 1,
        sessions: int = 1,
        raises: bool = False,
    ) -> None:
        self.service = "org.kde.konsole-1234"
        self.pid = 1234
        self._windows = [1] if windows is None else windows
        self._tabs = tabs
        self._sessions = sessions
        self._raises = raises

    def window_ids(self) -> list[int]:
        if self._raises:
            raise dbus.DBusException("gone")
        return self._windows

    def view_hierarchy(self, window_id: int) -> list[str]:
        return ["hierarchy"] * self._tabs

    def session_list(self, window_id: int) -> list[int]:
        return list(range(1, self._sessions + 1))


@pytest.fixture
def config(tmp_path: Path) -> watch.Config:
    return watch.Config(
        state_path=tmp_path / "snapshot.json", scrollback_dir=None, interval=45
    )


@pytest.fixture
def watcher(config: watch.Config, monkeypatch: pytest.MonkeyPatch) -> watch.Watcher:
    """A watcher with no bus behind it and nothing scheduled for real."""
    monkeypatch.setattr(watch.dbus.mainloop.glib, "DBusGMainLoop", lambda **_: None)
    instance = watch.Watcher.__new__(watch.Watcher)
    instance.config = config
    instance.bus = object()
    instance.loop = None
    instance.known = set()
    return instance


@pytest.fixture
def scheduled(monkeypatch: pytest.MonkeyPatch) -> list:
    """Catch the restore the watcher defers rather than letting it run."""
    calls: list = []
    monkeypatch.setattr(
        watch.GLib, "timeout_add", lambda delay, fn, *a: calls.append((delay, a))
    )
    return calls


def appear(watcher: watch.Watcher, name: str) -> None:
    watcher._on_name_owner_changed(name, "", ":1.42")


def vanish(watcher: watch.Watcher, name: str) -> None:
    watcher._on_name_owner_changed(name, ":1.42", "")


def test_no_scrollback_means_untouched(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(watch, "_has_scrollback", lambda *_: False)
    assert watch.is_untouched(FakeInstance())


def test_a_konsole_with_scrollback_is_in_use(monkeypatch: pytest.MonkeyPatch) -> None:
    """Output has already come and gone, so somebody has been working here."""
    monkeypatch.setattr(watch, "_has_scrollback", lambda *_: True)
    assert not watch.is_untouched(FakeInstance())


def test_a_second_tab_means_in_use(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(watch, "_has_scrollback", lambda *_: False)
    assert not watch.is_untouched(FakeInstance(tabs=2))


def test_a_split_means_in_use(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(watch, "_has_scrollback", lambda *_: False)
    assert not watch.is_untouched(FakeInstance(sessions=2))


def test_a_second_window_means_in_use(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(watch, "_has_scrollback", lambda *_: False)
    assert not watch.is_untouched(FakeInstance(windows=[1, 2]))


def test_an_unreadable_konsole_counts_as_in_use() -> None:
    """Being wrong here costs a manual restore; being wrong the other way
    costs somebody their terminal."""
    assert not watch.is_untouched(FakeInstance(raises=True))


def test_the_first_konsole_triggers_a_restore(
    watcher: watch.Watcher, scheduled: list
) -> None:
    appear(watcher, "org.kde.konsole-1")

    assert len(scheduled) == 1
    assert scheduled[0][1] == ("org.kde.konsole-1",)


def test_a_second_konsole_does_not(watcher: watch.Watcher, scheduled: list) -> None:
    """Opening another terminal is not a request to rebuild a session."""
    appear(watcher, "org.kde.konsole-1")
    scheduled.clear()

    appear(watcher, "org.kde.konsole-2")

    assert scheduled == []


def test_a_konsole_already_running_at_startup_suppresses_the_trigger(
    watcher: watch.Watcher, scheduled: list
) -> None:
    """Starting the watcher into a live session must not restore over it."""
    watcher.known = {"org.kde.konsole-1"}

    appear(watcher, "org.kde.konsole-2")

    assert scheduled == []


def test_closing_the_last_konsole_re_arms_the_trigger(
    watcher: watch.Watcher, scheduled: list
) -> None:
    appear(watcher, "org.kde.konsole-1")
    scheduled.clear()
    vanish(watcher, "org.kde.konsole-1")

    appear(watcher, "org.kde.konsole-2")

    assert len(scheduled) == 1


def test_a_restore_in_progress_suppresses_the_trigger(
    watcher: watch.Watcher, scheduled: list
) -> None:
    """The Konsole appearing is the one the restore just launched."""
    with lock.held(watcher.config.lock_path):
        appear(watcher, "org.kde.konsole-1")

    assert scheduled == []


def test_auto_restore_can_be_turned_off(
    watcher: watch.Watcher, scheduled: list
) -> None:
    watcher.config.auto_restore = False

    appear(watcher, "org.kde.konsole-1")

    assert scheduled == []


def test_names_that_are_not_konsole_are_ignored(
    watcher: watch.Watcher, scheduled: list
) -> None:
    """Every name on the session bus comes through this handler."""
    appear(watcher, "org.kde.plasmashell")

    assert scheduled == []
    assert watcher.known == set()


def test_restoring_without_a_snapshot_does_nothing(
    watcher: watch.Watcher, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(konsole, "discover", lambda bus: [FakeInstance()])
    monkeypatch.setattr(watch, "is_untouched", lambda instance: True)

    assert watcher.restore_into("org.kde.konsole-1234") is None


def test_an_in_use_konsole_is_left_alone(
    watcher: watch.Watcher, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Even having got this far, the freshness check is the last word."""
    called = []
    monkeypatch.setattr(konsole, "discover", lambda bus: [FakeInstance()])
    monkeypatch.setattr(watch, "is_untouched", lambda instance: False)
    monkeypatch.setattr(
        watch.restore_mod, "restore", lambda *a, **k: called.append(a)
    )
    watcher.config.state_path.write_text('{"version": 1, "instances": []}')

    assert watcher.restore_into("org.kde.konsole-1234") is None
    assert called == []


def test_snapshotting_is_skipped_while_a_restore_runs(
    watcher: watch.Watcher, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Otherwise a half-rebuilt Konsole overwrites the snapshot it came from."""
    called = []
    monkeypatch.setattr(
        watch.snapshot, "save", lambda *a, **k: called.append(a) or None
    )

    with lock.held(watcher.config.lock_path):
        assert watcher.snapshot_once() is None

    assert called == []
