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

from kontinue import generations, konsole, lock, model, watch


class FakeInstance:
    """A Konsole that answers the handful of questions is_untouched asks."""

    def __init__(
        self,
        windows: list[int] | None = None,
        tabs: int = 1,
        sessions: int = 1,
        raises: bool = False,
        pid: int = 1234,
    ) -> None:
        self.service = f"org.kde.konsole-{pid}"
        self.pid = pid
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


# -- a watcher started after the first Konsole -----------------------------


def saved_session(
    path: Path, pid: int = 1234, start_time: int | None = 500, panes: int = 3
) -> model.Snapshot:
    tabs = [
        model.Tab(root=model.Pane(view_id=i, session_id=i + 1), raw_hierarchy=f"({i})[{i}]")
        for i in range(panes)
    ]
    snap = model.Snapshot(
        captured_at="2026-09-21T14:10:00+00:00",
        instances=[
            model.Instance(
                pid=pid,
                start_time=start_time,
                windows=[model.Window(window_id=1, tabs=tabs)],
            )
        ],
    )
    path.write_text(snap.dumps())
    return snap


@pytest.fixture
def started_detached(monkeypatch: pytest.MonkeyPatch) -> None:
    """As from rc-file ``setsid nohup``: no terminal to be hung up by."""
    monkeypatch.setattr(watch, "has_controlling_terminal", lambda: False)


@pytest.fixture
def start_times(monkeypatch: pytest.MonkeyPatch) -> dict[int, int]:
    """Start times of running Konsoles, by pid, as /proc would report them."""
    times: dict[int, int] = {}
    monkeypatch.setattr(konsole, "read_start_time", lambda pid: times.get(pid))
    return times


def test_a_session_whose_konsole_is_gone_has_ended(
    tmp_path: Path, start_times: dict[int, int]
) -> None:
    saved = saved_session(tmp_path / "s.json", pid=1234)
    start_times[9999] = 700

    assert watch.session_ended(saved, [FakeInstance(pid=9999)])


def test_a_recycled_pid_is_not_the_same_konsole(
    tmp_path: Path, start_times: dict[int, int]
) -> None:
    """Pids come round again, especially after a container restart."""
    saved = saved_session(tmp_path / "s.json", pid=1234, start_time=500)
    start_times[1234] = 900

    assert watch.session_ended(saved, [FakeInstance(pid=1234)])


def test_the_same_konsole_still_running_has_not_ended(
    tmp_path: Path, start_times: dict[int, int]
) -> None:
    saved = saved_session(tmp_path / "s.json", pid=1234, start_time=500)
    start_times[1234] = 500

    assert not watch.session_ended(saved, [FakeInstance(pid=1234)])


def test_an_older_snapshot_matches_on_pid_alone(
    tmp_path: Path, start_times: dict[int, int]
) -> None:
    """With no start time recorded, a matching pid errs towards leaving it alone."""
    saved = saved_session(tmp_path / "s.json", pid=1234, start_time=None)
    start_times[1234] = 900

    assert not watch.session_ended(saved, [FakeInstance(pid=1234)])


def test_nothing_running_means_the_session_has_ended(tmp_path: Path) -> None:
    saved = saved_session(tmp_path / "s.json")

    assert watch.session_ended(saved, [])


def test_a_new_konsole_at_startup_after_a_finished_session_is_restored_into(
    watcher: watch.Watcher, scheduled: list, started_detached: None,
    start_times: dict[int, int],
) -> None:
    """The incident: the rc file starts the watcher inside the fresh Konsole."""
    saved_session(watcher.config.state_path, pid=1234)
    fresh = FakeInstance(pid=9999)
    start_times[9999] = 700

    watcher.resume([fresh])

    assert scheduled == [(1000, (fresh.service,))]


def test_a_finished_session_is_archived_before_anything_overwrites_it(
    watcher: watch.Watcher, scheduled: list, started_detached: None,
    start_times: dict[int, int],
) -> None:
    """The next timer save replaces it, whether or not a restore happens."""
    saved_session(watcher.config.state_path, pid=1234, panes=10)
    watcher.config.auto_restore = False

    watcher.resume([FakeInstance(pid=9999)])

    kept = generations.listing(watcher.config.state_path)
    assert len(kept) == 1
    assert model.Snapshot.loads(kept[0].read_text()).pane_count() == 10
    assert scheduled == []


def test_the_saved_session_still_running_is_left_alone(
    watcher: watch.Watcher, scheduled: list, started_detached: None,
    start_times: dict[int, int],
) -> None:
    """A watcher restarted under a live session must never restore over it."""
    saved_session(watcher.config.state_path, pid=1234, start_time=500)
    start_times[1234] = 500

    watcher.resume([FakeInstance(pid=1234)])

    assert scheduled == []
    assert generations.listing(watcher.config.state_path) == []


def test_several_konsoles_at_startup_are_left_alone(
    watcher: watch.Watcher, scheduled: list, started_detached: None,
    start_times: dict[int, int],
) -> None:
    saved_session(watcher.config.state_path, pid=1234)

    watcher.resume([FakeInstance(pid=8888), FakeInstance(pid=9999)])

    assert scheduled == []


def test_a_watcher_started_from_a_terminal_does_not_restore_into_it(
    watcher: watch.Watcher, scheduled: list, start_times: dict[int, int],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Retiring that Konsole would hang up the shell, and the watcher with it."""
    monkeypatch.setattr(watch, "has_controlling_terminal", lambda: True)
    saved_session(watcher.config.state_path, pid=1234)

    watcher.resume([FakeInstance(pid=9999)])

    assert scheduled == []


def test_startup_with_nothing_saved_does_nothing(
    watcher: watch.Watcher, scheduled: list, started_detached: None
) -> None:
    watcher.resume([FakeInstance(pid=9999)])

    assert scheduled == []


def test_the_konsole_a_restore_launched_is_not_restored_into_again(
    watcher: watch.Watcher, scheduled: list, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Its name is claimed mid-restore, so the signal arrives after the lock is
    released, and it must not read as another first Konsole."""
    saved_session(watcher.config.state_path)
    fresh, launched = FakeInstance(pid=1111), FakeInstance(pid=2222)
    running = [fresh]
    monkeypatch.setattr(konsole, "discover", lambda bus: list(running))
    monkeypatch.setattr(watch, "is_untouched", lambda instance: True)

    def fake_restore(*args, **kwargs):
        running.append(launched)
        return watch.restore_mod.Report(tabs=3, panes=3)

    monkeypatch.setattr(watch.restore_mod, "restore", fake_restore)
    monkeypatch.setattr(
        watch.restore_mod, "close_instance", lambda instance: running.remove(instance)
    )
    appear(watcher, fresh.service)
    scheduled.clear()

    watcher.restore_into(fresh.service)
    vanish(watcher, fresh.service)
    appear(watcher, launched.service)

    assert scheduled == []
