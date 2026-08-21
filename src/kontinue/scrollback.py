"""Persist pane scrollback to disk.

Scrollback is captured whole and never trimmed, which makes the naive approach
of copying every file on every run expensive: a long-lived pane's cell file
reaches tens of megabytes, and a snapshot on a timer would rewrite all of it
each time.

Konsole only ever appends to a history file while a pane is alive, so a copy can
be incremental. The store remembers each file's size and a hash of its head; if
the file has grown and the head is unchanged, only the new tail is copied. Two
things break that assumption and force a full recopy, both detected rather than
assumed:

* ``HistoryScrollFile::removeCells()`` truncates the files when Konsole reflows
  a resized pane, so the file can shrink.
* Clearing a pane's history resets the files, so a file of the same size can
  hold entirely different content.

Files are keyed by the inode of their cell file. Session ids are reused by
Konsole as soon as a session closes, whereas the inode is stable for the life of
the pane, which is exactly the lifetime of the scrollback.

Each entry also records which Konsole owned it, because "this pane is gone" has
two meanings that deserve opposite treatment. A pane whose session vanished
while its Konsole is *still running* was closed deliberately, and its scrollback
follows it. A pane whose whole Konsole exited did not go anywhere: it is exactly
what a later restore needs. Ownership is what tells the two apart, so it is
recorded at capture time rather than inferred later. See :meth:`Store.prune`.

Everything written here is created ``0600``. A saved scrollback is a verbatim
record of a terminal, including anything typed or printed in it.
"""

from __future__ import annotations

import dataclasses
import errno
import hashlib
import json
import logging
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .history import HistoryFiles, assert_safe_destination, open_readonly

log = logging.getLogger(__name__)

# Enough of the head to notice a file being cleared and refilled.
HEAD_SAMPLE_BYTES = 4096

# Snapshot files and the directory holding them are owner-only: scrollback is a
# verbatim terminal record and routinely contains credentials.
FILE_MODE = 0o600
DIR_MODE = 0o700

MANIFEST_NAME = "manifest.json"


def ensure_private_dir(path: Path) -> None:
    """Create ``path`` owner-only, and tighten it if it is not already.

    The mode argument to ``mkdir`` does nothing for a directory that already
    exists, and a store created by an earlier version, or under a looser umask,
    is left listable. The files inside are ``0600`` either way, but a directory
    listing of a scrollback store names one file per pane and says how big each
    is, so the documented promise is only kept if this tightens what it finds.
    Only group and other bits are cleared; the owner's are left alone.
    """
    path.mkdir(parents=True, exist_ok=True, mode=DIR_MODE)
    try:
        current = path.stat().st_mode & 0o777
    except OSError:  # pragma: no cover - stat failing here means bigger trouble
        return
    if current & 0o077:
        try:
            os.chmod(path, current & ~0o077)
        except OSError as exc:
            log.debug("could not tighten %s: %s", path, exc)


def write_private(path: Path, text: str) -> None:
    """Write text to ``path`` atomically, never readable by anyone else.

    The file is created with :data:`FILE_MODE` already set rather than chmodded
    afterwards, because a snapshot is a verbatim terminal record and a window
    where it sits at the default umask is a window where it can be read.
    Written via a temporary file in the same directory so an interrupted write
    cannot leave a half-written file where a good one used to be.
    """
    ensure_private_dir(path.parent)
    temp = path.with_suffix(path.suffix + ".tmp")
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, FILE_MODE)
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(text)
    except BaseException:
        temp.unlink(missing_ok=True)
        raise
    temp.replace(path)

# Read size for the userspace copy fallback.
COPY_CHUNK_BYTES = 4 * 1024 * 1024

# How long an orphan outlives the Konsole it belonged to. The orphan is the
# safety net for a restore that has not happened yet, so this only has to
# outlast the gap between losing a Konsole and rebuilding it. A week covers
# forgetting over a weekend without letting the store grow without bound.
ORPHAN_GRACE_SECONDS = 7 * 24 * 60 * 60


class Owner(tuple):
    """Which Konsole process a stored scrollback belongs to.

    A bare pid is not enough. Konsole exits, the pid is recycled by something
    unrelated, and an orphan would then look owned by a live process, which
    would get it culled as a closed pane. Pairing the pid with its start time
    from ``/proc`` makes the identity unique for as long as it matters.
    """

    __slots__ = ()

    def __new__(cls, pid: int, start_time: int) -> "Owner":
        return super().__new__(cls, (int(pid), int(start_time)))

    @property
    def pid(self) -> int:
        return self[0]

    @property
    def start_time(self) -> int:
        return self[1]


@dataclass
class PruneResult:
    """What a prune did, split by why.

    The distinction is the point of the whole exercise, so it is reported
    rather than summed: ``closed`` is scrollback deliberately discarded because
    its pane was closed, while ``orphaned`` is scrollback deliberately kept
    because its Konsole exited. Collapsing them into one number would hide
    which of the two rules was doing the work.
    """

    live: list[int] = field(default_factory=list)
    closed: list[int] = field(default_factory=list)
    orphaned: list[int] = field(default_factory=list)
    expired: list[int] = field(default_factory=list)

    @property
    def removed(self) -> list[int]:
        return self.closed + self.expired


@dataclass
class Entry:
    """What is known about one pane's stored scrollback."""

    inode: int
    cells_bytes: int
    head_hash: str
    lines: int
    # Absent on entries written before ownership was recorded. Such an entry is
    # treated as unowned, which is the cautious reading: unowned never means
    # "cull", only "keep and let the next capture claim it".
    owner_pid: int | None = None
    owner_start_time: int | None = None
    # When the owning Konsole was first observed to be gone. The grace period
    # runs from here, so it measures time since the Konsole died rather than
    # time since anything last looked.
    orphaned_at: float | None = None

    @property
    def filename(self) -> str:
        return f"{self.inode}.cells"

    @property
    def index_filename(self) -> str:
        return f"{self.inode}.index"

    @property
    def owner(self) -> Owner | None:
        if self.owner_pid is None or self.owner_start_time is None:
            return None
        return Owner(self.owner_pid, self.owner_start_time)


def _head_hash(path: str, limit: int = HEAD_SAMPLE_BYTES) -> str:
    fd = open_readonly(path)
    try:
        return hashlib.blake2b(os.read(fd, limit), digest_size=16).hexdigest()
    finally:
        os.close(fd)


class Store:
    """A directory of captured scrollback, plus the manifest describing it."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self._manifest_path = root / MANIFEST_NAME
        self._entries: dict[int, Entry] = {}
        self._load()

    # -- manifest --------------------------------------------------------

    def _load(self) -> None:
        try:
            raw = json.loads(self._manifest_path.read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            return
        # Take only the fields this version knows about. A manifest written by
        # an older kontinue is missing some, and one written by a newer kontinue
        # may carry extras; neither is a reason to lose the whole store.
        known = {f.name for f in dataclasses.fields(Entry)}
        for item in raw.get("entries", []):
            entry = Entry(**{k: v for k, v in item.items() if k in known})
            self._entries[entry.inode] = entry

    def _save(self) -> None:
        payload = {"entries": [asdict(entry) for entry in self._entries.values()]}
        write_private(self._manifest_path, json.dumps(payload, indent=2) + "\n")

    # -- capture ---------------------------------------------------------

    def capture(
        self,
        files: HistoryFiles,
        complete_bytes: int,
        lines: int,
        owner: Owner | None = None,
    ) -> Entry | None:
        """Copy a pane's cells up to ``complete_bytes``, appending where possible.

        ``complete_bytes`` comes from the index and covers only fully written
        lines, so a copy can never include a line Konsole is midway through.

        ``owner`` is the Konsole holding the pane open. Capturing is proof the
        pane is alive, so it also clears any orphan mark left by an earlier run.
        """
        if complete_bytes <= 0:
            return None

        ensure_private_dir(self.root)
        entry = Entry(files.cells_inode, 0, "", 0)
        destination = self.root / entry.filename
        assert_safe_destination(destination)

        previous = self._entries.get(files.cells_inode)
        head = _head_hash(files.cells)
        resume_at = 0

        if previous is not None and previous.head_hash == head:
            if previous.cells_bytes <= complete_bytes and destination.exists():
                if destination.stat().st_size == previous.cells_bytes:
                    resume_at = previous.cells_bytes
                else:
                    log.debug("stored copy for inode %s is the wrong size; recopying",
                              files.cells_inode)

        if resume_at >= complete_bytes:
            # Nothing new since last time; keep the recorded line count fresh.
            self._copy_index(files, entry)
            return self._record(files.cells_inode, resume_at, head, lines, owner)

        copied = self._copy_range(files.cells, destination, resume_at, complete_bytes)
        self._copy_index(files, entry)
        return self._record(files.cells_inode, resume_at + copied, head, lines, owner)

    def _record(
        self, inode: int, cells_bytes: int, head: str, lines: int, owner: Owner | None
    ) -> Entry:
        # A caller that does not know the owner must not erase one already
        # recorded: forgetting an owner turns a live pane into an orphan.
        previous = self._entries.get(inode)
        if owner is None and previous is not None:
            owner = previous.owner

        record = Entry(
            inode=inode,
            cells_bytes=cells_bytes,
            head_hash=head,
            lines=lines,
            owner_pid=owner.pid if owner is not None else None,
            owner_start_time=owner.start_time if owner is not None else None,
            orphaned_at=None,
        )
        self._entries[inode] = record
        return record

    def _copy_index(self, files: HistoryFiles, entry: Entry) -> None:
        """Keep the line offsets alongside the cells.

        Decoding needs them, and without them a stored cell file is an
        undifferentiated run of characters. The index is 8 bytes per line
        against 16 bytes per character, so copying it whole costs little.
        """
        destination = self.root / entry.index_filename
        assert_safe_destination(destination)
        self._copy_range(files.index, destination, 0, os.stat(files.index).st_size)

    def _copy_range(self, source: str, destination: Path, start: int, end: int) -> int:
        """Copy ``source[start:end]`` onto the end of ``destination``.

        ``copy_file_range`` keeps the bytes in the kernel, which matters when a
        long-lived pane's first capture is tens of megabytes. It refuses to work
        across filesystems, and the store does not have to sit on the same one
        as Konsole's cache, so a plain read and write loop stands behind it.
        """
        flags = os.O_WRONLY | os.O_CREAT
        if start == 0:
            flags |= os.O_TRUNC

        source_fd = open_readonly(source)
        dest_fd = os.open(destination, flags, FILE_MODE)
        try:
            os.chmod(destination, FILE_MODE)
            if start:
                os.lseek(dest_fd, start, os.SEEK_SET)
            os.lseek(source_fd, start, os.SEEK_SET)

            remaining = end - start
            try:
                return self._copy_in_kernel(source_fd, dest_fd, start, remaining)
            except OSError as exc:
                if exc.errno not in (errno.EXDEV, errno.ENOSYS, errno.EINVAL):
                    raise
                log.debug("copy_file_range unavailable here (%s); copying through userspace",
                          exc.strerror)
                os.lseek(source_fd, start, os.SEEK_SET)
                os.lseek(dest_fd, start, os.SEEK_SET)
                return self._copy_in_userspace(source_fd, dest_fd, remaining)
        finally:
            os.close(dest_fd)
            os.close(source_fd)

    @staticmethod
    def _copy_in_kernel(source_fd: int, dest_fd: int, start: int, remaining: int) -> int:
        copied = 0
        offset = start
        while copied < remaining:
            moved = os.copy_file_range(source_fd, dest_fd, remaining - copied, offset_src=offset)
            if moved == 0:
                break
            copied += moved
            offset += moved
        return copied

    @staticmethod
    def _copy_in_userspace(source_fd: int, dest_fd: int, remaining: int) -> int:
        copied = 0
        while copied < remaining:
            chunk = os.read(source_fd, min(COPY_CHUNK_BYTES, remaining - copied))
            if not chunk:
                break
            written = 0
            while written < len(chunk):
                written += os.write(dest_fd, chunk[written:])
            copied += len(chunk)
        return copied

    # -- housekeeping ----------------------------------------------------

    def prune(
        self,
        owners: dict[Owner, set[int]],
        referenced: set[int],
        grace: float = ORPHAN_GRACE_SECONDS,
        now: float | None = None,
    ) -> "PruneResult":
        """Drop stored scrollback that its pane will not come back to.

        ``owners`` maps each *running* Konsole to the inodes it currently holds
        open, and ``referenced`` is every inode some snapshot still points at.

        The rule the two arguments exist to express: an inode is culled only
        when its owning Konsole is running and no longer holds it, which is
        exactly a pane closed by hand. When the owning Konsole is gone the panes
        went with it, so the scrollback is kept as the material for the next
        restore. Absence from ``owners`` is never on its own a reason to delete,
        because the commonest way for an owner to be absent is that the user
        shut Konsole down.

        Orphans cannot be kept forever, so one is collected once it is both
        unreferenced by any snapshot, meaning no restore will ever want it, and
        older than ``grace``. Both conditions are needed: a restore that
        rebuilds only some panes leaves the rest unreferenced immediately, and
        the grace period is what stops that quietly discarding them.
        """
        now = time.time() if now is None else now
        live_anywhere = {inode for inodes in owners.values() for inode in inodes}
        result = PruneResult()

        for inode in list(self._entries):
            entry = self._entries[inode]
            owner = entry.owner

            if owner is not None and owner in owners:
                if inode in owners[owner]:
                    entry.orphaned_at = None
                    result.live.append(inode)
                    continue
                # The Konsole is still there and has let this pane go.
                self._discard(inode)
                result.closed.append(inode)
                continue

            if owner is None and inode in live_anywhere:
                # Written before ownership was recorded, but plainly still open.
                # Leave it be and let the next capture claim it.
                entry.orphaned_at = None
                result.live.append(inode)
                continue

            if entry.orphaned_at is None:
                entry.orphaned_at = now
            if inode not in referenced and now - entry.orphaned_at > grace:
                self._discard(inode)
                result.expired.append(inode)
                continue
            result.orphaned.append(inode)

        return result

    def _discard(self, inode: int) -> None:
        stored = self._entries.pop(inode)
        (self.root / stored.filename).unlink(missing_ok=True)
        (self.root / stored.index_filename).unlink(missing_ok=True)

    def read_lines(self, inode: int, colour: bool = False) -> list[str]:
        """Decode a stored scrollback back into lines.

        With ``colour`` the lines carry the SGR sequences needed to reproduce
        their original appearance, which is what replaying into a pane wants.
        """
        from . import history

        cells_path = self.root / f"{inode}.cells"
        index_path = self.root / f"{inode}.index"
        if not (cells_path.exists() and index_path.exists()):
            return []

        offsets = history.read_index(str(index_path))
        data = cells_path.read_bytes()
        # The index can describe more lines than were copied if the pane grew
        # between the two reads, so trim it to what is actually stored.
        usable = [offset for offset in offsets if offset <= len(data)]
        if colour:
            return history.decode_ansi(data, usable)
        return history.decode(data, usable)

    def total_bytes(self) -> int:
        return sum(entry.cells_bytes for entry in self._entries.values())

    def commit(self) -> None:
        self._save()
