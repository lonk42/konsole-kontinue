"""Tests for the scrollback store.

The store's job is to avoid recopying tens of megabytes on every snapshot while
never producing a copy that silently disagrees with the source, so these tests
are mostly about the cases where appending is the wrong answer.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from kontinue import history
from kontinue.scrollback import (
    DIR_MODE,
    FILE_MODE,
    Owner,
    Store,
    ensure_private_dir,
    write_private,
)

from test_history import build_cells


def make_files(tmp_path: Path, lines: list[str]) -> tuple[history.HistoryFiles, list[int]]:
    """Stand-in for a pane's history, as ordinary files rather than /proc links."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    cells, offsets = build_cells(lines)
    cells_path = tmp_path / "cells"
    cells_path.write_bytes(cells)
    index_path = tmp_path / "index"
    index_path.write_bytes(b"".join(offset.to_bytes(8, "little") for offset in offsets))
    flags_path = tmp_path / "flags"
    flags_path.write_bytes(b"\x00" * (len(offsets) * history.LINE_PROPERTY_SIZE))

    files = history.HistoryFiles(
        str(index_path), str(cells_path), str(flags_path), cells_path.stat().st_ino
    )
    return files, offsets


def append_lines(files: history.HistoryFiles, existing: list[str], extra: list[str]) -> list[int]:
    cells, offsets = build_cells(existing + extra)
    Path(files.cells).write_bytes(cells)
    Path(files.index).write_bytes(b"".join(o.to_bytes(8, "little") for o in offsets))
    return offsets


def test_first_capture_copies_everything(tmp_path: Path) -> None:
    files, offsets = make_files(tmp_path / "src", ["alpha", "beta"])
    store = Store(tmp_path / "store")

    entry = store.capture(files, history.complete_bytes(offsets), len(offsets))

    assert entry is not None
    stored = (store.root / entry.filename).read_bytes()
    assert history.decode(stored, offsets) == ["alpha", "beta"]


def test_stored_files_are_owner_only(tmp_path: Path) -> None:
    """Scrollback is a verbatim terminal record and routinely holds secrets."""
    files, offsets = make_files(tmp_path / "src", ["secret"])
    store = Store(tmp_path / "store")
    entry = store.capture(files, history.complete_bytes(offsets), len(offsets))

    assert entry is not None
    assert (store.root / entry.filename).stat().st_mode & 0o777 == 0o600
    assert store.root.stat().st_mode & 0o777 == 0o700


def test_growth_is_appended_not_recopied(tmp_path: Path) -> None:
    base = ["line one", "line two"]
    files, offsets = make_files(tmp_path / "src", base)
    store = Store(tmp_path / "store")
    entry = store.capture(files, history.complete_bytes(offsets), len(offsets))
    assert entry is not None
    first_size = entry.cells_bytes

    grown = append_lines(files, base, ["line three"])
    stored_path = store.root / entry.filename
    before = stored_path.stat().st_mtime_ns

    entry = store.capture(files, history.complete_bytes(grown), len(grown))

    assert entry is not None
    assert entry.cells_bytes > first_size
    assert history.decode(stored_path.read_bytes(), grown) == [
        "line one",
        "line two",
        "line three",
    ]
    assert stored_path.stat().st_mtime_ns >= before


def test_nothing_new_means_no_copy(tmp_path: Path) -> None:
    files, offsets = make_files(tmp_path / "src", ["unchanged"])
    store = Store(tmp_path / "store")
    first = store.capture(files, history.complete_bytes(offsets), len(offsets))
    assert first is not None

    second = store.capture(files, history.complete_bytes(offsets), len(offsets))

    assert second is not None
    assert second.cells_bytes == first.cells_bytes


def test_a_cleared_history_forces_a_full_recopy(tmp_path: Path) -> None:
    """Clearing a pane resets the files, so equal size does not mean equal content."""
    files, offsets = make_files(tmp_path / "src", ["original content here"])
    store = Store(tmp_path / "store")
    entry = store.capture(files, history.complete_bytes(offsets), len(offsets))
    assert entry is not None

    replaced = append_lines(files, [], ["replaced content here"])
    entry = store.capture(files, history.complete_bytes(replaced), len(replaced))

    assert entry is not None
    stored = (store.root / entry.filename).read_bytes()
    assert history.decode(stored, replaced) == ["replaced content here"]


def test_a_shrunk_history_forces_a_full_recopy(tmp_path: Path) -> None:
    """Konsole truncates the files when it reflows a resized pane."""
    base = ["one", "two", "three"]
    files, offsets = make_files(tmp_path / "src", base)
    store = Store(tmp_path / "store")
    assert store.capture(files, history.complete_bytes(offsets), len(offsets))

    shrunk = append_lines(files, [], ["one"])
    entry = store.capture(files, history.complete_bytes(shrunk), len(shrunk))

    assert entry is not None
    stored = (store.root / entry.filename).read_bytes()
    assert history.decode(stored, shrunk) == ["one"]


def test_empty_history_is_skipped(tmp_path: Path) -> None:
    files, _ = make_files(tmp_path / "src", [])
    store = Store(tmp_path / "store")
    assert store.capture(files, 0, 0) is None


def test_manifest_survives_a_restart(tmp_path: Path) -> None:
    files, offsets = make_files(tmp_path / "src", ["persisted"])
    store = Store(tmp_path / "store")
    entry = store.capture(files, history.complete_bytes(offsets), len(offsets))
    assert entry is not None
    store.commit()

    reopened = Store(tmp_path / "store")
    assert reopened.total_bytes() == entry.cells_bytes


KONSOLE = Owner(4242, 99000)


def capture_owned(store: Store, files: history.HistoryFiles, offsets: list[int]):
    entry = store.capture(
        files, history.complete_bytes(offsets), len(offsets), owner=KONSOLE
    )
    assert entry is not None
    return entry


def test_prune_removes_a_pane_closed_in_a_live_konsole(tmp_path: Path) -> None:
    """The one case that should delete: the Konsole is still there, the pane is not."""
    files, offsets = make_files(tmp_path / "src", ["gone soon"])
    store = Store(tmp_path / "store")
    entry = capture_owned(store, files, offsets)

    result = store.prune({KONSOLE: set()}, referenced=set())

    assert result.closed == [files.cells_inode]
    assert not (store.root / entry.filename).exists()
    assert store.total_bytes() == 0


def test_prune_keeps_live_panes(tmp_path: Path) -> None:
    files, offsets = make_files(tmp_path / "src", ["still here"])
    store = Store(tmp_path / "store")
    entry = capture_owned(store, files, offsets)

    result = store.prune({KONSOLE: {files.cells_inode}}, referenced=set())

    assert result.removed == []
    assert result.live == [files.cells_inode]
    assert (store.root / entry.filename).exists()


def test_prune_keeps_panes_whose_konsole_exited(tmp_path: Path) -> None:
    """Closing Konsole must not cost you the scrollback you closed it with.

    This is the case that used to wipe the store: with no Konsole running,
    every entry looked like a closed pane.
    """
    files, offsets = make_files(tmp_path / "src", ["worth keeping"])
    store = Store(tmp_path / "store")
    entry = capture_owned(store, files, offsets)

    result = store.prune(owners={}, referenced=set())

    assert result.removed == []
    assert result.orphaned == [files.cells_inode]
    assert (store.root / entry.filename).exists()


def test_prune_does_not_trust_a_recycled_pid(tmp_path: Path) -> None:
    """A different process wearing the old pid is not the owner."""
    files, offsets = make_files(tmp_path / "src", ["worth keeping"])
    store = Store(tmp_path / "store")
    capture_owned(store, files, offsets)

    impostor = Owner(KONSOLE.pid, KONSOLE.start_time + 500)
    result = store.prune({impostor: set()}, referenced=set())

    assert result.removed == []
    assert result.orphaned == [files.cells_inode]


def test_orphan_expires_once_unreferenced_and_past_the_grace_period(
    tmp_path: Path,
) -> None:
    files, offsets = make_files(tmp_path / "src", ["stale"])
    store = Store(tmp_path / "store")
    entry = capture_owned(store, files, offsets)

    # First sight of the orphan starts its clock rather than deleting it.
    store.prune(owners={}, referenced=set(), grace=100, now=1000.0)
    assert (store.root / entry.filename).exists()

    result = store.prune(owners={}, referenced=set(), grace=100, now=1201.0)

    assert result.expired == [files.cells_inode]
    assert not (store.root / entry.filename).exists()


def test_a_referenced_orphan_outlives_the_grace_period(tmp_path: Path) -> None:
    """A snapshot pointing at an orphan means a restore still wants it."""
    files, offsets = make_files(tmp_path / "src", ["still wanted"])
    store = Store(tmp_path / "store")
    entry = capture_owned(store, files, offsets)

    store.prune(owners={}, referenced=set(), grace=100, now=1000.0)
    result = store.prune(
        owners={}, referenced={files.cells_inode}, grace=100, now=99999.0
    )

    assert result.removed == []
    assert (store.root / entry.filename).exists()


def test_reappearing_owner_clears_the_orphan_mark(tmp_path: Path) -> None:
    """A Konsole that comes back must not carry a half-spent grace period."""
    files, offsets = make_files(tmp_path / "src", ["back again"])
    store = Store(tmp_path / "store")
    capture_owned(store, files, offsets)

    store.prune(owners={}, referenced=set(), grace=100, now=1000.0)
    store.prune({KONSOLE: {files.cells_inode}}, referenced=set(), now=1050.0)
    result = store.prune(owners={}, referenced=set(), grace=100, now=1100.0)

    assert result.orphaned == [files.cells_inode]
    assert store._entries[files.cells_inode].orphaned_at == 1100.0


def test_entries_predating_ownership_are_not_culled(tmp_path: Path) -> None:
    """An upgrade must not read every existing entry as a closed pane."""
    files, offsets = make_files(tmp_path / "src", ["from an older kontinue"])
    store = Store(tmp_path / "store")
    entry = store.capture(files, history.complete_bytes(offsets), len(offsets))
    assert entry is not None and entry.owner is None

    result = store.prune({KONSOLE: {files.cells_inode}}, referenced=set())

    assert result.live == [files.cells_inode]
    assert (store.root / entry.filename).exists()


def test_capture_keeps_an_owner_a_later_capture_omits(tmp_path: Path) -> None:
    files, offsets = make_files(tmp_path / "src", ["owned"])
    store = Store(tmp_path / "store")
    capture_owned(store, files, offsets)

    offsets = append_lines(files, ["owned"], ["and more"])
    entry = store.capture(files, history.complete_bytes(offsets), len(offsets))

    assert entry is not None
    assert entry.owner == KONSOLE


def test_manifest_round_trips_ownership(tmp_path: Path) -> None:
    files, offsets = make_files(tmp_path / "src", ["owned"])
    store = Store(tmp_path / "store")
    capture_owned(store, files, offsets)
    store.commit()

    reopened = Store(tmp_path / "store")

    assert reopened._entries[files.cells_inode].owner == KONSOLE


def test_manifest_survives_fields_it_does_not_know(tmp_path: Path) -> None:
    """A manifest from a newer kontinue must not cost the whole store."""
    files, offsets = make_files(tmp_path / "src", ["owned"])
    store = Store(tmp_path / "store")
    capture_owned(store, files, offsets)
    store.commit()

    manifest = store.root / "manifest.json"
    payload = json.loads(manifest.read_text())
    payload["entries"][0]["something_from_the_future"] = True
    manifest.write_text(json.dumps(payload))

    reopened = Store(tmp_path / "store")

    assert reopened._entries[files.cells_inode].owner == KONSOLE


def test_capture_refuses_an_unsafe_destination(tmp_path: Path) -> None:
    files, offsets = make_files(tmp_path / "src", ["data"])
    store = Store(tmp_path / "store")
    store.root.mkdir(parents=True, exist_ok=True)

    decoy = store.root / f"{files.cells_inode}.cells"
    other = tmp_path / "other"
    other.write_bytes(b"someone else's file")
    os.link(other, decoy)

    with pytest.raises(history.UnsafeAccessError):
        store.capture(files, history.complete_bytes(offsets), len(offsets))


# -- privacy of what gets written ----------------------------------------


def test_write_private_is_owner_only(tmp_path: Path) -> None:
    """Everything this tool writes is a verbatim terminal record.

    The store honoured 0600 from the start; the snapshot did not, and it
    carries every pane's visible screen. Both go through this now.
    """
    target = tmp_path / "state" / "snapshot.json"
    write_private(target, '{"hello": "world"}')

    assert target.read_text() == '{"hello": "world"}'
    assert target.stat().st_mode & 0o777 == FILE_MODE
    assert target.parent.stat().st_mode & 0o777 == 0o700


def test_write_private_never_widens_an_existing_file(tmp_path: Path) -> None:
    target = tmp_path / "snapshot.json"
    target.write_text("old")
    os.chmod(target, 0o644)

    write_private(target, "new")

    assert target.read_text() == "new"
    assert target.stat().st_mode & 0o777 == FILE_MODE


def test_write_private_leaves_no_temporary_file_behind(tmp_path: Path) -> None:
    target = tmp_path / "snapshot.json"
    write_private(target, "content")
    assert [path.name for path in tmp_path.iterdir()] == ["snapshot.json"]


def test_write_private_keeps_the_old_file_when_writing_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An interrupted save must never leave a half-written snapshot in place."""
    from kontinue import scrollback as scrollback_mod

    target = tmp_path / "snapshot.json"
    write_private(target, "good")

    real_fdopen = os.fdopen

    class Failing:
        def __init__(self, fd: int) -> None:
            self._handle = real_fdopen(fd, "w")

        def __enter__(self) -> "Failing":
            return self

        def __exit__(self, *exc_info: object) -> bool:
            self._handle.close()
            return False

        def write(self, text: str) -> int:
            raise RuntimeError("disk went away")

    monkeypatch.setattr(scrollback_mod.os, "fdopen", lambda fd, mode: Failing(fd))

    with pytest.raises(RuntimeError):
        write_private(target, "bad")

    assert target.read_text() == "good"
    assert [path.name for path in tmp_path.iterdir()] == ["snapshot.json"]


def test_an_existing_loose_directory_is_tightened(tmp_path: Path) -> None:
    """mkdir's mode does nothing to a directory that is already there.

    A store created by an earlier version stays listable otherwise, and a
    listing names one file per pane and its size.
    """
    root = tmp_path / "scrollback"
    root.mkdir()
    # chmod, not mkdir's mode, which the umask filters: under a umask of 077
    # the directory would start out private and the test would prove nothing.
    root.chmod(0o755)
    assert root.stat().st_mode & 0o077

    ensure_private_dir(root)

    assert root.stat().st_mode & 0o777 == DIR_MODE


def test_tightening_leaves_owner_bits_alone(tmp_path: Path) -> None:
    root = tmp_path / "scrollback"
    root.mkdir(mode=0o750)
    ensure_private_dir(root)
    assert root.stat().st_mode & 0o700 == 0o700
    assert root.stat().st_mode & 0o077 == 0
