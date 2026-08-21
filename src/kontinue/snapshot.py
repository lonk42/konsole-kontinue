"""Capture the state of every running Konsole into a :class:`Snapshot`."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

import dbus

from . import generations, history, konsole, model
from .hierarchy import HierarchyParseError, parse
from .scrollback import Owner, PruneResult, Store, write_private

log = logging.getLogger(__name__)


@dataclass
class CaptureStats:
    """What a capture could not do, for the caller to report.

    ``panes_without_history`` is the one that matters to a first-time user:
    Konsole's default profile keeps scrollback in memory, where nothing outside
    the process can reach it, so a capture of a default setup silently stores
    nothing. Counting it here is what lets the CLI say so out loud.
    """

    panes_without_history: int = 0


def capture(
    bus: dbus.Bus | None = None,
    probe: bool = False,
    store: Store | None = None,
    stats: CaptureStats | None = None,
) -> model.Snapshot:
    """Snapshot every Konsole process on the session bus.

    With ``probe`` the exact view-to-session mapping is recovered by moving the
    focus through each pane, which is only reliable while the window is active
    - see :func:`konsole.map_by_probe`. It is off by default so that scheduled
    snapshots never steal focus.

    A process that disappears mid-capture is skipped with a warning rather than
    failing the whole run: closing a window during a scheduled snapshot should
    not cost you the other windows.
    """
    instances = []
    for instance in konsole.discover(bus):
        try:
            captured = capture_instance(instance, probe=probe, store=store, stats=stats)
        except dbus.DBusException as exc:
            log.warning("skipping %s: %s", instance.service, exc)
            continue
        if captured.windows:
            instances.append(captured)
    return model.Snapshot.now(instances)


def capture_instance(
    instance: konsole.Instance,
    probe: bool = False,
    store: Store | None = None,
    stats: CaptureStats | None = None,
) -> model.Instance:
    windows = []
    for window_id in instance.window_ids():
        window = capture_window(instance, window_id, probe=probe, store=store, stats=stats)
        if window.tabs:
            windows.append(window)
    return model.Instance(pid=instance.pid, windows=windows)


def capture_window(
    instance: konsole.Instance,
    window_id: int,
    probe: bool = False,
    store: Store | None = None,
    stats: CaptureStats | None = None,
) -> model.Window:
    """Capture one window's tabs, layouts and per-pane metadata."""
    raw_tabs = instance.view_hierarchy(window_id)

    trees = []
    for raw in raw_tabs:
        try:
            trees.append((raw, parse(raw)))
        except HierarchyParseError as exc:
            # An unparseable tab is worth reporting loudly: it means Konsole
            # changed its format, and every later release will need the fix.
            log.error("could not parse tab layout: %s", exc)

    tab_views = [[view.view_id for view in tree.views()] for _, tree in trees]
    active_session_id = instance.current_session(window_id)
    mapping = _resolve_mapping(instance, window_id, tab_views, probe=probe)

    sessions: dict[int, konsole.SessionInfo] = {}
    for session_id in set(mapping.views_to_sessions.values()):
        try:
            sessions[session_id] = instance.session_info(session_id)
        except dbus.DBusException as exc:
            log.warning("could not read session %s: %s", session_id, exc)

    tabs = []
    for raw, tree in trees:
        proportions = {
            splitter.splitter_id: instance.split_proportions(window_id, splitter.splitter_id)
            for splitter in tree.splitters()
        }
        root = model.build_layout(tree, proportions)
        _attach_sessions(root, mapping.views_to_sessions, sessions)
        if store is not None:
            _attach_scrollback(root, sessions, instance.pid, store, instance, stats)
        tabs.append(model.Tab(root=root, raw_hierarchy=raw))

    return model.Window(
        window_id=window_id,
        tabs=tabs,
        active_session_id=active_session_id,
        mapping_quality=mapping.quality,
    )


def _resolve_mapping(
    instance: konsole.Instance,
    window_id: int,
    tab_views: list[list[int]],
    probe: bool,
) -> konsole.Mapping:
    """Get the best view-to-session mapping available, preferring the probe."""
    if probe:
        view_ids = [view_id for views in tab_views for view_id in views]
        with instance.preserved_focus(window_id):
            probed = konsole.map_by_probe(instance, window_id, view_ids)
        if probed is not None:
            return probed
        log.warning(
            "focus probe did not take effect (the window is probably not "
            "active); falling back to passive mapping"
        )

    mapping = konsole.map_passive(instance, window_id, tab_views)
    if mapping.quality != "exact":
        # Debug, not warning: this is a property of the snapshot, not an event,
        # and the watcher would otherwise repeat it every tick for as long as a
        # split tab is open. It is reported from `mapping_quality` instead, by
        # whoever is describing the result.
        log.debug(
            "pane order within multi-pane tabs is inferred; per-pane details "
            "may be swapped between panes of the same tab"
        )
    return mapping


def _attach_sessions(
    node: model.LayoutNode,
    view_to_session: dict[int, int],
    sessions: dict[int, konsole.SessionInfo],
) -> None:
    """Fill in per-pane session metadata in place."""
    if isinstance(node, model.Split):
        for child in node.children:
            _attach_sessions(child, view_to_session, sessions)
        return

    session_id = view_to_session.get(node.view_id)
    if session_id is None:
        log.warning("view %s has no session mapping; captured layout only", node.view_id)
        return

    node.session_id = session_id
    info = sessions.get(session_id)
    if info is None:
        return

    node.profile = info.profile
    node.cwd = info.cwd
    node.local_title_format = info.local_title_format
    node.remote_title_format = info.remote_title_format
    node.tab_color = info.tab_color


def _attach_scrollback(
    node: model.LayoutNode,
    sessions: dict[int, konsole.SessionInfo],
    konsole_pid: int,
    store: Store,
    instance: konsole.Instance | None = None,
    stats: CaptureStats | None = None,
) -> None:
    """Capture each pane's scrollback and record where it was stored.

    A pane whose profile uses a fixed scrollback size has no history files at
    all, so it is skipped rather than treated as an error.
    """
    if isinstance(node, model.Split):
        for child in node.children:
            _attach_scrollback(child, sessions, konsole_pid, store, instance, stats)
        return

    owner = owner_of(konsole_pid)

    info = sessions.get(node.session_id) if node.session_id is not None else None
    if info is None or info.pts is None:
        return

    if instance is not None:
        try:
            # The visible screen is not in the history file, so it is captured
            # separately and replayed after it.
            node.visible = instance.displayed_text(info.session_id)
        except dbus.DBusException as exc:
            log.debug("could not read the visible screen of session %s: %s",
                      info.session_id, exc)

    files = history.locate(konsole_pid, info.pts)
    if files is None:
        log.debug("no file-backed history for session %s; is its scrollback unlimited?",
                  info.session_id)
        if stats is not None:
            stats.panes_without_history += 1
        return

    try:
        offsets = history.read_index(files.index)
        entry = store.capture(
            files, history.complete_bytes(offsets), len(offsets), owner=owner
        )
    except (OSError, history.HistoryFormatError) as exc:
        log.warning("could not capture scrollback for session %s: %s", info.session_id, exc)
        return

    if entry is not None:
        node.scrollback_file = entry.filename
        node.scrollback_lines = entry.lines


@dataclass
class SaveResult:
    """What one save did, for whoever wants to report it."""

    snapshot: model.Snapshot
    path: Path
    pruned: PruneResult | None = None
    stored_bytes: int = 0
    stats: CaptureStats = field(default_factory=CaptureStats)


def save(
    path: Path,
    scrollback_dir: Path | None,
    bus: dbus.Bus | None = None,
    probe: bool = False,
    keep_generations: int = generations.DEFAULT_KEEP,
) -> SaveResult | None:
    """Capture, write and prune in one go, or do nothing at all.

    ``None`` means no Konsole was running. That is deliberately not an empty
    snapshot: writing one would replace a good record of a Konsole that has
    merely exited with a record of nothing, which is the state the next restore
    would then faithfully rebuild.

    Shared by ``kontinue save`` and the watcher so that a snapshot taken on a
    timer is the same thing as one taken by hand.
    """
    store = Store(scrollback_dir) if scrollback_dir is not None else None
    stats = CaptureStats()
    captured = capture(bus, probe=probe, store=store, stats=stats)
    if not captured.instances:
        return None

    # Archive the snapshot this one is about to replace, before replacing it.
    if keep_generations:
        generations.retain(path, keep=keep_generations)

    # Owner-only, like the scrollback store: a snapshot carries every pane's
    # visible screen, which is as much a verbatim terminal record as the
    # scrollback is, and just as likely to hold a token someone echoed.
    write_private(path, captured.dumps())

    result = SaveResult(snapshot=captured, path=path, stats=stats)
    if store is not None:
        # Generations count as referenced: a retained arrangement whose
        # scrollback has been collected still restores, but rebuilds empty.
        spare = live_scrollback_inodes(captured) | generations.referenced_inodes(path)
        result.pruned = store.prune(live_pane_inodes(bus), spare)
        store.commit()
        result.stored_bytes = store.total_bytes()
    return result


def owner_of(konsole_pid: int) -> Owner | None:
    """Identify a Konsole process well enough to survive pid reuse."""
    start_time = konsole.read_start_time(konsole_pid)
    if start_time is None:
        return None
    return Owner(konsole_pid, start_time)


def open_ptys(konsole_pid: int) -> list[str]:
    """Every pty a Konsole process is holding, one per live pane."""
    found = []
    fd_dir = Path(f"/proc/{konsole_pid}/fd")
    try:
        entries = sorted(fd_dir.iterdir(), key=lambda entry: int(entry.name))
    except OSError:
        return found
    for entry in entries:
        try:
            target = os.readlink(entry)
        except OSError:
            continue
        if target.startswith("/dev/pts/") and not target.endswith("/ptmx"):
            found.append(target)
    return found


def live_pane_inodes(bus: dbus.Bus | None = None) -> dict[Owner, set[int]]:
    """Which scrollback inodes each running Konsole currently holds open.

    This is the ground truth :meth:`Store.prune` needs. It is read from
    ``/proc`` rather than from a snapshot because the question is what exists
    right now, not what was last written down, and the two differ precisely
    when a pane has just been closed.
    """
    owners: dict[Owner, set[int]] = {}
    for instance in konsole.discover(bus):
        owner = owner_of(instance.pid)
        if owner is None:
            # The process went between discovery and here, so it owns nothing
            # we can prove. Say nothing rather than claim it holds no panes,
            # which would read as "every pane of it was closed".
            continue
        inodes = set()
        for pts in open_ptys(instance.pid):
            files = history.locate(instance.pid, pts)
            if files is not None:
                inodes.add(files.cells_inode)
        owners[owner] = inodes
    return owners


def live_scrollback_inodes(snapshot: model.Snapshot) -> set[int]:
    """Inodes still referenced by a snapshot, for pruning the store."""
    inodes = set()
    for instance in snapshot.instances:
        for window in instance.windows:
            for tab in window.tabs:
                for pane in model._walk_panes(tab.root):
                    if pane.scrollback_file:
                        inodes.add(int(pane.scrollback_file.split(".")[0]))
    return inodes
