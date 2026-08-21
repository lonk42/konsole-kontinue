"""A lock held for the length of a restore.

Restoring and snapshotting must not overlap. A restore builds its tabs one at a
time, so a snapshot taken while one is in progress records a half-built Konsole,
and because that snapshot overwrites the good one, the next restore rebuilds the
half. Nothing about the Konsole itself says a restore is underway, so the fact
has to be published somewhere both sides can see it.

``flock`` is used rather than a pid file because the kernel releases it when the
holder exits, however it exits. A restore killed halfway leaves no stale lock to
clear by hand, which matters when the thing that would have to clear it is an
autostarted watcher that nobody is watching.
"""

from __future__ import annotations

import errno
import fcntl
import logging
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

log = logging.getLogger(__name__)

LOCK_NAME = "restore.lock"


class Busy(RuntimeError):
    """The lock is held by someone else."""


@contextmanager
def held(path: Path) -> Iterator[None]:
    """Hold the lock for the duration of the block, or raise :class:`Busy`."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno in (errno.EACCES, errno.EAGAIN):
                raise Busy(f"another restore is in progress ({path})") from exc
            raise
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def is_held(path: Path) -> bool:
    """Whether a restore is in progress, without waiting for one that is.

    Taking the lock and dropping it again is the only way to ask. The answer is
    stale the moment it is given, which is why this guards a snapshot that can
    simply be skipped and retried on the next tick, and never anything that
    must be exactly right.

    ``flock`` is held against the open file description rather than the
    process, so this reports a lock taken by :func:`held` in this same process
    as held, which is what the watcher needs when it restores.
    """
    if not path.exists():
        return False
    try:
        fd = os.open(path, os.O_RDWR)
    except OSError:
        return False
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return True
    else:
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    finally:
        os.close(fd)
