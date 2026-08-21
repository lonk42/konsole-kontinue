"""Tests for decoding Konsole's history files.

The fixtures are built rather than captured, because the interesting cases are
the ones a live Konsole will not produce on demand: a changed character size, a
truncated file, a half-written line.
"""

from __future__ import annotations

import os
import struct
from pathlib import Path

import pytest

from kontinue import history

STRIDE = history.EXPECTED_CHARACTER_SIZE


def build_cells(lines: list[str], stride: int = STRIDE) -> tuple[bytes, list[int]]:
    """Encode lines the way Konsole does: a flat cell array plus end offsets."""
    out = bytearray()
    offsets = []
    for line in lines:
        for char in line:
            out += struct.pack("<I", ord(char)) + b"\x00" * (stride - 4)
        offsets.append(len(out))
    return bytes(out), offsets


def test_decode_round_trip() -> None:
    lines = ["first line", "second line", "third"]
    cells, offsets = build_cells(lines)
    assert history.decode(cells, offsets) == lines


def test_decode_strips_grid_padding() -> None:
    """Konsole pads a row to the terminal width; trailing blanks are not content."""
    cells, offsets = build_cells(["text" + " " * 60])
    assert history.decode(cells, offsets) == ["text"]


def test_complete_bytes_is_the_last_index_entry() -> None:
    cells, offsets = build_cells(["one", "two"])
    assert history.complete_bytes(offsets) == len(cells)
    assert history.complete_bytes([]) == 0


def test_a_half_written_line_is_excluded() -> None:
    """The index lags the cells, so trailing bytes with no index entry are dropped."""
    cells, offsets = build_cells(["complete"])
    partial = cells + struct.pack("<I", ord("x")) + b"\x00" * (STRIDE - 4)
    assert history.decode(partial, offsets) == ["complete"]


def test_stride_is_derived_from_content() -> None:
    cells, offsets = build_cells(["hello world", "another line of text"])
    assert history._derive_stride(offsets, cells) == STRIDE


def test_stride_prefers_the_smallest_candidate_that_fits() -> None:
    """A multiple of the true size also decodes as text, sampling every Nth cell."""
    cells, offsets = build_cells(["aaaaaaaaaaaaaaaa", "bbbbbbbbbbbbbbbb"])
    assert history._plausibility(cells, 32) == pytest.approx(1.0)
    assert history._derive_stride(offsets, cells) == STRIDE


def test_unknown_format_raises_rather_than_returning_rubbish() -> None:
    """A character size change must surface as an error, not as corrupt text."""
    cells = os.urandom(4096)
    offsets = [len(cells)]
    with pytest.raises(history.HistoryFormatError):
        history._derive_stride(offsets, cells)


def test_plausibility_rejects_a_wrong_stride() -> None:
    cells, _ = build_cells(["the quick brown fox jumps over the lazy dog"])
    assert history._plausibility(cells, STRIDE) == pytest.approx(1.0)
    assert history._plausibility(cells, 12) < 0.75


def test_read_index_parses_offsets(tmp_path: Path) -> None:
    path = tmp_path / "index"
    path.write_bytes(struct.pack("<3q", 160, 320, 480))
    assert history.read_index(str(path)) == [160, 320, 480]


def test_read_index_of_an_empty_file(tmp_path: Path) -> None:
    path = tmp_path / "index"
    path.write_bytes(b"")
    assert history.read_index(str(path)) == []


def test_writing_under_proc_is_refused() -> None:
    """The live history inode must never be reachable for writing."""
    with pytest.raises(history.UnsafeAccessError):
        history.assert_safe_destination(Path("/proc/self/fd/1"))


def test_a_hard_linked_destination_is_refused(tmp_path: Path) -> None:
    """Konsole's unlinked history inodes can be linked back into the filesystem,
    which would make an innocent-looking path a live pane's scrollback."""
    original = tmp_path / "original"
    original.write_bytes(b"data")
    link = tmp_path / "link"
    os.link(original, link)

    with pytest.raises(history.UnsafeAccessError):
        history.assert_safe_destination(link)


def test_an_unlinked_inode_is_refused(tmp_path: Path) -> None:
    """An inode with no links but an open descriptor is what a live pane's
    scrollback looks like from outside, so it must never be a write target."""
    victim = tmp_path / "victim"
    victim.write_bytes(b"live data")
    holder = os.open(victim, os.O_RDONLY)
    try:
        os.unlink(victim)
        proc_path = Path(f"/proc/self/fd/{holder}")
        with pytest.raises(history.UnsafeAccessError):
            history.assert_safe_destination(proc_path)
    finally:
        os.close(holder)


def test_an_ordinary_destination_is_allowed(tmp_path: Path) -> None:
    history.assert_safe_destination(tmp_path / "new-file")


def test_locate_returns_none_without_file_backed_history(tmp_path: Path) -> None:
    """A pane with a fixed scrollback size has no history files to find."""
    assert history.locate(os.getpid(), "/dev/pts/nonexistent") is None


# -- colour and attributes -------------------------------------------------

DEFAULT_FG = (history.COLOR_SPACE_DEFAULT, 0, 0, 0)
DEFAULT_BG = (history.COLOR_SPACE_DEFAULT, 1, 0, 0)


def cell(char: str, rendition: int = 0, fg=DEFAULT_FG, bg=DEFAULT_BG) -> bytes:
    """One Character: codepoint, rendition, foreground, background, flags."""
    return (
        struct.pack("<I", ord(char))
        + struct.pack("<H", rendition)
        + bytes(fg)
        + bytes(bg)
        + struct.pack("<H", 1)
    )


def styled(cells: list[bytes]) -> tuple[bytes, list[int]]:
    data = b"".join(cells)
    return data, [len(data)]


def test_plain_text_needs_no_escapes() -> None:
    data, offsets = styled([cell(c) for c in "plain"])
    assert history.decode_ansi(data, offsets, history.EXPECTED_CHARACTER_SIZE) == ["plain"]


def test_system_colour_becomes_a_basic_sgr() -> None:
    red = (history.COLOR_SPACE_SYSTEM, 1, 0, 0)
    data, offsets = styled([cell("x", fg=red)])
    assert history.decode_ansi(data, offsets, 16) == ["\x1b[0;31;49mx\x1b[0m"]


def test_intense_system_colour_uses_the_bright_range() -> None:
    """Konsole stores brightness as an intensity beside the base colour."""
    bright_green = (history.COLOR_SPACE_SYSTEM, 2, 1, 0)
    data, offsets = styled([cell("x", fg=bright_green)])
    assert "92" in history.decode_ansi(data, offsets, 16)[0]


def test_256_colour() -> None:
    orange = (history.COLOR_SPACE_256, 208, 0, 0)
    data, offsets = styled([cell("x", fg=orange)])
    assert "38;5;208" in history.decode_ansi(data, offsets, 16)[0]


def test_true_colour() -> None:
    magenta = (history.COLOR_SPACE_RGB, 255, 0, 255)
    data, offsets = styled([cell("x", fg=magenta)])
    assert "38;2;255;0;255" in history.decode_ansi(data, offsets, 16)[0]


def test_background_colour() -> None:
    yellow = (history.COLOR_SPACE_SYSTEM, 3, 0, 0)
    data, offsets = styled([cell("x", bg=yellow)])
    assert "43" in history.decode_ansi(data, offsets, 16)[0]


def test_renditions_map_to_their_sgr_codes() -> None:
    for flag, code in ((history.RE_BOLD, "1"), (history.RE_ITALIC, "3"),
                       (history.RE_REVERSE, "7"), (history.RE_STRIKEOUT, "9")):
        data, offsets = styled([cell("x", rendition=flag)])
        assert code in history.decode_ansi(data, offsets, 16)[0].split("m")[0]


def test_underline_style_is_taken_from_the_mask() -> None:
    single = 1 << 12
    data, offsets = styled([cell("x", rendition=single)])
    assert "4" in history.decode_ansi(data, offsets, 16)[0].split("m")[0]


def test_a_run_shares_one_escape() -> None:
    """Re-emitting per character would multiply the replay size for no gain."""
    red = (history.COLOR_SPACE_SYSTEM, 1, 0, 0)
    data, offsets = styled([cell(c, fg=red) for c in "same"])
    line = history.decode_ansi(data, offsets, 16)[0]
    assert line.count("\x1b[0;31;49m") == 1


def test_a_styled_line_ends_reset() -> None:
    """Otherwise a restored pane leaks styling into whatever the shell prints next."""
    red = (history.COLOR_SPACE_SYSTEM, 1, 0, 0)
    data, offsets = styled([cell("x", fg=red)])
    assert history.decode_ansi(data, offsets, 16)[0].endswith(history.RESET)


def test_trailing_padding_is_dropped() -> None:
    data, offsets = styled([cell(c) for c in "hi" + " " * 40])
    assert history.decode_ansi(data, offsets, 16) == ["hi"]


def test_trailing_spaces_with_a_background_are_kept() -> None:
    """A highlighted run of spaces is content, not grid padding."""
    blue = (history.COLOR_SPACE_SYSTEM, 4, 0, 0)
    data, offsets = styled([cell("h")] + [cell(" ", bg=blue) for _ in range(3)])
    line = history.decode_ansi(data, offsets, 16)[0]
    assert "44" in line
    assert line.rstrip(history.RESET).endswith("   ")


def test_an_extended_grapheme_keeps_its_column() -> None:
    """The codepoint is a hash into a table that only exists in Konsole's memory."""
    data, offsets = styled([cell("a"), cell("￿", rendition=history.RE_EXTENDED_CHAR)])
    assert len(history.decode_ansi(data, offsets, 16)[0].replace(history.RESET, "")) >= 2
