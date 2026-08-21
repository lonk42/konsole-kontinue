# Reading scrollback

Konsole's D-Bus interface cannot reach a pane's scrollback:
`getDisplayedText()` is clamped to the visible window and rejects any offset reaching into history,
and nothing on the interface writes into a session's history either.
The data is still reachable, just not that way.

## Where the data lives

A pane set to unlimited scrollback gets a `HistoryScrollFile`, which keeps three files:

| file | contents |
|---|---|
| index | one `qint64` per line, holding that line's **end** offset into the cells file |
| cells | a flat array of `Character`, currently 16 bytes each |
| lineflags | one `LineProperty` per line, 6 bytes each, carrying the wrapped flag |

`HistoryFile`'s constructor creates each one in a cache directory, opens it, and immediately unlinks it:

```cpp
// Remove file entry from filesystem. Since the file
// is opened, it will still be available for reading
// and writing. This guarantees the file won't remain
// in filesystem after process termination, even when
// there was a crash.
unlink(QFile::encodeName(_tmpFile.fileName()).constData());
```

That gives the scrollback exactly the lifetime you would want:
on disk rather than in memory, invisible to anything walking the filesystem,
and reclaimed by the kernel when the pane closes.
It also leaves the descriptors open in Konsole's file table,
so the data is readable through `/proc/<konsole-pid>/fd/<n>` for as long as the pane lives.

A pane set to a fixed number of lines uses a compact in-memory structure instead.
There are no files, and nothing outside Konsole can read it.

## Finding a pane's files

Konsole's descriptor table is laid out per pane, in creation order:
the pty master, the pty slave, then that pane's three history files.

```
26 -> /dev/pts/ptmx
27 -> /dev/pts/2
28 -> /home/you/.cache/konsole/#276436 (deleted)     index
30 -> /home/you/.cache/konsole/#281566 (deleted)     cells
31 -> /home/you/.cache/konsole/#281590 (deleted)     lineflags
32 -> /dev/pts/ptmx
36 -> /dev/pts/4
...
```

So a pane's session id leads to its files:
ask D-Bus for `processId()`, read `/proc/<shell-pid>/fd/0` to get the pty,
find that pty in Konsole's descriptor table,
and take the next three unlinked cache files.
Stopping at the following pty keeps one pane's files from being attributed to the next.

## Reading a line

Line `i` spans `[start, end)` in the cells file, where `start` is `0` for line 0
and `index[i - 1]` otherwise, and `end` is `index[i]`.

`addLine()` appends the index entry *after* `addCells()` has written the characters,
so the index always lags the cells.
That makes the last index entry a safe upper bound:
reading only that far can never catch a line Konsole is midway through writing.

Each `Character` is 16 bytes:

| offset | field | meaning |
|---|---|---|
| 0 | `character` | `uint32`, a UTF-32 codepoint |
| 4 | `rendition` | `uint16` of `RE_*` flags: bold, italic, reverse, the underline style, and so on |
| 6 | `foregroundColor` | `CharacterColor` |
| 10 | `backgroundColor` | `CharacterColor` |
| 14 | `flags` | `uint16` of `ExtraFlags` |

`CharacterColor` is a colour space byte followed by three bytes whose meaning depends on it:

| space | encoding |
|---|---|
| 1 `DEFAULT` | `u` is 0 for the default foreground, 1 for the default background |
| 2 `SYSTEM` | `u` is the base colour 0 to 7, `v` the intensity: 0 normal, 1 intense, 2 faint |
| 3 `256` | `u` is the palette index |
| 4 `RGB` | `u`, `v`, `w` are red, green and blue |

`RE_EXTENDED_CHAR` means the codepoint is not a character at all
but a hash into a table of multi-codepoint graphemes that lives only in Konsole's memory.
Those cells cannot be recovered, so a placeholder keeps the column.

## The character size is not a contract

`Character` is a private C++ struct.
Its size is 16 bytes today and nothing promises it will stay that way.
Reading the grid at the wrong stride does not fail, it produces text-shaped rubbish,
so the size is derived from the data rather than assumed:

1. Every line is a whole number of characters,
   so the greatest common divisor of the line lengths narrows the candidates.
2. Each surviving candidate is scored by how much of the file decodes to plausible characters.
   Terminal output is overwhelmingly ASCII, so the correct stride scores near 100% and a wrong one scores far lower.
3. A tie breaks towards the smallest candidate,
   because any multiple of the true size also decodes as text by sampling every Nth cell.
4. If the best score is still poor, kontinue raises rather than writing out nonsense.

## Writing is forbidden

The descriptors reached through `/proc` point at the inode Konsole is still writing to.
Opening one for writing corrupts a running pane's scrollback,
and there is a subtle path to doing it by accident.

An unlinked inode normally cannot be linked back into the filesystem,
but `linkat()` on the `/proc` path with `AT_SYMLINK_FOLLOW` succeeds on current kernels
for a file you own on the same filesystem.
That turns an ordinary looking filename into a live pane's history,
and an `O_TRUNC` on it then destroys the scrollback with no error and no warning.
This is not hypothetical; it happened while this was being built, and cost a pane's history.

Two rules follow, both enforced in code:

* `history.open_readonly()` is the only opener used for anything under `/proc`.
* `history.assert_safe_destination()` guards every write.
  It rejects paths under `/proc`, checking the path **as given** as well as the resolved one,
  because resolving a `/proc/<pid>/fd/<n>` path follows the magic link out of `/proc`
  and a check on the resolved path alone never fires.
  It also rejects any destination whose link count is not exactly 1:
  zero means an unlinked inode someone else is still using,
  and more than one means a hard link.

## Storage

Copies are keyed by the inode of the cells file.
Session ids are reused as soon as a session closes,
whereas the inode is stable for the life of the pane,
which is exactly the lifetime of the scrollback.

Konsole only appends to a history file while a pane is alive,
so copies are incremental: the store records each file's size and a hash of its first 4 KB,
and copies only the new tail when both still agree.
Two things break that and force a full recopy, each detected rather than assumed:

* `HistoryScrollFile::removeCells()` truncates the files when Konsole reflows a resized pane.
* Clearing a pane's history resets the files, so the same size can hold different content.

## Replaying

Restoring writes the saved lines straight to the new pane's pty slave,
which puts them on screen exactly as if the pane had printed them,
so they scroll and search like any other history.

Colours and attributes are rebuilt as SGR sequences from each cell's rendition and colours.
Cells that look alike share one escape,
and every styled line ends with a reset,
so a line can be emitted assuming the terminal starts in its default state
and a restored pane cannot leak styling into whatever the shell prints next.

The layout file's `Command` field is not used for this.
`Session::runCommandFromLayout()` delivers it with `sendText()`,
which types the command into the shell,
leaving the command line itself sitting in the restored history.

The history file holds only lines that have scrolled off the top,
so the screen as it stood at capture time is saved separately,
through `getAllDisplayedTextList()` on D-Bus,
and replayed after the history.
Without it the most recent screenful of every pane would be missing.

That last screenful is the one part that replays without colour:
D-Bus returns it as plain text and offers no styled equivalent.
