"""Tests for the restore lock.

The lock exists to stop a snapshot being taken of a half-rebuilt Konsole, so
what matters is that it is visible to a second asker and that it never survives
the block that took it.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from kontinue import lock


def test_lock_is_visible_while_held(tmp_path: Path) -> None:
    path = tmp_path / lock.LOCK_NAME

    assert not lock.is_held(path)
    with lock.held(path):
        assert lock.is_held(path)
    assert not lock.is_held(path)


def test_a_second_holder_is_refused(tmp_path: Path) -> None:
    path = tmp_path / lock.LOCK_NAME

    with lock.held(path):
        with pytest.raises(lock.Busy):
            with lock.held(path):
                pass


def test_the_lock_is_released_when_the_block_raises(tmp_path: Path) -> None:
    """A restore that fails halfway must not wedge the watcher for good."""
    path = tmp_path / lock.LOCK_NAME

    with pytest.raises(ValueError):
        with lock.held(path):
            raise ValueError("restore blew up")

    assert not lock.is_held(path)


def test_an_absent_lock_file_is_not_held(tmp_path: Path) -> None:
    assert not lock.is_held(tmp_path / "never-created.lock")


def test_the_lock_file_is_owner_only(tmp_path: Path) -> None:
    """It sits beside the snapshot, which is private."""
    path = tmp_path / lock.LOCK_NAME

    with lock.held(path):
        assert os.stat(path).st_mode & 0o777 == 0o600


def test_holding_creates_the_directory(tmp_path: Path) -> None:
    """The watcher can take the lock before anything has been saved."""
    path = tmp_path / "state" / lock.LOCK_NAME

    with lock.held(path):
        assert path.exists()
