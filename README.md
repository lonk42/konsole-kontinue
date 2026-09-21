# konsole-kontinue

Session persistence tool for [Konsole](https://konsole.kde.org/).

Provides a similar approach to browser style 'Restore your session' but for Konsole tabs and panes.
Layout, naming and scrollback are all retained and restorable.

## What it saves

- **Tabs, in order, with their names.**
  A tab you renamed by hand comes back with the name you gave it,
  and one left on Konsole's automatic naming keeps following it.
- **Splits, nested to any depth.**
  Which way each split runs and how the space is divided between panes.
- **Each pane's working directory and Konsole profile.**
- **Tab colours**, and which tab was in front.
- **The whole scrollback of every pane**, uncapped and in colour.
  Everything that scrolled off the top, not just what was on screen,
  with the colours and attributes it had.
  [Detail](#scrollback)

Foreground commands are not re-run on restoration.

## Install

### Prerequisites

```bash
sudo dnf install python3-dbus      # Fedora
sudo apt install python3-dbus      # Debian/Ubuntu
sudo pacman -S python-dbus         # Arch
pip install --user .
```

Needs Python 3.10 or newer.
Installing `dbus-python` from your distro rather than letting `pip` build it
avoids needing dbus and glib development headers.

### Konsole settings

Two Konsole settings are mandatory:

1. Single process: **Settings > Configure Konsole > General > Run all Konsole windows in a single process**
2. Unlimited scrollback: **Settings > Edit Current Profile > Scrolling > Unlimited scrollback**

### kontinue-watch

kontinue can be driven entirely from its CLI, but `kontinue watch` runs it as a daemon.
It is a single process that saves on a timer and restores when Konsole next opens.

```console
$ kontinue install
installed /home/you/.config/autostart/kontinue-watch.desktop
runs: /usr/bin/kontinue watch
ksmserver will start it at your next login.
Start it now without waiting:  kontinue watch &
```

**Saving is on a timer.**
Watch takes a fresh snapshot every interval, so the interval is how much you can lose.
`--interval SECONDS` sets it.

**Auto-restore when the first Konsole opens.**
If no Konsole is running and a new one starts, kontinue restores into it.
Closing them all arms it again for next time.
A watcher started after that first Konsole opened, such as one launched from `~/.bashrc`, restores into it too, provided the saved session has ended.

## Usage

With the watch daemon running, saving and restoring happen on their own.
The CLI drives the same operations by hand, and works as an alternative to the daemon.

```bash
kontinue save                    # Write a snapshot to ~/.local/state/kontinue/snapshot.json
kontinue save --stdout           # Print it instead
kontinue save --probe            # More accurate on tabs with splits, see below
kontinue save --no-scrollback    # Layout only, skip the scrollback copy
kontinue restore [PATH]          # Rebuild a saved arrangement
kontinue restore --new-window    # Rebuild into a fresh Konsole rather than the running one
kontinue watch                   # Save on a timer, and restore when Konsole next opens
kontinue show [PATH]             # Print a saved snapshot as a readable tree
kontinue show --generations      # List older arrangements that were kept
kontinue show --generation N     # Print one of them as a tree
kontinue restore --generation N  # Rebuild one of them
kontinue prune                   # Drop scrollback for panes you closed
kontinue install                 # Start the watcher when you log in
kontinue uninstall               # Stop doing that
kontinue status                  # Report whether any of this is set up
```

```console
$ kontinue save
saved 16 pane(s) across 10 tab(s) to ~/.local/state/kontinue/snapshot.json

$ kontinue show
captured 2026-08-12T02:02:55+00:00
konsole (pid 3633 at capture time)
  window 1
    tab 0  (1)[1]
      horizontal split #1  [100%]
        pane view=1 session=2 profile=Shell
          title='%d : %n' cwd=/home/you/src/myproject
    tab 1  (3)[2]
      horizontal split #3  [100%]
        pane view=2 session=3 profile=Shell
          title='%d : %n' cwd=/home/you
```

```console
$ kontinue restore
restored 4 pane(s) across 2 tab(s), replayed 243 scrollback line(s) into 4 pane(s)
```

## Checking it is working

```console
$ kontinue status
watcher:     running (pid 4127)
autostart:   installed, and ksmserver will run it
snapshot:    16 pane(s) across 10 tab(s), captured 2026-08-20T02:29:00+00:00
generations: 3 retained
scrollback:  12.4 MB in ~/.local/state/kontinue/scrollback
konsole:     1 running
```

If a recent generation holds far more panes than the current snapshot, `status` names it and prints the command to restore it.

## Older arrangements

The snapshot is overwritten every time the timer fires.
If you close the wrong window, restore an older one:

```console
$ kontinue show --generations
1  captured 2026-08-20T02:18:38+00:00  6 pane(s) across 4 tab(s)
   myproject (3), you (2), notes
2  captured 2026-08-19T21:04:11+00:00  14 pane(s) across 9 tab(s)
   myproject (6), infra (4), you (3), logs; named: build

look inside one with: kontinue show --generation N
restore one with:     kontinue restore --new-window --generation N
```

One is kept when the last Konsole exits, being the closing state of a finished session,
and otherwise at most one an hour, so timer saves do not fill the archive with copies of the same arrangement.
Five are kept, and the scrollback they point at is held for as long as they are.

## Split panes

A [limitation in Konsole's D-Bus interface](docs/konsole-dbus.md#there-is-no-view-to-session-lookup) is that it will describe the shape of a window, and it will describe each pane,
but it will not say which pane is which.
For a tab holding a single pane there is only one possible answer.
For a tab with split panes, kontinue has to work the arrangement out, and it can be wrong.

Every window in a snapshot is therefore labelled with how much to trust it, `exact` or `inferred`.

The experimental `--probe` flag resolves the split tabs by briefly moving the keyboard focus through each pane and asking Konsole which one it landed on.
It only works while the Konsole window is in the foreground, and it moves your focus while it runs, so it is not on the timer.

## Scrollback

Restoring scrollback needs unlimited scrollback.
On that setting Konsole buffers a pane's history to a file rather than keeping it in memory, and kontinue persists the file.

Restored scrollback keeps its colours, bold, italics and underlines.
The one exception is the last screenful of each pane,
which Konsole only hands over as plain text.

Scrollback lives beside the snapshot in `~/.local/state/kontinue/scrollback/`,
one file per pane, and is copied incrementally:
a pane only costs its full size once, and after that only what it has added.

**Closing a pane discards its scrollback. Quitting Konsole does not.**
To control disk usage, close panes when they get large.
Closing a pane is kontinue's signal to collect it in a week.
`kontinue prune` collects right away.

> **This is a verbatim record of your terminals.**
> Your output is buffered to disk, including tokens, keys and anything you sent to stdout.
> Scrollback is stored `0600`, and has no other protection.

[How this works](docs/scrollback.md)

## Development

```bash
pip install --user -e '.[dev]'
pytest
```

The code that reads Konsole's layout descriptions is self-contained
and tested against real examples captured from a running Konsole.
Everything that talks to Konsole lives in `src/kontinue/konsole.py`;
the rest of the package never touches D-Bus.

Test against a throwaway Konsole rather than the one you are working in:

```bash
konsole --separate --profile <name> &
gdbus call --session --dest org.kde.konsole-<pid> \
  --object-path /Windows/1 --method org.kde.konsole.Window.viewHierarchy
```

## Future

- Scrollback encryption
- A 'would you like to restore' prompt
- Scrollback size limits and warnings
- Disk space protection

## Documentation

| | |
|---|---|
| [docs/konsole-dbus.md](docs/konsole-dbus.md) | How Konsole's D-Bus interface behaves, what it will and will not tell you, and the traps |
| [docs/scrollback.md](docs/scrollback.md) | Where Konsole keeps scrollback, how it is read, and why writing to it is forbidden |
