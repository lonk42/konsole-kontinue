"""Read a pane's scrollback out of Konsole's history files.

Konsole stores an unlimited scrollback in three files per pane. It creates them
in a cache directory, opens them, and immediately unlinks them, so they have no
name and the kernel frees the space when the pane closes. The open descriptors
stay in Konsole's file table, which makes the data reachable through
``/proc/<konsole-pid>/fd/<n>`` for as long as the pane lives.

The layout is described in ``HistoryScrollFile.cpp``::

    The history scroll makes a Row(Row(Cell)) from two history buffers. The
    index buffer contains start of line positions which refer to the cells
    buffer. Note that index[0] addresses the second line (line #1), while the
    first line (line #0) starts at 0 in cells.

So for line ``i``:

* ``start = 0`` when ``i == 0``, otherwise ``index[i - 1]``
* ``end = index[i]``

and ``addLine()`` appends the index entry *after* ``addCells()`` has written the
characters, which makes the index a conservative bound. Reading only as far as
the last index entry can never catch a half-written line.

**Everything in this module is read-only, and must stay that way.** The
descriptors reached through ``/proc`` point at the live inode Konsole is still
writing to. Opening one with ``O_TRUNC``, or linking it into the filesystem and
then opening that name for writing, destroys the scrollback of a running pane.
:func:`open_readonly` is the only way this package opens those paths.
"""

from __future__ import annotations

import os
import re
import struct
from dataclasses import dataclass
from math import gcd
from pathlib import Path
from typing import NamedTuple

# sizeof(qint64), one entry per line, holding that line's end offset in cells.
INDEX_ENTRY_SIZE = 8

# sizeof(LineProperty). Verified against live files: an index of N entries is
# always paired with a lineflags file of N * 6 bytes.
LINE_PROPERTY_SIZE = 6

# sizeof(Character). Not a stable ABI, so it is confirmed at read time by
# :func:`_derive_stride` rather than trusted.
EXPECTED_CHARACTER_SIZE = 16

# Plausible sizes for Character across versions, used to sanity-check the value
# derived from the file itself.
CANDIDATE_CHARACTER_SIZES = (12, 16, 20, 24, 32)

# A decoded line wider than this means the stride is wrong and we are reading
# structure as text.
MAX_PLAUSIBLE_COLUMNS = 4096

# Konsole's cache files are named "#<n>" and unlinked, so the /proc symlink
# reads as "/path/#12345 (deleted)".
HISTORY_PATH_RE = re.compile(r"/konsole/#\d+ \(deleted\)$")


class HistoryFormatError(RuntimeError):
    """The history files do not match the layout this version understands."""


class UnsafeAccessError(RuntimeError):
    """Raised when something tries to open a live history file for writing."""


def open_readonly(path: str | os.PathLike[str]) -> int:
    """Open a file descriptor that cannot modify what it points at.

    The only opener used for anything under ``/proc``. Callers get a raw fd so
    that :func:`os.copy_file_range` can be used against it.
    """
    return os.open(os.fspath(path), os.O_RDONLY | os.O_NOCTTY)


def assert_safe_destination(path: Path) -> None:
    """Refuse to write to a path that could be a live history file.

    Three ways that can happen, and the first is easy to get wrong: resolving a
    ``/proc/<pid>/fd/<n>`` path follows the magic link *out* of ``/proc`` to the
    file it points at, so a check on the resolved path alone never fires. The
    path as given has to be tested too.

    The link count catches the other two. A file this store owns has exactly one
    link. Zero means an unlinked inode kept alive by someone else's open
    descriptor, which is precisely what a live pane's scrollback is. More than
    one means a hard link, and Konsole's unlinked history inodes *can* be linked
    back into the filesystem, which turns an innocent looking filename into a
    running pane's history.
    """
    absolute = Path(os.path.abspath(path))
    resolved = path.resolve()

    for candidate in (absolute, resolved):
        if candidate.is_relative_to("/proc"):
            raise UnsafeAccessError(f"refusing to write under /proc: {candidate}")

    if not resolved.exists():
        return

    links = resolved.stat().st_nlink
    if links != 1:
        raise UnsafeAccessError(
            f"refusing to write to {resolved}: it has {links} links, so it is "
            "either a hard link or an unlinked file something else is still using"
        )


@dataclass(frozen=True)
class HistoryFiles:
    """The three files backing one pane's scrollback, as ``/proc`` paths."""

    index: str
    cells: str
    flags: str
    cells_inode: int

    def sizes(self) -> tuple[int, int, int]:
        return tuple(os.stat(p).st_size for p in (self.index, self.cells, self.flags))


def locate(konsole_pid: int, pts: str) -> HistoryFiles | None:
    """Find the history files belonging to the pane running on ``pts``.

    Konsole's descriptor table is laid out per pane: the pty master, then the
    slave, then that pane's three history files, in creation order. Walking
    forward from the pane's ``/dev/pts/N`` entry to the next three unlinked
    cache files therefore identifies them, and stopping at the following pty
    keeps one pane's files from being attributed to the next.

    Returns ``None`` for a pane with no file-backed history, which is every pane
    whose profile uses a fixed scrollback size rather than an unlimited one.
    """
    fd_dir = Path(f"/proc/{konsole_pid}/fd")
    try:
        entries = sorted((int(e.name) for e in fd_dir.iterdir()), key=int)
    except OSError:
        return None

    targets: list[tuple[int, str]] = []
    for fd in entries:
        try:
            targets.append((fd, os.readlink(fd_dir / str(fd))))
        except OSError:
            continue

    start = next((i for i, (_, t) in enumerate(targets) if t == pts), None)
    if start is None:
        return None

    found: list[str] = []
    for fd, target in targets[start + 1 :]:
        if HISTORY_PATH_RE.search(target):
            found.append(str(fd_dir / str(fd)))
            if len(found) == 3:
                break
        elif target.startswith("/dev/pts/") and not target.endswith("/ptmx"):
            break

    if len(found) != 3:
        return None

    index, cells, flags = found
    try:
        return HistoryFiles(index, cells, flags, os.stat(cells).st_ino)
    except OSError:
        return None


def _plausibility(sample: bytes, stride: int) -> float:
    """Fraction of cells at ``stride`` whose first field looks like a character.

    Reading the grid at the wrong stride does not fail, it yields text-shaped
    rubbish, so the check has to be about content rather than structure.
    """
    cells = len(sample) // stride
    if cells == 0:
        return 0.0

    good = 0
    for position in range(0, cells * stride, stride):
        codepoint = struct.unpack_from("<I", sample, position)[0]
        # Terminal output is overwhelmingly ASCII, and every cell of a grid row
        # past the text is a space, so a correct stride scores very high.
        if codepoint == 32 or 33 <= codepoint <= 126:
            good += 1
        elif 0xA0 <= codepoint <= 0x2FFF or 0x1F300 <= codepoint <= 0x1FAFF:
            good += 1

    return good / cells


def _derive_stride(offsets: list[int], sample: bytes) -> int:
    """Work out sizeof(Character) from the data rather than trusting a constant.

    ``Character`` is a private C++ struct with no ABI guarantee, so its size can
    change between Konsole releases. Every line occupies a whole number of
    characters, which narrows the candidates, but a run of equal-width lines
    makes the greatest common divisor a multiple of the real size rather than
    the size itself. Scoring how much like text each candidate decodes to
    settles it, and refusing a poor best score means a format change surfaces as
    an error instead of as corrupted output.
    """
    spans = [b - a for a, b in zip([0, *offsets], offsets) if b > a]
    divisor = 0
    for span in spans:
        divisor = gcd(divisor, span)

    candidates = [
        size
        for size in CANDIDATE_CHARACTER_SIZES
        if (divisor == 0 or divisor % size == 0)
        and (not spans or max(spans) // size <= MAX_PLAUSIBLE_COLUMNS)
    ]
    if not candidates:
        raise HistoryFormatError(
            f"line lengths share a divisor of {divisor} bytes, which matches no "
            "known Konsole character size; the history format has probably changed"
        )

    # Any multiple of the true size also decodes as text, because it samples
    # every Nth cell, so a tie must break towards the smallest candidate.
    scored = sorted(
        ((_plausibility(sample, size), size) for size in candidates),
        key=lambda pair: (-pair[0], pair[1]),
    )
    best_score, best_size = scored[0]

    if best_score < 0.75:
        raise HistoryFormatError(
            f"no candidate character size decodes as text (best was {best_size} "
            f"bytes at {best_score:.0%} plausible); the history format has "
            "probably changed"
        )

    return best_size


def read_index(path: str) -> list[int]:
    """Line end offsets into the cells file, one per complete line."""
    fd = open_readonly(path)
    try:
        data = os.read(fd, os.fstat(fd).st_size)
    finally:
        os.close(fd)

    count = len(data) // INDEX_ENTRY_SIZE
    return list(struct.unpack_from(f"<{count}q", data, 0)) if count else []


def complete_bytes(offsets: list[int]) -> int:
    """How much of the cells file belongs to lines that are fully written."""
    return offsets[-1] if offsets else 0


def decode(cells: bytes, offsets: list[int], stride: int | None = None) -> list[str]:
    """Turn the cell grid into text, one string per scrollback line.

    Only the character is decoded. Each cell also carries a rendition, a
    foreground and a background colour, which a future revision can re-emit as
    SGR sequences so a restored pane keeps its colours.
    """
    if not offsets:
        return []

    stride = stride if stride is not None else _derive_stride(offsets, cells[:65536])
    lines = []
    start = 0

    for end in offsets:
        if end > len(cells):
            break
        chunk = cells[start:end]
        chars = []
        for position in range(0, len(chunk) - stride + 1, stride):
            codepoint = struct.unpack_from("<I", chunk, position)[0]
            # Konsole stores multi-codepoint graphemes as a hash into a table
            # that lives only in its memory, flagged by a high rendition bit.
            # Those decode as an implausible codepoint; keep a placeholder so
            # column positions still line up.
            chars.append(chr(codepoint) if 32 <= codepoint <= 0x10FFFF else "�")
        lines.append("".join(chars).rstrip())
        start = end

    return lines


def read_text(files: HistoryFiles) -> list[str]:
    """Read one pane's whole scrollback as lines of text."""
    offsets = read_index(files.index)
    limit = complete_bytes(offsets)
    if limit == 0:
        return []

    fd = open_readonly(files.cells)
    try:
        data = os.read(fd, limit)
    finally:
        os.close(fd)

    return decode(data, offsets)


# Character is 16 bytes laid out as:
#   0..3   character   uint32, a UTF-32 codepoint
#   4..5   rendition   uint16, the RE_* flags below
#   6..9   foreground  CharacterColor
#   10..13 background  CharacterColor
#   14..15 flags       uint16, ExtraFlags
RENDITION_OFFSET = 4
FOREGROUND_OFFSET = 6
BACKGROUND_OFFSET = 10

# CharacterColor is colour space, then three bytes whose meaning depends on it.
COLOR_SPACE_UNDEFINED = 0
COLOR_SPACE_DEFAULT = 1
COLOR_SPACE_SYSTEM = 2
COLOR_SPACE_256 = 3
COLOR_SPACE_RGB = 4

RE_BOLD = 1 << 0
RE_BLINK = 1 << 1
RE_REVERSE = 1 << 3
RE_ITALIC = 1 << 4
RE_EXTENDED_CHAR = 1 << 6
RE_FAINT = 1 << 7
RE_STRIKEOUT = 1 << 8
RE_CONCEAL = 1 << 9
RE_OVERLINE = 1 << 10
RE_UNDERLINE_MASK = 15 << 12

# Rendition bit to SGR parameter. RE_TRANSPARENT, RE_CURSOR and RE_SELECTED are
# drawing state rather than character attributes, so they are not emitted.
RENDITION_SGR = (
    (RE_BOLD, 1),
    (RE_FAINT, 2),
    (RE_ITALIC, 3),
    (RE_BLINK, 5),
    (RE_REVERSE, 7),
    (RE_CONCEAL, 8),
    (RE_STRIKEOUT, 9),
    (RE_OVERLINE, 53),
)

# Konsole's underline styles, in the order the mask encodes them.
UNDERLINE_SGR = {1: "4", 2: "21", 3: "4:3", 4: "4:4", 5: "4:5"}

RESET = "\x1b[0m"


class Style(NamedTuple):
    """A cell's appearance, as the pieces an SGR sequence is built from."""

    rendition: int
    foreground: tuple[int, int, int, int]
    background: tuple[int, int, int, int]

    def is_plain(self) -> bool:
        return self == PLAIN_STYLE


PLAIN_STYLE = Style(0, (COLOR_SPACE_DEFAULT, 0, 0, 0), (COLOR_SPACE_DEFAULT, 1, 0, 0))


def _colour_sgr(colour: tuple[int, int, int, int], foreground: bool) -> list[str]:
    space, u, v, _w = colour
    base = 30 if foreground else 40
    bright = 90 if foreground else 100

    if space == COLOR_SPACE_DEFAULT or space == COLOR_SPACE_UNDEFINED:
        return [str(base + 9)]
    if space == COLOR_SPACE_SYSTEM:
        # _v is the intensity: 0 normal, 1 intense, 2 faint. Intense maps to the
        # bright range; faint has no colour of its own and is a rendition.
        return [str((bright if v == 1 else base) + (u & 7))]
    if space == COLOR_SPACE_256:
        return [str(base + 8), "5", str(u)]
    if space == COLOR_SPACE_RGB:
        return [str(base + 8), "2", str(u), str(v), str(_w)]
    return [str(base + 9)]


def style_sgr(style: Style) -> str:
    """The full SGR sequence for a style, starting from a reset.

    Emitting from a known state each time is longer than a minimal diff but
    cannot drift, which matters when replaying into a terminal whose state we
    do not control.
    """
    if style.is_plain():
        return RESET

    parts = ["0"]
    parts += [str(sgr) for bit, sgr in RENDITION_SGR if style.rendition & bit]

    underline = (style.rendition & RE_UNDERLINE_MASK) >> 12
    if underline in UNDERLINE_SGR:
        parts.append(UNDERLINE_SGR[underline])

    parts += _colour_sgr(style.foreground, foreground=True)
    parts += _colour_sgr(style.background, foreground=False)
    return "\x1b[" + ";".join(parts) + "m"


def _read_colour(chunk: bytes, position: int) -> tuple[int, int, int, int]:
    return tuple(chunk[position : position + 4])


def decode_ansi(cells: bytes, offsets: list[int], stride: int | None = None) -> list[str]:
    """Decode the grid to text carrying its original colours and attributes.

    Cells with the same appearance share one escape sequence, and every line
    ends with a reset so a restored pane cannot leak styling into whatever the
    shell prints next.
    """
    if not offsets:
        return []

    stride = stride if stride is not None else _derive_stride(offsets, cells[:65536])
    lines = []
    start = 0

    for end in offsets:
        if end > len(cells):
            break
        lines.append(_decode_line_ansi(cells[start:end], stride))
        start = end

    return lines


def _decode_line_ansi(chunk: bytes, stride: int) -> str:
    positions = range(0, len(chunk) - stride + 1, stride)
    decoded = []

    for position in positions:
        codepoint = struct.unpack_from("<I", chunk, position)[0]
        rendition = struct.unpack_from("<H", chunk, position + RENDITION_OFFSET)[0]
        if rendition & RE_EXTENDED_CHAR or not 32 <= codepoint <= 0x10FFFF:
            # A multi-codepoint grapheme is stored as a hash into a table that
            # lives only in Konsole's memory, so the character cannot be
            # recovered; keep the column.
            character = "�" if codepoint else " "
        else:
            character = chr(codepoint)
        decoded.append(
            (
                character,
                Style(
                    rendition,
                    _read_colour(chunk, position + FOREGROUND_OFFSET),
                    _read_colour(chunk, position + BACKGROUND_OFFSET),
                ),
            )
        )

    # Trailing blanks are grid padding, not content, but only when they carry no
    # background colour: a highlighted run of spaces is something to keep.
    while decoded and decoded[-1][0] == " " and decoded[-1][1].is_plain():
        decoded.pop()

    if not decoded:
        return ""

    out = []
    # Each line is emitted as if the terminal were in its default state, which
    # holds because every styled line below ends with a reset.
    current = PLAIN_STYLE
    for character, style in decoded:
        if style != current:
            out.append(style_sgr(style))
            current = style
        out.append(character)

    if not current.is_plain():
        out.append(RESET)
    return "".join(out)
