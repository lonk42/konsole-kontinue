"""Command line entry point."""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

from . import autostart, generations, lock, model, restore as restore_mod, scrollback, snapshot

log = logging.getLogger("kontinue")


def default_state_path() -> Path:
    """Snapshots live in XDG state, which is for data that should persist
    across restarts but is not precious enough to back up."""
    base = os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state")
    return Path(base) / "kontinue" / "snapshot.json"


def default_scrollback_dir() -> Path:
    return default_state_path().parent / "scrollback"


def build_parser() -> argparse.ArgumentParser:
    # Carried by every subcommand as well as the top level, so that both
    # `kontinue -v watch` and `kontinue watch -v` work. Getting that wrong is a
    # confusing first thing to meet, because argparse rejects the second form
    # with a bare "unrecognized arguments".
    common = argparse.ArgumentParser(add_help=False)
    # SUPPRESS, not a False default: a subparser sharing this flag would
    # otherwise write its own default over a -v given before the subcommand,
    # so `kontinue -v watch` would parse as not verbose.
    common.add_argument(
        "-v", "--verbose", action="store_true", default=argparse.SUPPRESS,
        help="log what is happening",
    )

    parser = argparse.ArgumentParser(
        prog="kontinue",
        description="Save and restore Konsole tabs, splits and layouts.",
        parents=[common],
    )

    subcommands = parser.add_subparsers(dest="command", required=True)

    save = subcommands.add_parser("save", parents=[common],
        help="capture the current Konsole state")
    save.add_argument(
        "-o",
        "--output",
        type=Path,
        default=None,
        help=f"where to write the snapshot (default: {default_state_path()})",
    )
    save.add_argument(
        "--probe",
        action="store_true",
        help="resolve pane identity exactly by walking the focus; only works "
        "while the Konsole window is active, and moves your focus while it runs",
    )
    save.add_argument(
        "--no-scrollback",
        action="store_true",
        help="skip capturing pane scrollback",
    )
    save.add_argument(
        "--stdout",
        action="store_true",
        help="print the snapshot instead of writing it to disk",
    )

    restore_cmd = subcommands.add_parser(
        "restore", parents=[common], help="rebuild a saved arrangement in Konsole"
    )
    restore_cmd.add_argument(
        "path", type=Path, nargs="?", default=None,
        help=f"snapshot to restore (default: {default_state_path()})",
    )
    restore_cmd.add_argument(
        "--generation", type=int, default=0, metavar="N",
        help="restore a retained older arrangement instead of the current one; "
        "1 is the most recent, and `kontinue show --generations` lists them",
    )
    restore_cmd.add_argument(
        "--new-window", action="store_true",
        help="restore into a freshly launched Konsole instead of one already running",
    )
    restore_cmd.add_argument(
        "--no-scrollback", action="store_true",
        help="rebuild the layout without replaying saved scrollback",
    )

    prune = subcommands.add_parser(
        "prune", parents=[common],
        help="delete stored scrollback for panes that were closed"
    )
    prune.add_argument(
        "path",
        type=Path,
        nargs="?",
        default=None,
        help="snapshot whose scrollback to spare, since a restore will want it "
        f"(default: {default_state_path()})",
    )

    watch_cmd = subcommands.add_parser(
        "watch",
        parents=[common],
        help="snapshot on a timer, and restore when the first Konsole opens",
    )
    watch_cmd.add_argument(
        "--interval",
        type=int,
        default=45,
        metavar="SECONDS",
        help="how often to snapshot; this is also how much can be lost, since "
        "scrollback dies with the Konsole process (default: 45)",
    )
    watch_cmd.add_argument(
        "--no-restore",
        action="store_true",
        help="only save; do not restore when the first Konsole opens",
    )
    watch_cmd.add_argument(
        "--no-scrollback", action="store_true", help="do not capture pane scrollback"
    )

    show = subcommands.add_parser("show", parents=[common],
        help="summarise a snapshot as a tree")
    show.add_argument(
        "path",
        type=Path,
        nargs="?",
        default=None,
        help=f"snapshot to read (default: {default_state_path()})",
    )
    show.add_argument(
        "--generations", action="store_true",
        help="list retained older arrangements instead of showing a snapshot",
    )
    show.add_argument(
        "--generation", type=int, default=0, metavar="N",
        help="show retained arrangement N, numbered as --generations lists them",
    )

    install = subcommands.add_parser(
        "install", parents=[common],
        help="start the watcher automatically when you log in",
    )
    install.add_argument(
        "--interval", type=int, default=None, metavar="SECONDS",
        help="snapshot interval to bake into the autostart entry",
    )
    install.add_argument(
        "--no-scrollback", action="store_true",
        help="have the installed watcher save layout only",
    )

    subcommands.add_parser(
        "uninstall", parents=[common],
        help="stop the watcher starting when you log in",
    )

    subcommands.add_parser(
        "status", parents=[common],
        help="report whether saving and restoring are actually set up",
    )

    return parser


def cmd_save(args: argparse.Namespace) -> int:
    if args.stdout:
        captured = snapshot.capture(probe=args.probe)
        if not captured.instances:
            log.error("no running Konsole found on the session bus")
            return 1
        sys.stdout.write(captured.dumps())
        return 0

    result = snapshot.save(
        args.output or default_state_path(),
        None if args.no_scrollback else default_scrollback_dir(),
        probe=args.probe,
    )
    if result is None:
        log.error("no running Konsole found on the session bus")
        return 1

    print(describe_save(result), file=sys.stderr)
    return 0


def describe_save(result: snapshot.SaveResult) -> str:
    captured = result.snapshot
    message = (
        f"saved {captured.pane_count()} pane(s) across "
        f"{captured.tab_count()} tab(s) to {result.path}"
    )
    if result.pruned is not None:
        message += (
            f"\nscrollback: {result.stored_bytes / 1e6:.1f} MB in "
            f"{default_scrollback_dir()}"
        )
        message += describe_prune(result.pruned)
    message += describe_missing_history(result.stats.panes_without_history)
    inferred = sum(
        1
        for instance in captured.instances
        for window in instance.windows
        if window.mapping_quality != "exact"
    )
    if inferred:
        message += (
            f"\nnote: {inferred} window(s) have split tabs whose pane order is "
            "inferred; use --probe for exact"
        )
    return message


def describe_missing_history(panes: int) -> str:
    """Warn about panes whose scrollback could not be reached.

    Konsole's default profile keeps history in memory, where nothing outside
    the process can read it, so a default setup saves layout and no scrollback
    at all. Silence here reads as success, and the loss only shows up at the
    restore, so this is worth a line every time.
    """
    if not panes:
        return ""
    return (
        f"\nwarning: {panes} pane(s) keep scrollback in memory, so none was saved "
        "for them.\n         Turn on Settings > Edit Current Profile > Scrolling > "
        "Unlimited scrollback."
    )


def describe_prune(pruned: scrollback.PruneResult) -> str:
    """Say what a prune did, only where there is something to say."""
    parts = []
    if pruned.closed:
        parts.append(f"{len(pruned.closed)} closed pane(s) pruned")
    if pruned.orphaned:
        parts.append(f"{len(pruned.orphaned)} kept from an exited Konsole")
    if pruned.expired:
        parts.append(f"{len(pruned.expired)} expired")
    return f" ({', '.join(parts)})" if parts else ""


def cmd_restore(args: argparse.Namespace) -> int:
    path = args.path or default_state_path()
    if args.generation:
        try:
            loaded, path = generations.load(default_state_path(), args.generation)
        except (FileNotFoundError, ValueError) as exc:
            log.error("%s", exc)
            return 1
        except model.SchemaError as exc:
            log.error("%s", exc)
            return 1
        print(f"restoring generation {args.generation}, captured {loaded.captured_at}",
              file=sys.stderr)
    else:
        try:
            loaded = model.Snapshot.loads(path.read_text())
        except FileNotFoundError:
            log.error("no snapshot at %s", path)
            return 1
        except model.SchemaError as exc:
            log.error("%s", exc)
            return 1

    store = None if args.no_scrollback else scrollback.Store(default_scrollback_dir())
    try:
        with lock.held(default_state_path().parent / lock.LOCK_NAME):
            report = restore_mod.restore(loaded, store=store, reuse=not args.new_window)
    except lock.Busy as exc:
        log.error("%s", exc)
        return 1
    except restore_mod.RestoreError as exc:
        log.error("%s", exc)
        return 1

    for warning in report.warnings:
        log.warning("%s", warning)
    print(report.summary(), file=sys.stderr)
    return 0


def cmd_prune(args: argparse.Namespace) -> int:
    """Drop scrollback whose pane was closed, judged against what is running."""
    store = scrollback.Store(default_scrollback_dir())
    before = store.total_bytes()

    pruned = store.prune(snapshot.live_pane_inodes(), snapshot_references(args.path))
    store.commit()
    freed = before - store.total_bytes()

    print(
        f"pruned {len(pruned.removed)} pane(s), freeing {freed / 1e6:.1f} MB"
        + describe_prune(pruned),
        file=sys.stderr,
    )
    return 0


def snapshot_references(path: Path | None) -> set[int]:
    """Inodes the saved snapshot still points at.

    Read rather than assumed empty: these are what a later restore will ask
    for, and they are what holds an exited Konsole's scrollback in place.
    An unreadable snapshot yields nothing, which only ever costs an orphan its
    reprieve once it is already past the grace period.
    """
    path = path or default_state_path()
    try:
        loaded = model.Snapshot.loads(path.read_text())
    except (FileNotFoundError, model.SchemaError):
        return set()
    return snapshot.live_scrollback_inodes(loaded) | generations.referenced_inodes(
        default_state_path()
    )


def cmd_watch(args: argparse.Namespace) -> int:
    from . import watch

    config = watch.Config(
        state_path=default_state_path(),
        scrollback_dir=None if args.no_scrollback else default_scrollback_dir(),
        interval=args.interval,
        auto_restore=not args.no_restore,
    )
    # The watcher's whole output is its log, so it says what it is doing at
    # info level even without -v; a silent daemon is indistinguishable from a
    # dead one.
    logging.getLogger("kontinue").setLevel(
        logging.DEBUG if getattr(args, "verbose", False) else logging.INFO
    )
    return watch.Watcher(config).run()


def cmd_show(args: argparse.Namespace) -> int:
    if args.generations:
        return show_generations()

    if args.generation:
        if args.path is not None:
            log.error("give a path or --generation, not both")
            return 1
        try:
            loaded, _ = generations.load(default_state_path(), args.generation)
        except (FileNotFoundError, ValueError) as exc:
            # ValueError covers SchemaError too.
            log.error("%s", exc)
            return 1
    else:
        path = args.path or default_state_path()
        try:
            loaded = model.Snapshot.loads(path.read_text())
        except FileNotFoundError:
            log.error("no snapshot at %s", path)
            return 1
        except model.SchemaError as exc:
            log.error("%s", exc)
            return 1

    print(f"captured {loaded.captured_at}")
    for instance in loaded.instances:
        print(f"konsole (pid {instance.pid} at capture time)")
        for window in instance.windows:
            print(f"  window {window.window_id}")
            if window.mapping_quality != "exact":
                print(f"    (pane identity {window.mapping_quality})")
            for index, tab in enumerate(window.tabs):
                print(f"    tab {index}  {tab.raw_hierarchy}")
                _print_node(tab.root, depth=3)
    return 0


def show_generations() -> int:
    """List retained arrangements, newest first, with what each holds."""
    kept = generations.listing(default_state_path())
    if not kept:
        print("no generations retained yet", file=sys.stderr)
        return 0

    for index, path in enumerate(kept, start=1):
        try:
            loaded = model.Snapshot.loads(path.read_text())
        except (OSError, ValueError, KeyError) as exc:
            print(f"{index}  {path.name}  (unreadable: {exc})")
            continue
        print(
            f"{index}  captured {loaded.captured_at}  "
            f"{loaded.pane_count()} pane(s) across {loaded.tab_count()} tab(s)"
        )
        print(f"   {generations.describe(loaded)}")
    # stdout, like the listing above it: sending the two to different streams
    # puts the hint before the list whenever the output is piped, because only
    # one of them is block-buffered.
    print("\nlook inside one with: kontinue show --generation N")
    print("restore one with:     kontinue restore --new-window --generation N")
    return 0


def _print_node(node: model.LayoutNode, depth: int) -> None:
    pad = "  " * depth
    if isinstance(node, model.Split):
        sizes = " / ".join(f"{value:.0f}%" for value in node.proportions)
        print(f"{pad}{node.orientation} split #{node.splitter_id}  [{sizes}]")
        for child in node.children:
            _print_node(child, depth + 1)
        return

    title = node.local_title_format or "?"
    print(f"{pad}pane view={node.view_id} session={node.session_id} profile={node.profile}")
    print(f"{pad}  title={title!r} cwd={node.cwd}")
    if node.scrollback_lines:
        print(f"{pad}  scrollback={node.scrollback_lines} lines -> {node.scrollback_file}")


def cmd_install(args: argparse.Namespace) -> int:
    path = autostart.install(interval=args.interval, no_scrollback=args.no_scrollback)
    print(f"installed {path}")
    print(f"runs: {autostart.command()} watch")

    found = autostart.processor()
    if found:
        print(f"{found} will start it at your next login.")
        print("Start it now without waiting:  kontinue watch &")
        return 0

    # The case this check exists for. A desktop with no session manager never
    # reads ~/.config/autostart, and the entry sits there doing nothing. It is
    # also the case kontinue is most needed in, because the same missing piece
    # is why Konsole does not restore itself.
    print()
    print("warning: nothing on this system runs autostart entries, so the file")
    print("         above will not start anything on its own. This is normal on a")
    print("         session started by hand rather than by a session manager,")
    print("         which is the same reason Konsole does not restore itself here.")
    print()
    print("Add this to whatever starts your desktop, before the compositor:")
    print(f"    {autostart.manual_start_line()}")
    print()
    print("Start it now:  kontinue watch &")
    return 0


def cmd_uninstall(args: argparse.Namespace) -> int:
    path = autostart.uninstall()
    if path is None:
        print("was not installed")
        return 0
    print(f"removed {path}")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    """Report whether this is actually set up to restore anything.

    Every part of kontinue fails quietly by design: a watcher that is not
    running saves nothing, an autostart entry nothing reads starts nothing, and
    a profile with fixed scrollback stores no history. None of that is visible
    until a restore comes up short, so this is the one command that answers
    "is it working" before it matters.
    """
    from . import konsole

    state = default_state_path()

    watchers = autostart.watcher_pids()
    if watchers:
        print(f"watcher:     running (pid {', '.join(str(p) for p in watchers)})")
    else:
        print("watcher:     NOT running, so nothing is being saved")

    entry = autostart.installed()
    if entry is None:
        print("autostart:   not installed  (kontinue install)")
    else:
        found = autostart.processor()
        if found:
            print(f"autostart:   installed, and {found} will run it")
        else:
            print("autostart:   installed, but NOTHING WILL RUN IT on this session")
            print(f"             add to your desktop startup:  {autostart.manual_start_line()}")

    try:
        loaded = model.Snapshot.loads(state.read_text())
    except FileNotFoundError:
        print(f"snapshot:    none yet at {state}")
        loaded = None
    except (OSError, ValueError, KeyError) as exc:
        print(f"snapshot:    unreadable ({exc})")
        loaded = None
    if loaded is not None:
        print(
            f"snapshot:    {loaded.pane_count()} pane(s) across {loaded.tab_count()} "
            f"tab(s), captured {loaded.captured_at}"
        )

    kept = generations.listing(state)
    print(f"generations: {len(kept)} retained")
    bigger = generations.outgrown(state, loaded)
    if bigger is not None:
        # The one sign of a lost session anything can see: something small was
        # saved over something large. It may have been meant, so it is said
        # rather than acted on.
        index, generation = bigger
        current = loaded.pane_count() if loaded is not None else 0
        print(
            f"             generation {index} holds {generation.pane_count()} pane(s), "
            f"the current snapshot {current}"
        )
        print(f"             ({generations.describe(generation)})")
        print(
            "             if that session was lost:  "
            f"kontinue restore --new-window --generation {index}"
        )

    store_dir = default_scrollback_dir()
    if store_dir.is_dir():
        total = sum(path.stat().st_size for path in store_dir.glob("*.cells"))
        print(f"scrollback:  {total / 1e6:.1f} MB in {store_dir}")
    else:
        print("scrollback:  nothing stored yet")

    try:
        running = konsole.discover()
    except Exception as exc:  # a broken bus is worth reporting, not raising
        print(f"konsole:     could not reach the session bus ({exc})")
        return 0
    print(f"konsole:     {len(running)} running")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if getattr(args, "verbose", False) else logging.WARNING,
        format="%(levelname)s: %(message)s",
    )

    handlers = {
        "save": cmd_save,
        "restore": cmd_restore,
        "prune": cmd_prune,
        "watch": cmd_watch,
        "show": cmd_show,
        "install": cmd_install,
        "uninstall": cmd_uninstall,
        "status": cmd_status,
    }
    return handlers[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())
