# Konsole's D-Bus API in practice

Reference notes for anyone working on kontinue or writing their own Konsole tooling.
None of this is documented upstream.
Verified against Konsole 26.04.3.

Konsole exposes, per process:

```
org.kde.konsole-<pid>
    /Windows/<n>    org.kde.konsole.Window
    /Sessions/<n>   org.kde.konsole.Session
```

## The viewHierarchy grammar

`viewHierarchy()` returns one string per tab, in tab order,
each encoding that tab's splitter tree.
From `ViewSplitter::getChildWidgetsLayout()`:

```
node     := view | splitter
view     := INT                       # a TerminalDisplay id
splitter := '(' INT ')' body
body     := '[' children ']'          # Qt::Horizontal
          | '{' children '}'          # Qt::Vertical
children := node ('|' node)*
```

`(0)[0|(1){1|2}]` is splitter 0 laid out horizontally,
holding view 0 alongside splitter 1, which stacks views 1 and 2 vertically.

Ids are not contiguous.
Konsole never reuses view ids, so closing panes leaves permanent gaps:
`(7)[10]` is an ordinary tab.

## View ids and session ids cannot be paired by sorting

They look like parallel counters and behave like it right up until you close a pane.
The allocation differs:

| id | allocation | reuses ids? |
|---|---|:---:|
| view | `TerminalDisplay::_id(++lastViewId)`, a static counter | ❌ |
| session | `Session::_sessionId = maxSessionId + 1` over *live* sessions | ✅ |

Sorting view ids and session ids and zipping them
gives the correct answer in every test that does not close a pane,
and is permanently wrong afterwards.

## There is no view-to-session lookup

`viewHierarchy()` speaks in view ids.
Sessions are addressed by session id.
Nothing maps between them.
Two routes exist, both imperfect.

### Passive: pair against sessionList

`ViewManager::sessionList()` loops over tabs
and, within each, collects `findChildren<TerminalDisplay *>()`.

The outer loop makes the **grouping by tab exact**:
the first N ids belong to the first tab.
The inner order is Qt object-tree order, which splitting reparents,
so within a multi-pane tab it does not match layout order.

Result: exact for single-pane tabs, a guess otherwise.
Costs nothing and touches nothing.

### Probe: walk the focus

Call `setCurrentView(viewId)`, then read `currentSession()`.
Exact, but **only while the Konsole window is active.**

`currentSession()` reads `ViewManager::_pluggedController`,
which is updated by the `viewFocused` signal, i.e. a real Qt focus event.
On an inactive window, `setFocus()` sets the focus widget without delivering that event,
so `_pluggedController` never changes
and every view reports the *previously* current session.

The failure is silent
and produces a plausible-looking mapping where every view points at one session.
Check the result for injectivity before trusting it.
This also moves the user's focus, so it is unsuitable for anything running on a timer.

## Other sharp edges

**`tabColor()` returns `#000000` when unset**,
so "no colour" and "black" are indistinguishable.

**`newSession` is overloaded three ways** on one bus name,
which upsets strict introspection.
Call a specific arity.

**No working-directory getter exists.**
`Session::currentWorkingDirectory()` is used internally
by Konsole's own session-management code but is not `Q_SCRIPTABLE`.
Read `/proc/<processId>/cwd`,
which is cheaper and more robust than shelling out to `pwdx`.

**Capture `tabTitleFormat(0)`, not `title(1)`.**
Renaming a tab stores the new name *as* that session's title format,
so capturing the format round-trips manual renames.
`title()` returns the resolved string (`git : bash`)
and would freeze a dynamic format into a stale literal.

**Nothing can close a pane from outside.**
`Session` has no `close` and `Window` has nothing that removes a view;
the closest it offers is `moveView`, which relocates one between tabs.
`sendText()` would let you type `exit`, but it is among the methods disabled by
Konsole's security setting, so it cannot be relied on.
Killing the pane's shell does not work either,
because a shell that dies from a signal is an abnormal exit
and Konsole keeps the pane open to report it.
A shell exiting normally does close its pane, which is the only route,
and it is not one an outside process can take.

A whole Konsole process can be closed, by signalling it.
So anything wanting to replace what is on screen has to work a window at a time:
build the replacement in a new window, then close the old one.
`loadLayout()` appending rather than replacing is the same constraint seen from the other side.

**Scrollback is unreachable over D-Bus**, though not unreachable in general;
see [docs/scrollback.md](scrollback.md).
`getDisplayedText()` opens with:

```cpp
if (startLineOffset < 0 || endLineOffset >= screenWindow->windowLines()
    || startLineOffset > endLineOffset) {
    return QStringList();
}
```

so it is clamped to the visible window and rejects any request reaching into history.
Nothing on the interface writes into a session's history either;
`sendText()` and `runCommand()` go to the shell's *input*.

## Useful methods

On `org.kde.konsole.Window`:

| method | note |
|---|---|
| `viewHierarchy()` | one layout string per tab, in tab order |
| `sessionList()` | session ids, grouped by tab, scrambled within a tab |
| `getSplitProportions(id)` | child sizes as percentages, empty if no such splitter |
| `currentSession()` | needs a real focus event to be current |
| `setCurrentView(id)` | switches tab and sets focus widget; returns false if unknown |
| `createSplit(viewId, horizontal)` | |
| `resizeSplits(splitterId, percentages)` | rejects any percentage below 1 |
| `loadLayout(file)` | loads Konsole's own layout JSON |
| `saveLayoutFile()` | opens a save dialog, so not scriptable |

| `newSession(profile, dir)` | opens a tab on a named profile; useful for driving tests |

On `org.kde.konsole.Session`:
`profile()`, `processId()`, `tabTitleFormat(role)`, `tabColor()`, `historySize()`,
`setTabTitleFormat(role, s)`, `runCommand(s)`.
`processId()` returns 0 once the pane's program has exited,
which is how a pane Konsole is holding open to report a crash can be told from a live one.

Watching for Konsole starting and stopping is a plain `NameOwnerChanged` subscription
on `org.freedesktop.DBus`, filtered to names matching `org.kde.konsole-<pid>`.
The name is claimed before the window is built,
so anything inspecting the new process has to let it settle first.

## Command line

```
konsole --layout <file.json>       # load a split layout at startup
konsole --tabs-from-file <file>    # create tabs from a config file
konsole --separate                 # force a new process, useful for testing
```

The tabs file is one tab per line, `;;`-separated, key/value split on the first colon.
Each line needs at least one of `command` or `profile`
or Konsole warns and skips it:

```
workdir: /path;; title: My Tab;; command:
```

Konsole's layout JSON, per tab only:

```json
{
  "Orientation": "Horizontal",
  "Widgets": [{"SessionRestoreId": 0}, {"SessionRestoreId": 0}]
}
```

The restore path also accepts `Columns`, `Lines`, `Command` and `WorkingDirectory` per widget,
which the shipped examples in `data/layouts/` do not show.
`SessionRestoreId: 0` means "create a fresh session";
the id lookup is only used for live re-layout within one process.

## Prior art

[Kelvin-Ng/konsole-session-restore](https://github.com/Kelvin-Ng/konsole-session-restore)
is about thirty lines
and captures tabs, working directories and title formats via `--tabs-from-file`.
The `tabTitleFormat` insight
and the `-e 'bash -c exit'` trick for suppressing Konsole's extra default tab
both come from it.

[CarloWood/konsole-session-restore](https://github.com/CarloWood/konsole-session-restore)
is much larger
and handles multiple windows, per-session profiles and window geometry.
It is X11-only: it depends on `wmctrl`, `DISPLAY` and `XAUTHORITY`,
and ships a systemd user service.

Neither handles splits.
