"""Rebuild a saved arrangement in a running Konsole.

The structure is handed back to Konsole rather than assembled by hand.
``ViewManager::loadLayout()`` takes a JSON description of one tab's splitter
tree and appends it as a new tab, creating a fresh session per pane and honouring
a ``WorkingDirectory`` on each, so restoring a window is one call per tab.

Two things that layout file cannot carry are applied afterwards over D-Bus: tab
titles and tab colours. Split proportions are applied afterwards too, because
the splitter ids to resize only exist once the tab has been built.

Scrollback is written straight to each new pane's terminal. The layout file does
have a ``Command`` field, but ``Session::runCommandFromLayout()`` delivers it
with ``sendText()``, which types it into the shell and leaves the command line
itself visible in the restored history. Writing to the pty puts the text on the
screen with nothing in front of it.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import signal
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

import dbus

from . import konsole, model
from .hierarchy import parse
from .scrollback import Store

log = logging.getLogger(__name__)

# How long to wait for a newly launched Konsole to claim its bus name.
# These are all generous on purpose. Every one of them polls, so a responsive
# machine waits only as long as it actually needs; the whole cost of a longer
# limit is paid in the failure case. And the failure case here is the one that
# matters most: a restore on login happens while the desktop, the browser and
# everything else in the session are starting at once, which is the slowest
# Konsole will ever be. Timing out there does not report an error to anybody,
# it silently returns a degraded arrangement, so these are set to outlast a bad
# login rather than to fail fast.
KONSOLE_START_TIMEOUT = 30.0

# A Konsole claims its bus name before it has built a window, so answering
# viewHierarchy() at that moment reports a window that does not exist yet.
WINDOW_READY_TIMEOUT = 20.0

TAB_READY_TIMEOUT = 20.0

# How long to wait for a restored pane's shell to be running and attached to a
# pty, before giving up on replaying its scrollback.
SHELL_READY_TIMEOUT = 20.0

POLL_INTERVAL = 0.1


class RestoreError(RuntimeError):
    """Restoring could not proceed."""


@dataclass
class Report:
    """What a restore actually managed to do."""

    tabs: int = 0
    panes: int = 0
    scrollback_panes: int = 0
    scrollback_lines: int = 0
    skipped_windows: int = 0
    warnings: list[str] = field(default_factory=list)

    def summary(self) -> str:
        parts = [f"restored {self.panes} pane(s) across {self.tabs} tab(s)"]
        if self.scrollback_panes:
            parts.append(
                f"replayed {self.scrollback_lines} scrollback line(s) "
                f"into {self.scrollback_panes} pane(s)"
            )
        return ", ".join(parts)


def to_layout(node: model.LayoutNode) -> dict:
    """Convert a snapshot's layout tree into Konsole's own layout JSON.

    ``SessionRestoreId: 0`` tells the restore path to create a new session
    rather than adopt a live one, which is the only sensible reading of a
    session id from a Konsole that has since exited.
    """
    if isinstance(node, model.Split):
        return {
            "Orientation": node.orientation.capitalize(),
            "Widgets": [to_layout(child) for child in node.children],
        }

    pane: dict[str, object] = {"SessionRestoreId": 0}
    if node.cwd and Path(node.cwd).is_dir():
        pane["WorkingDirectory"] = node.cwd
    return pane


def ensure_konsole(
    bus: dbus.Bus | None = None,
    reuse: bool = True,
    first_layout: str | None = None,
) -> tuple[konsole.Instance, bool]:
    """Return a Konsole to restore into, launching one if needed.

    The second element says whether the first tab has already been built. A
    freshly launched Konsole always opens one tab, so it is launched with
    ``--layout`` pointing at the first tab of the snapshot: ``Application``
    loads that instead of creating a default session, which avoids leaving an
    empty tab in front of the restored ones.
    """
    existing = konsole.discover(bus)
    if existing and reuse:
        return existing[0], False

    if shutil.which("konsole") is None:
        raise RestoreError("konsole is not on PATH")

    before = {instance.service for instance in existing}
    command = ["konsole", "--separate"]
    if first_layout is not None:
        command += ["--layout", first_layout]
    subprocess.Popen(
        command,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )

    deadline = time.monotonic() + KONSOLE_START_TIMEOUT
    while time.monotonic() < deadline:
        for instance in konsole.discover(bus):
            if instance.service not in before:
                # The name is claimed before the window exists, so returning
                # here would hand back a Konsole with nothing in it and the
                # first tab would be dropped as though the layout had failed.
                await_window(instance)
                return instance, first_layout is not None
        time.sleep(POLL_INTERVAL)

    raise RestoreError("launched konsole but it never appeared on the session bus")


def await_window(instance: konsole.Instance) -> bool:
    """Wait until a freshly launched Konsole has a window with a view in it."""
    deadline = time.monotonic() + WINDOW_READY_TIMEOUT
    while time.monotonic() < deadline:
        try:
            window_ids = instance.window_ids()
            if window_ids and instance.view_hierarchy(window_ids[0]):
                return True
        except dbus.DBusException:
            pass  # still starting; it has not exported its objects yet
        time.sleep(POLL_INTERVAL)
    return False


def restore(
    snapshot: model.Snapshot,
    store: Store | None = None,
    bus: dbus.Bus | None = None,
    reuse: bool = True,
) -> Report:
    """Rebuild a snapshot's tabs, splits and scrollback.

    Every tab is restored into a single window. A snapshot taken from several
    windows is reported rather than silently flattened, because Konsole offers
    no way to open a second window in an existing process over D-Bus.
    """
    report = Report()

    windows = [
        window
        for snapshot_instance in snapshot.instances
        for window in snapshot_instance.windows
    ]
    if not windows:
        report.warnings.append("snapshot contains no windows")
        return report

    if len(windows) > 1:
        report.skipped_windows = len(windows) - 1
        report.warnings.append(
            f"snapshot has {len(windows)} windows; restoring the first only, "
            "because a second window cannot be opened over D-Bus"
        )

    tabs = windows[0].tabs
    if not tabs:
        report.warnings.append("the window in this snapshot has no tabs")
        return report

    first_layout = _write_layout(tabs[0].root) if not reuse else None
    try:
        instance, first_done = ensure_konsole(bus, reuse=reuse, first_layout=first_layout)
    finally:
        if first_layout is not None:
            os.unlink(first_layout)

    window_id = instance.window_ids()[0]

    if first_done and _adopt_launched_tab(instance, window_id, tabs[0], store, report):
        # The launcher already built tab one; adopt it rather than adding another.
        pending = tabs[1:]
    else:
        # Either nothing was launched for us, or the launch produced no layout
        # to adopt. Both mean every tab still has to be built.
        pending = tabs

    for tab in pending:
        try:
            restore_tab(instance, window_id, tab, store, report)
        except (dbus.DBusException, OSError) as exc:
            report.warnings.append(f"could not restore a tab: {exc}")

    return report


def close_instance(instance: konsole.Instance) -> None:
    """Ask a whole Konsole process to quit.

    Individual panes cannot be closed from outside at all. Neither ``Session``
    nor ``Window`` exposes anything that closes a view, ``sendText`` is
    unavailable when Konsole's security setting is on, so ``exit`` cannot be
    typed either, and killing a pane's shell only makes Konsole keep the pane
    to report that the program died. A whole process, on the other hand, goes
    when it is asked.

    That is why auto-restore builds a new window rather than reusing an empty
    one: closing the window it displaces is possible, and tidying the tab it
    would otherwise leave behind is not.
    """
    os.kill(instance.pid, signal.SIGTERM)


def _write_layout(node: model.LayoutNode) -> str:
    handle = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
    with handle:
        json.dump(to_layout(node), handle)
    return handle.name


def _adopt_launched_tab(
    instance: konsole.Instance,
    window_id: int,
    tab: model.Tab,
    store: Store | None,
    report: Report,
) -> bool:
    """Finish the tab that ``konsole --layout`` already built.

    Returns whether it was there to adopt. A ``False`` is not fatal and must
    not be treated as one: the caller rebuilds the tab the ordinary way
    instead. Losing it silently is how a restore drops the arrangement's first
    tab, splits and all, and still reports success for the rest.
    """
    entries = instance.view_hierarchy(window_id)
    if not entries:
        report.warnings.append(
            "konsole started without the layout it was given; rebuilding that tab"
        )
        return False

    tree = parse(entries[0])
    report.tabs += 1
    _finish_tab(instance, window_id, tab, tree, store, report)
    return True


def restore_tab(
    instance: konsole.Instance,
    window_id: int,
    tab: model.Tab,
    store: Store | None,
    report: Report,
) -> None:
    """Rebuild one tab, then apply everything the layout file cannot carry."""
    before = set(_all_view_ids(instance, window_id))

    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as handle:
        json.dump(to_layout(tab.root), handle)
        layout_path = handle.name

    try:
        instance.load_layout(window_id, layout_path)
        new_tab = _await_new_tab(instance, window_id, before)
    finally:
        os.unlink(layout_path)

    if new_tab is None:
        report.warnings.append("konsole did not add the tab that was asked for")
        return

    _, tree = new_tab
    report.tabs += 1
    _finish_tab(instance, window_id, tab, tree, store, report)


def _finish_tab(
    instance: konsole.Instance,
    window_id: int,
    tab: model.Tab,
    tree,
    store: Store | None,
    report: Report,
) -> None:
    """Apply everything the layout file cannot carry to a freshly built tab."""
    view_ids = [view.view_id for view in tree.views()]
    mapping = konsole.map_passive(
        instance,
        window_id,
        [[v.view_id for v in parse(entry).views()]
         for entry in instance.view_hierarchy(window_id)],
    )

    for pane, view_id in zip(model._walk_panes(tab.root), view_ids):
        session_id = mapping.views_to_sessions.get(view_id)
        if session_id is None:
            report.warnings.append(f"restored view {view_id} has no session")
            continue
        report.panes += 1
        _apply_pane(instance, session_id, pane, store, report)

    _apply_proportions(instance, window_id, tab.root, tree, report)


def _apply_pane(
    instance: konsole.Instance,
    session_id: int,
    pane: model.Pane,
    store: Store | None,
    report: Report,
) -> None:
    """Restore a pane's naming, colour and scrollback."""
    try:
        if pane.local_title_format:
            instance.set_tab_title_format(session_id, konsole.LOCAL_TAB_TITLE,
                                          pane.local_title_format)
        if pane.remote_title_format:
            instance.set_tab_title_format(session_id, konsole.REMOTE_TAB_TITLE,
                                          pane.remote_title_format)
        if pane.tab_color:
            instance.set_tab_color(session_id, pane.tab_color)
    except dbus.DBusException as exc:
        report.warnings.append(f"could not apply naming to session {session_id}: {exc}")

    if store is None or not (pane.scrollback_file or pane.visible):
        return
    if not pane.scrollback_file:
        lines = list(pane.visible or [])
        pts = _await_shell(instance, session_id)
        if pts is not None and lines:
            try:
                _replay(pts, lines)
                report.scrollback_panes += 1
                report.scrollback_lines += len(lines)
            except OSError as exc:
                report.warnings.append(f"could not replay into {pts}: {exc}")
        return

    lines = store.read_lines(int(pane.scrollback_file.split(".")[0]), colour=True)
    # The history file holds only what scrolled off, so the screen as it was at
    # capture time is appended to make the pane whole again.
    lines = lines + (pane.visible or [])
    if not lines:
        return

    pts = _await_shell(instance, session_id)
    if pts is None:
        report.warnings.append(
            f"session {session_id} had no terminal in time; scrollback not replayed"
        )
        return

    try:
        _replay(pts, lines)
    except OSError as exc:
        report.warnings.append(f"could not replay scrollback into {pts}: {exc}")
        return

    report.scrollback_panes += 1
    report.scrollback_lines += len(lines)


def _replay(pts: str, lines: list[str]) -> None:
    """Write saved history to a pane so it lands in its scrollback.

    Writing to the pty slave appears as terminal output, exactly as if the pane
    had printed the text itself, so it scrolls and is searchable like any other
    history. Carriage returns are included because the terminal is in raw mode
    and a bare newline would step down without returning to column zero.
    """
    payload = "".join(f"{line}\r\n" for line in lines)
    fd = os.open(pts, os.O_WRONLY | os.O_NOCTTY)
    try:
        data = payload.encode("utf-8", errors="replace")
        written = 0
        while written < len(data):
            written += os.write(fd, data[written:])
    finally:
        os.close(fd)


def _apply_proportions(
    instance: konsole.Instance,
    window_id: int,
    saved: model.LayoutNode,
    live,
    report: Report,
) -> None:
    """Resize the rebuilt splitters to the proportions that were saved.

    The splitter ids in the new tab are not the saved ones, so the two trees are
    walked together and matched by position. They have the same shape because
    Konsole built the new one from the saved one.
    """
    saved_splits = [node for node in _walk_splits(saved)]
    live_splits = list(live.splitters())

    if len(saved_splits) != len(live_splits):
        report.warnings.append("restored tab has a different shape; sizes not applied")
        return

    for saved_split, live_split in zip(saved_splits, live_splits):
        if not saved_split.proportions:
            continue
        # resizeSplits rejects any percentage below 1.
        sizes = [max(1.0, value) for value in saved_split.proportions]
        try:
            instance.resize_splits(window_id, live_split.splitter_id, sizes)
        except dbus.DBusException as exc:
            report.warnings.append(f"could not size splitter {live_split.splitter_id}: {exc}")


def _walk_splits(node: model.LayoutNode):
    if isinstance(node, model.Split):
        yield node
        for child in node.children:
            yield from _walk_splits(child)


def _all_view_ids(instance: konsole.Instance, window_id: int) -> list[int]:
    ids = []
    for entry in instance.view_hierarchy(window_id):
        ids.extend(view.view_id for view in parse(entry).views())
    return ids


def _await_new_tab(instance: konsole.Instance, window_id: int, before: set[int]):
    """Wait for the tab loadLayout was asked to add, and return its tree."""
    deadline = time.monotonic() + TAB_READY_TIMEOUT
    while time.monotonic() < deadline:
        for entry in instance.view_hierarchy(window_id):
            tree = parse(entry)
            if any(view.view_id not in before for view in tree.views()):
                return entry, tree
        time.sleep(POLL_INTERVAL)
    return None


def _await_shell(instance: konsole.Instance, session_id: int) -> str | None:
    """Wait until a restored pane has a shell attached to a terminal."""
    deadline = time.monotonic() + SHELL_READY_TIMEOUT
    while time.monotonic() < deadline:
        try:
            info = instance.session_info(session_id)
        except dbus.DBusException:
            return None
        if info.pts is not None:
            return info.pts
        time.sleep(POLL_INTERVAL)
    return None
