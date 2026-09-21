"""Keep a few older snapshots, so one bad save cannot cost you the arrangement.

The current snapshot is overwritten every time the timer fires, which makes it
an excellent record of the last forty-five seconds and a poor one of anything
else. Two situations turn that into real loss:

**A snapshot can catch a bad moment.** A window closed by accident, or a Konsole
caught mid-shutdown with half its tabs already gone, is saved as faithfully as
any other arrangement, and the good record is gone one tick later.

**A restore is not always wanted immediately.** Somebody who reboots, works in a
single terminal for a day and only then wants last week's arrangement back has
had it overwritten many times over.

So the snapshot being replaced is archived first, and the archive is what
``restore --generation`` reads. Two rules decide when:

- **On a gap**, so a generation is a meaningfully different arrangement rather
  than the same one forty-five seconds older. Timer saves would otherwise fill
  the whole archive inside four minutes.
- **On the last Konsole exiting**, unconditionally, because that snapshot is the
  final state of a finished session. That is the one somebody means by "the
  previous session", and it is worth keeping whatever the clock says.

Generations are pruned by count, oldest first. Their scrollback is spared by
:func:`referenced_inodes`, which the pruner unions into what a restore might
still want; without it the store would drop the history a generation points at
and leave a layout that rebuilds empty.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from pathlib import Path

from . import model
from .scrollback import DIR_MODE, write_private

log = logging.getLogger(__name__)

DIRECTORY_NAME = "generations"

# Enough to cover a working day of arrangements without the store growing
# without bound. Each generation costs a JSON file; the scrollback it points at
# is shared with the current snapshot until that moves on.
DEFAULT_KEEP = 5

# How far apart two generations have to be. An hour keeps the archive spanning
# a useful stretch of time rather than the last few minutes.
DEFAULT_MIN_GAP_SECONDS = 60 * 60

_STAMP_RE = re.compile(r"^snapshot-(\d{8}T\d{6})\.json$")


def directory(state_path: Path) -> Path:
    """Where generations live, beside the snapshot they came from."""
    return state_path.parent / DIRECTORY_NAME


def _stamp(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%S")


def _parse_stamp(path: Path) -> datetime | None:
    match = _STAMP_RE.match(path.name)
    if match is None:
        return None
    try:
        return datetime.strptime(match.group(1), "%Y%m%dT%H%M%S").replace(
            tzinfo=timezone.utc
        )
    except ValueError:
        return None


def listing(state_path: Path) -> list[Path]:
    """Every retained generation, newest first.

    Ordered by the timestamp in the filename rather than by mtime, because the
    name records when the arrangement was captured and the mtime only records
    when it was archived.
    """
    root = directory(state_path)
    if not root.is_dir():
        return []
    stamped = [(moment, path) for path in root.glob("snapshot-*.json")
               if (moment := _parse_stamp(path)) is not None]
    return [path for _, path in sorted(stamped, key=lambda item: item[0], reverse=True)]


def retain(
    state_path: Path,
    keep: int = DEFAULT_KEEP,
    min_gap: float = DEFAULT_MIN_GAP_SECONDS,
    force: bool = False,
) -> Path | None:
    """Archive the snapshot at ``state_path`` before something replaces it.

    Returns the generation written, or ``None`` when there was nothing to keep
    or the newest generation is too recent. ``force`` skips the gap rule, for
    the session-end case where the age of the snapshot says nothing about how
    much it is worth keeping.

    Never raises for an unreadable or absent snapshot: this runs on the way to
    saving a new one, and failing to archive the old one must not cost the save.
    """
    try:
        text = state_path.read_text()
        current = model.Snapshot.loads(text)
    except FileNotFoundError:
        return None
    except (OSError, ValueError, KeyError) as exc:
        # ValueError covers both SchemaError and a JSONDecodeError from a file
        # truncated by a full disk. Either way this runs on the way to saving a
        # new snapshot, and failing to archive the old one must not cost the save.
        log.debug("not archiving the current snapshot: %s", exc)
        return None

    try:
        captured_at = datetime.fromisoformat(current.captured_at)
    except ValueError:
        log.debug("snapshot has an unreadable timestamp; not archiving it")
        return None
    if captured_at.tzinfo is None:
        captured_at = captured_at.replace(tzinfo=timezone.utc)

    existing = listing(state_path)
    if existing:
        newest = _parse_stamp(existing[0])
        if newest is not None:
            gap = (captured_at - newest).total_seconds()
            if gap <= 0:
                # Already archived, or the clock went backwards. Either way
                # there is nothing new to keep.
                return None
            if not force and gap < min_gap:
                return None

    target = directory(state_path) / f"snapshot-{_stamp(captured_at)}.json"
    if target.exists():
        return None
    target.parent.mkdir(parents=True, exist_ok=True, mode=DIR_MODE)
    write_private(target, text)
    log.debug("archived generation %s", target.name)

    trim(state_path, keep=keep)
    return target


def trim(state_path: Path, keep: int = DEFAULT_KEEP) -> list[Path]:
    """Delete the oldest generations beyond ``keep``. Returns what went."""
    if keep < 0:
        return []
    removed = []
    for path in listing(state_path)[keep:]:
        try:
            path.unlink()
            removed.append(path)
        except OSError as exc:
            log.warning("could not remove generation %s: %s", path.name, exc)
    return removed


def load(state_path: Path, index: int) -> tuple[model.Snapshot, Path]:
    """Read generation ``index``, where 1 is the most recent one.

    Index 0 is not a generation: it is the current snapshot, which the caller
    already knows how to read.
    """
    if index < 1:
        raise ValueError("generation numbers start at 1")
    available = listing(state_path)
    if index > len(available):
        raise FileNotFoundError(
            f"there is no generation {index}; {len(available)} kept"
        )
    path = available[index - 1]
    return model.Snapshot.loads(path.read_text()), path


def referenced_inodes(state_path: Path) -> set[int]:
    """Every scrollback inode any retained generation still points at.

    Unioned into what the pruner spares. A generation whose scrollback has been
    collected still restores, but rebuilds empty panes, which is the failure
    this exists to prevent.
    """
    from . import snapshot as snapshot_mod

    inodes: set[int] = set()
    for path in listing(state_path):
        try:
            loaded = model.Snapshot.loads(path.read_text())
        except (OSError, ValueError, KeyError) as exc:
            # One corrupt generation must not stop the others being spared, and
            # must never propagate: this is called from the save path.
            log.debug("ignoring unreadable generation %s: %s", path.name, exc)
            continue
        inodes |= snapshot_mod.live_scrollback_inodes(loaded)
    return inodes


def describe(snapshot: model.Snapshot, limit: int = 6) -> str:
    """Say where a snapshot's panes were, well enough to recognise it.

    Pane counts alone do not tell one working day from another, but the
    directories the panes were in usually do, as do any tabs the user named.
    """
    counts: dict[str, int] = {}
    named: list[str] = []
    for pane in snapshot.panes():
        place = (Path(pane.cwd).name or pane.cwd) if pane.cwd else "?"
        counts[place] = counts.get(place, 0) + 1
        title = pane.local_title_format
        # A title with no placeholders is one the user typed in; the default
        # formats say nothing a directory does not already say.
        if title and "%" not in title and title not in named:
            named.append(title)

    places = sorted(counts.items(), key=lambda item: -item[1])
    parts = [f"{place} ({count})" if count > 1 else place for place, count in places[:limit]]
    if len(places) > limit:
        parts.append(f"+{len(places) - limit} more")
    text = ", ".join(parts)
    if named:
        text += "; named: " + ", ".join(named[:limit])
    return text


# How recent a generation has to be for status to hold it up against the
# current snapshot. Older ones are likelier a deliberate change than a loss.
OUTGROWN_WINDOW_SECONDS = 3 * 24 * 60 * 60


def outgrown(
    state_path: Path,
    current: model.Snapshot | None,
    window: float = OUTGROWN_WINDOW_SECONDS,
) -> tuple[int, model.Snapshot] | None:
    """A recent generation much bigger than the current snapshot, if any.

    That shape is what a lost session looks like: something saved a small new
    arrangement over a large one. Closing a tab or two is not it, so this wants
    at least twice the panes and at least three more. Returns the generation's
    number, as ``restore --generation`` takes it, and the generation.

    The newest one that qualifies wins rather than the biggest, because the
    lost session is the last large one, not whichever was largest that day.
    """
    current_panes = current.pane_count() if current is not None else 0
    reference = _captured(current) if current is not None else None

    for index, path in enumerate(listing(state_path), start=1):
        try:
            loaded = model.Snapshot.loads(path.read_text())
        except (OSError, ValueError, KeyError):
            continue
        moment = _captured(loaded)
        if reference is not None and moment is not None:
            if (reference - moment).total_seconds() > window:
                continue
        panes = loaded.pane_count()
        if panes >= 2 * current_panes and panes - current_panes >= 3:
            return index, loaded
    return None


def _captured(snapshot: model.Snapshot) -> datetime | None:
    try:
        moment = datetime.fromisoformat(snapshot.captured_at)
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)
