"""Thin wrapper over Konsole's D-Bus API.

Everything kontinue knows about talking to Konsole lives here, so the rest of
the package deals in plain Python types. Konsole exposes, per process:

    org.kde.konsole-<pid>
        /Windows/<n>    org.kde.konsole.Window
        /Sessions/<n>   org.kde.konsole.Session

Several things about this interface are surprising enough to be worth stating
up front, since they shape the code below:

* ``viewHierarchy()`` reports *view* ids; sessions are addressed by *session*
  id, and no method maps between them. Worse, the two ids are allocated by
  different strategies - ``TerminalDisplay`` uses ``_id(++lastViewId)``, a
  static counter that never reuses, while ``Session`` uses ``maxSessionId + 1``
  over *live* sessions, which reuses ids after a close. So they cannot be
  paired by sorting. :func:`map_passive` and :func:`map_by_probe` are the two
  ways out, with different trade-offs.
* ``sessionList()`` groups by tab but scrambles order *within* a tab; see
  :meth:`Instance.session_list`.
* ``newSession`` is overloaded three ways on the same bus name, which confuses
  strict introspection. We always call the two-argument form explicitly.
* ``tabColor()`` returns ``'#000000'`` rather than an empty string when no
  colour is set, so the default is indistinguishable from a black tab.
"""

from __future__ import annotations

import re
from contextlib import contextmanager
from dataclasses import dataclass
import os
from pathlib import Path
from typing import Iterator, NamedTuple

import dbus

SERVICE_RE = re.compile(r"^org\.kde\.konsole-(\d+)$")

# Konsole reports this when a session has no explicit tab colour set.
UNSET_TAB_COLOR = "#000000"

# Session::title() / setTabTitleFormat() role constants, from Session.h.
LOCAL_TAB_TITLE = 0
REMOTE_TAB_TITLE = 1


class KonsoleError(RuntimeError):
    """Raised when Konsole is present but did not answer as expected."""


@dataclass(frozen=True)
class SessionInfo:
    """Everything we can learn about one pane without modifying it."""

    session_id: int
    profile: str
    cwd: str | None
    local_title_format: str
    remote_title_format: str
    tab_color: str | None
    process_id: int
    pts: str | None


class Instance:
    """One running Konsole process, addressed by its bus name."""

    def __init__(self, bus: dbus.Bus, service: str, pid: int) -> None:
        self._bus = bus
        self.service = service
        self.pid = pid

    def __repr__(self) -> str:
        return f"<Instance {self.service}>"

    # -- object plumbing -------------------------------------------------

    def _window(self, window_id: int) -> dbus.Interface:
        obj = self._bus.get_object(self.service, f"/Windows/{window_id}")
        return dbus.Interface(obj, "org.kde.konsole.Window")

    def _session(self, session_id: int) -> dbus.Interface:
        obj = self._bus.get_object(self.service, f"/Sessions/{session_id}")
        return dbus.Interface(obj, "org.kde.konsole.Session")

    def window_ids(self) -> list[int]:
        """Enumerate window object paths by introspecting the process root."""
        obj = self._bus.get_object(self.service, "/Windows")
        xml = dbus.Interface(obj, dbus.INTROSPECTABLE_IFACE).Introspect()
        return sorted(int(name) for name in re.findall(r'<node name="(\d+)"', xml))

    # -- reads -----------------------------------------------------------

    def view_hierarchy(self, window_id: int) -> list[str]:
        """One layout string per tab, in tab order."""
        return [str(entry) for entry in self._window(window_id).viewHierarchy()]

    def session_list(self, window_id: int) -> list[int]:
        """Session ids, grouped by tab in tab order.

        ``ViewManager::sessionList()`` loops over tabs and, within each,
        collects ``findChildren<TerminalDisplay *>()``. The outer loop means
        the *grouping* is reliable: the first N ids belong to the first tab.
        The inner order is Qt object-tree order, which splitting reparents,
        so within a multi-pane tab the order does not match the layout.
        """
        return [int(entry) for entry in self._window(window_id).sessionList()]

    def split_proportions(self, window_id: int, splitter_id: int) -> list[float]:
        """Child sizes of a splitter as percentages; empty if it does not exist."""
        return [float(value) for value in self._window(window_id).getSplitProportions(splitter_id)]

    def current_session(self, window_id: int) -> int:
        return int(self._window(window_id).currentSession())

    def session_info(self, session_id: int) -> SessionInfo:
        session = self._session(session_id)
        process_id = int(session.processId())
        tab_color = str(session.tabColor())

        return SessionInfo(
            session_id=session_id,
            profile=str(session.profile()),
            cwd=read_cwd(process_id),
            # Konsole stores a manually renamed tab *as* its title format, so
            # capturing the format round-trips renames. title() would instead
            # freeze the resolved string ("git : bash") into a stale literal.
            local_title_format=str(session.tabTitleFormat(LOCAL_TAB_TITLE)),
            remote_title_format=str(session.tabTitleFormat(REMOTE_TAB_TITLE)),
            tab_color=None if tab_color == UNSET_TAB_COLOR else tab_color,
            process_id=process_id,
            pts=read_pts(process_id),
        )

    # -- writes ----------------------------------------------------------

    def load_layout(self, window_id: int, path: str) -> None:
        """Append a tab built from a Konsole layout JSON file."""
        self._window(window_id).loadLayout(path)

    def resize_splits(self, window_id: int, splitter_id: int, percentages: list[float]) -> bool:
        """Set a splitter's child sizes. Rejects any percentage below 1."""
        return bool(self._window(window_id).resizeSplits(splitter_id, percentages))

    def set_tab_title_format(self, session_id: int, role: int, value: str) -> None:
        self._session(session_id).setTabTitleFormat(role, value)

    def set_tab_color(self, session_id: int, colour: str) -> None:
        self._session(session_id).setTabColor(colour)

    # -- focus walking ---------------------------------------------------

    def displayed_text(self, session_id: int) -> list[str]:
        """The pane's visible screen, which the history file does not contain.

        Konsole's history holds only lines that have scrolled off the top, so
        without this the most recent screenful of a pane is never captured.
        """
        return [str(line) for line in self._session(session_id).getAllDisplayedTextList(True)]

    def set_current_view(self, window_id: int, view_id: int) -> bool:
        return bool(self._window(window_id).setCurrentView(view_id))

    @contextmanager
    def preserved_focus(self, window_id: int) -> Iterator[None]:
        """Restore the focused session after a block that moves the focus."""
        try:
            original = self.current_session(window_id)
        except dbus.DBusException as exc:  # pragma: no cover - defensive
            raise KonsoleError(f"could not read current session: {exc}") from exc
        try:
            yield
        finally:
            try:
                self._window(window_id).setCurrentSession(original)
            except dbus.DBusException:
                # Losing focus position is annoying but must never fail a snapshot.
                pass


class Mapping(NamedTuple):
    """A view-to-session mapping and how much to trust it.

    ``quality`` is one of:

    ``exact``
        Every pane is definitely bound to the right session.
    ``inferred``
        Tab membership is certain, but the pane order within at least one
        multi-pane tab is a guess, so per-pane details (working directory,
        title) may be swapped between panes *of the same tab*.
    """

    views_to_sessions: dict[int, int]
    quality: str


def map_passive(instance: Instance, window_id: int, tabs: list[list[int]]) -> Mapping:
    """Map views to sessions without touching the UI.

    ``tabs`` is the per-tab list of view ids in layout order. Pairing those
    against :meth:`Instance.session_list`, which is grouped by tab, gives an
    exact answer for every single-pane tab. Multi-pane tabs degrade to
    ``inferred`` - see :meth:`Instance.session_list` for why the inner order
    cannot be trusted.
    """
    session_ids = instance.session_list(window_id)
    mapping: dict[int, int] = {}
    quality = "exact"
    cursor = 0

    for view_ids in tabs:
        group = session_ids[cursor : cursor + len(view_ids)]
        cursor += len(view_ids)
        if len(group) != len(view_ids):
            # Konsole disagrees with itself about how many panes exist, which
            # means the window changed mid-read. Everything after this point
            # is unreliable, so stop rather than mis-attribute.
            return Mapping(mapping, "inferred")
        if len(view_ids) > 1:
            quality = "inferred"
        mapping.update(zip(view_ids, group))

    return Mapping(mapping, quality)


def map_by_probe(instance: Instance, window_id: int, view_ids: list[int]) -> Mapping | None:
    """Map views to sessions by focusing each view in turn.

    This is exact, but only when the Konsole window can actually take focus.
    ``currentSession()`` reads ``ViewManager::_pluggedController``, which is
    updated by the ``viewFocused`` signal - a real Qt focus event. Calling
    ``setCurrentView()`` on an inactive window sets the focus widget without
    delivering that event, so every view reports the *previously* current
    session and the mapping silently collapses to a single value.

    Because that failure is silent, the result is checked for injectivity and
    ``None`` is returned when the probe clearly did not take effect. This also
    **moves the user's focus**, so it belongs behind an explicit opt-in and
    inside :meth:`Instance.preserved_focus`.
    """
    mapping: dict[int, int] = {}
    for view_id in view_ids:
        if not instance.set_current_view(window_id, view_id):
            continue
        mapping[view_id] = instance.current_session(window_id)

    if len(view_ids) > 1 and len(set(mapping.values())) < len(mapping):
        return None
    return Mapping(mapping, "exact")


def read_pts(process_id: int) -> str | None:
    """The terminal a pane's shell is attached to.

    Needed to find that pane's scrollback: Konsole's descriptor table is ordered
    per pane, so the pty identifies where a pane's history files sit in it.
    """
    try:
        target = os.readlink(f"/proc/{process_id}/fd/0")
    except OSError:
        return None
    return target if target.startswith("/dev/pts/") else None


def read_start_time(process_id: int) -> int | None:
    """A process's start time, in clock ticks since boot.

    Pids are recycled, so a pid alone cannot say whether the Konsole running
    now is the one that owned a stored scrollback. Field 22 of ``/proc/<pid>/
    stat`` never changes for the life of a process, and the pair is unique.

    The executable name sits in field 2 wrapped in brackets and may itself
    contain spaces or brackets, so the fields are counted from the *last*
    closing bracket rather than by splitting the whole line.
    """
    try:
        with open(f"/proc/{process_id}/stat", "r") as handle:
            data = handle.read()
    except OSError:
        return None
    try:
        fields = data[data.rindex(")") + 1 :].split()
        return int(fields[19])
    except (ValueError, IndexError):
        return None


def read_cwd(process_id: int) -> str | None:
    """Resolve a session's working directory from ``/proc``.

    Konsole does not expose the working directory over D-Bus - ``Session``
    tracks it internally for its own session-management code, but no
    ``Q_SCRIPTABLE`` getter exists. Reading the symlink directly avoids
    shelling out to ``pwdx`` and fails quietly on a process that has exited.
    """
    try:
        return str(Path(f"/proc/{process_id}/cwd").resolve(strict=True))
    except (OSError, RuntimeError):
        return None


def discover(bus: dbus.Bus | None = None) -> list[Instance]:
    """Find every running Konsole process on the session bus."""
    bus = bus if bus is not None else dbus.SessionBus()
    instances = []
    for name in bus.list_names():
        match = SERVICE_RE.match(str(name))
        if match:
            instances.append(Instance(bus, str(name), int(match.group(1))))
    return sorted(instances, key=lambda instance: instance.pid)
