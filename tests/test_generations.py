"""Tests for retained generations.

The point of keeping older snapshots is that the current one is overwritten
every time the timer fires, so a snapshot that caught a bad moment is the only
record left one tick later. These tests are mostly about the two ways that
safety net can fail quietly: filling the archive with near-identical copies
until the useful one has aged out, and letting the pruner collect the
scrollback a retained arrangement points at.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from kontinue import generations, model
from kontinue.scrollback import FILE_MODE


def snapshot_text(captured_at: datetime, scrollback_file: str | None = None) -> str:
    """A minimal but schema-valid snapshot, optionally pointing at scrollback."""
    pane = model.Pane(view_id=0, session_id=1)
    if scrollback_file is not None:
        pane.scrollback_file = scrollback_file
        pane.scrollback_lines = 10
    tab = model.Tab(root=pane, raw_hierarchy="(0)[0]")
    window = model.Window(window_id=1, tabs=[tab])
    instance = model.Instance(pid=123, windows=[window])
    snap = model.Snapshot(
        captured_at=captured_at.isoformat(timespec="seconds"), instances=[instance]
    )
    return snap.dumps()


@pytest.fixture
def state(tmp_path: Path) -> Path:
    return tmp_path / "kontinue" / "snapshot.json"


def write_current(state: Path, moment: datetime, scrollback_file: str | None = None) -> None:
    state.parent.mkdir(parents=True, exist_ok=True)
    state.write_text(snapshot_text(moment, scrollback_file))


def test_nothing_to_retain_is_not_an_error(state: Path) -> None:
    """Runs on the way to every save, so an absent snapshot must be harmless."""
    assert generations.retain(state) is None


def test_first_retain_archives_the_current_snapshot(state: Path) -> None:
    moment = datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc)
    write_current(state, moment)

    kept = generations.retain(state)

    assert kept is not None
    assert kept.name == "snapshot-20260820T120000.json"
    assert model.Snapshot.loads(kept.read_text()).captured_at == moment.isoformat()


def test_a_generation_is_owner_only(state: Path) -> None:
    """A generation is a snapshot, so it carries the same verbatim screen text."""
    write_current(state, datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc))
    kept = generations.retain(state)
    assert kept is not None
    assert kept.stat().st_mode & 0o777 == FILE_MODE


def test_timer_saves_do_not_fill_the_archive(state: Path) -> None:
    """Forty-five seconds apart is the same arrangement, not a new generation."""
    base = datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc)
    write_current(state, base)
    assert generations.retain(state) is not None

    for tick in range(1, 6):
        write_current(state, base + timedelta(seconds=45 * tick))
        assert generations.retain(state) is None

    assert len(generations.listing(state)) == 1


def test_a_gap_earns_a_new_generation(state: Path) -> None:
    base = datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc)
    write_current(state, base)
    generations.retain(state)

    write_current(state, base + timedelta(hours=2))
    assert generations.retain(state) is not None
    assert len(generations.listing(state)) == 2


def test_force_ignores_the_gap(state: Path) -> None:
    """Session end is worth keeping however recently the last one was kept."""
    base = datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc)
    write_current(state, base)
    generations.retain(state)

    write_current(state, base + timedelta(seconds=45))
    assert generations.retain(state, force=True) is not None
    assert len(generations.listing(state)) == 2


def test_the_same_snapshot_is_never_archived_twice(state: Path) -> None:
    """Two session-end events over one unchanged snapshot must not duplicate it."""
    write_current(state, datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc))
    assert generations.retain(state, force=True) is not None
    assert generations.retain(state, force=True) is None
    assert len(generations.listing(state)) == 1


def test_listing_is_newest_first(state: Path) -> None:
    base = datetime(2026, 8, 20, 6, 0, tzinfo=timezone.utc)
    for hour in range(4):
        write_current(state, base + timedelta(hours=hour))
        generations.retain(state)

    stamps = [path.name for path in generations.listing(state)]
    assert stamps == sorted(stamps, reverse=True)


def test_oldest_generations_are_trimmed(state: Path) -> None:
    base = datetime(2026, 8, 20, 0, 0, tzinfo=timezone.utc)
    for hour in range(8):
        write_current(state, base + timedelta(hours=hour))
        generations.retain(state, keep=3)

    kept = generations.listing(state)
    assert len(kept) == 3
    # The three most recent survive, not the three first written.
    assert kept[0].name == "snapshot-20260820T070000.json"
    assert kept[-1].name == "snapshot-20260820T050000.json"


def test_load_counts_from_one(state: Path) -> None:
    base = datetime(2026, 8, 20, 0, 0, tzinfo=timezone.utc)
    for hour in range(3):
        write_current(state, base + timedelta(hours=hour))
        generations.retain(state)

    newest, path = generations.load(state, 1)
    assert newest.captured_at.startswith("2026-08-20T02:00")
    assert path.name == "snapshot-20260820T020000.json"

    with pytest.raises(ValueError):
        generations.load(state, 0)
    with pytest.raises(FileNotFoundError):
        generations.load(state, 99)


def test_generations_hold_their_scrollback_against_pruning(state: Path) -> None:
    """The failure this prevents: a generation that restores empty panes.

    A retained arrangement points at inodes the current snapshot has moved on
    from. If those are not spared, the layout still rebuilds and every pane
    comes back blank, which looks like a restore bug rather than a prune one.
    """
    base = datetime(2026, 8, 20, 0, 0, tzinfo=timezone.utc)
    write_current(state, base, scrollback_file="4242.cells")
    generations.retain(state)
    write_current(state, base + timedelta(hours=2), scrollback_file="9999.cells")
    generations.retain(state)

    assert generations.referenced_inodes(state) == {4242, 9999}


def test_an_unreadable_generation_is_skipped_not_fatal(state: Path) -> None:
    write_current(state, datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc),
                  scrollback_file="77.cells")
    generations.retain(state)
    (generations.directory(state) / "snapshot-20260101T000000.json").write_text("{ not json")

    # The good one still counts; the broken one does not take the call down.
    assert generations.referenced_inodes(state) == {77}


def test_a_stray_file_is_not_mistaken_for_a_generation(state: Path) -> None:
    write_current(state, datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc))
    generations.retain(state)
    (generations.directory(state) / "notes.txt").write_text("hello")
    (generations.directory(state) / "snapshot-nonsense.json").write_text("{}")

    assert [path.name for path in generations.listing(state)] == [
        "snapshot-20260820T120000.json"
    ]


def test_a_clock_going_backwards_does_not_archive(state: Path) -> None:
    """An older snapshot than the newest generation is not a new generation."""
    base = datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc)
    write_current(state, base)
    generations.retain(state)

    write_current(state, base - timedelta(hours=3))
    assert generations.retain(state, force=True) is None


def wide_snapshot(
    captured_at: datetime, dirs: list[str], titles: list[str] | None = None
) -> model.Snapshot:
    """One tab per directory, the shape a working session usually has."""
    titles = titles or ["%d : %n"] * len(dirs)
    tabs = [
        model.Tab(
            root=model.Pane(view_id=i, session_id=i + 1, cwd=cwd, local_title_format=title),
            raw_hierarchy=f"({i})[{i}]",
        )
        for i, (cwd, title) in enumerate(zip(dirs, titles))
    ]
    window = model.Window(window_id=1, tabs=tabs)
    return model.Snapshot(
        captured_at=captured_at.isoformat(timespec="seconds"),
        instances=[model.Instance(pid=123, windows=[window])],
    )


def archive(state: Path, snap: model.Snapshot) -> None:
    state.parent.mkdir(parents=True, exist_ok=True)
    state.write_text(snap.dumps())
    assert generations.retain(state, force=True) is not None


def test_describe_names_directories_by_how_many_panes_were_in_them() -> None:
    moment = datetime(2026, 9, 21, 14, 10, tzinfo=timezone.utc)
    snap = wide_snapshot(
        moment,
        ["/config/scrub", "/config/git/QRCatcher", "/config/git/QRCatcher", "/tmp"],
        ["%d : %n", "Data seeder", "%d : %n", "%d : %n"],
    )

    assert generations.describe(snap) == (
        "QRCatcher (2), scrub, tmp; named: Data seeder"
    )


def test_describe_caps_a_long_list() -> None:
    moment = datetime(2026, 9, 21, 14, 10, tzinfo=timezone.utc)
    snap = wide_snapshot(moment, [f"/d{i}" for i in range(9)])

    assert generations.describe(snap, limit=3) == "d0, d1, d2, +6 more"


def test_a_session_saved_over_by_a_small_one_is_flagged(state: Path) -> None:
    """The shape of the loss that prompted this: ten tabs, then one."""
    before = datetime(2026, 9, 21, 14, 10, tzinfo=timezone.utc)
    archive(state, wide_snapshot(before, [f"/d{i}" for i in range(10)]))
    archive(state, wide_snapshot(before + timedelta(hours=8), ["/d0"]))
    current = wide_snapshot(before + timedelta(hours=8, minutes=3), ["/d0", "/d1"])

    found = generations.outgrown(state, current)

    assert found is not None
    index, generation = found
    assert index == 2
    assert generation.pane_count() == 10


def test_closing_a_few_tabs_is_not_flagged(state: Path) -> None:
    before = datetime(2026, 9, 21, 14, 10, tzinfo=timezone.utc)
    archive(state, wide_snapshot(before, [f"/d{i}" for i in range(12)]))
    current = wide_snapshot(before + timedelta(hours=1), [f"/d{i}" for i in range(9)])

    assert generations.outgrown(state, current) is None


def test_an_old_large_generation_is_not_flagged(state: Path) -> None:
    """A week-old arrangement is likelier a deliberate change than a loss."""
    before = datetime(2026, 9, 10, 14, 10, tzinfo=timezone.utc)
    archive(state, wide_snapshot(before, [f"/d{i}" for i in range(10)]))
    current = wide_snapshot(before + timedelta(days=7), ["/d0"])

    assert generations.outgrown(state, current) is None


def test_with_no_current_snapshot_any_sizeable_generation_is_flagged(state: Path) -> None:
    archive(state, wide_snapshot(datetime(2026, 9, 21, tzinfo=timezone.utc),
                                 ["/a", "/b", "/c"]))

    found = generations.outgrown(state, None)

    assert found is not None and found[0] == 1


def test_the_newest_large_generation_is_the_one_flagged(state: Path) -> None:
    """The lost session is the last big one, not the biggest of the day."""
    morning = datetime(2026, 9, 21, 11, 0, tzinfo=timezone.utc)
    archive(state, wide_snapshot(morning, [f"/d{i}" for i in range(12)]))
    archive(state, wide_snapshot(morning + timedelta(hours=3), [f"/d{i}" for i in range(10)]))
    archive(state, wide_snapshot(morning + timedelta(hours=11), ["/d0"]))
    current = wide_snapshot(morning + timedelta(hours=11, minutes=1), ["/d0"])

    found = generations.outgrown(state, current)

    assert found is not None
    assert found[0] == 2
    assert found[1].pane_count() == 10
