"""Keep a snapshot current, and rebuild one when Konsole reappears.

Two jobs, both of which have to happen while nobody is paying attention.

**Saving has to be periodic, because there is no last moment to save at.** A
pane's scrollback lives in files Konsole has already unlinked, so it exists only
while the process does. By the time anything could observe Konsole exiting, the
data it would want to save has gone with it. Nothing can be hooked late enough
to catch it and early enough for it to still be there, so the snapshot is taken
on a timer and the interval is simply how much can be lost.

**Restoring is triggered by an event, because polling is far too slow for it.**
The restore has to land in the gap between Konsole opening and the user typing
into it, which is a fraction of a second. ``NameOwnerChanged`` on the session
bus fires the instant a Konsole claims its name, which is early enough;
a thirty second timer would arrive to find a terminal already in use and
overwrite it.

The trigger is the zero-to-one transition: a Konsole appearing when no other is
running. That needs no "already restored" marker, because it describes a state
rather than an occasion, and it cannot fire twice for one session: after the
first restore there is a Konsole running, so the next one to open is not the
first. Opening a second terminal is left alone, which is what anyone opening a
second terminal wants.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

import dbus
import dbus.mainloop.glib
from gi.repository import GLib

from . import generations, konsole, lock, model, restore as restore_mod, snapshot
from .scrollback import Store

log = logging.getLogger(__name__)

# How long to let a Konsole finish starting before looking at it. It claims its
# bus name before its window is built, so an immediate viewHierarchy() answers
# for a window that is not there yet.
SETTLE_SECONDS = 1.0

DBUS_INTERFACE = "org.freedesktop.DBus"


@dataclass
class Config:
    state_path: Path
    scrollback_dir: Path | None
    interval: int = 45
    auto_restore: bool = True
    settle: float = SETTLE_SECONDS
    keep_generations: int = generations.DEFAULT_KEEP

    @property
    def lock_path(self) -> Path:
        return self.state_path.parent / lock.LOCK_NAME


class Watcher:
    """Snapshots on a timer, restores when the first Konsole shows up."""

    def __init__(self, config: Config, bus: dbus.Bus | None = None) -> None:
        self.config = config
        dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
        self.bus = bus if bus is not None else dbus.SessionBus()
        self.loop = GLib.MainLoop()
        self.known: set[str] = set()
        # Said once. A watcher repeating the same profile advice every tick for
        # a login's worth of ticks is noise, not a warning.
        self._warned_no_history = False
        self._warned_inferred = False

    # -- lifecycle -------------------------------------------------------

    def run(self) -> int:
        self.known = {instance.service for instance in konsole.discover(self.bus)}
        log.info(
            "watching; %d konsole(s) running, snapshotting every %ds",
            len(self.known),
            self.config.interval,
        )

        self.bus.add_signal_receiver(
            self._on_name_owner_changed,
            signal_name="NameOwnerChanged",
            dbus_interface=DBUS_INTERFACE,
        )
        GLib.timeout_add_seconds(self.config.interval, self._on_tick)

        # A Konsole already open when the watcher starts is not a zero-to-one
        # transition, so this never restores over a session already in progress.
        try:
            self.loop.run()
        except KeyboardInterrupt:
            log.info("stopping")
        return 0

    # -- saving ----------------------------------------------------------

    def _on_tick(self) -> bool:
        try:
            self.snapshot_once()
        except Exception:
            # A watcher that dies on a transient D-Bus error stops saving and
            # says nothing, and the loss only shows up when a restore is needed.
            log.exception("snapshot failed; continuing")
        return True

    def snapshot_once(self) -> snapshot.SaveResult | None:
        if lock.is_held(self.config.lock_path):
            log.debug("restore in progress; skipping this snapshot")
            return None

        result = snapshot.save(
            self.config.state_path,
            self.config.scrollback_dir,
            bus=self.bus,
            keep_generations=self.config.keep_generations,
        )
        if result is None:
            log.debug("no konsole running; keeping the previous snapshot")
            return None

        log.info(
            "snapshotted %d pane(s) across %d tab(s)",
            result.snapshot.pane_count(),
            result.snapshot.tab_count(),
        )
        inferred = any(
            window.mapping_quality != "exact"
            for instance in result.snapshot.instances
            for window in instance.windows
        )
        if inferred and not self._warned_inferred:
            self._warned_inferred = True
            log.info(
                "some split tabs have inferred pane order; per-pane details may be "
                "swapped within a tab, never between tabs"
            )
        if result.stats.panes_without_history and not self._warned_no_history:
            self._warned_no_history = True
            log.warning(
                "%d pane(s) keep scrollback in memory and cannot be saved; turn on "
                "Unlimited scrollback in the Konsole profile to have it kept",
                result.stats.panes_without_history,
            )
        return result

    # -- restoring -------------------------------------------------------

    def _on_name_owner_changed(self, name: str, old_owner: str, new_owner: str) -> None:
        name = str(name)
        if not konsole.SERVICE_RE.match(name):
            return

        if not new_owner:
            self.known.discard(name)
            log.debug("konsole %s went away", name)
            if not self.known:
                # The last one has gone, so the snapshot on disk is the closing
                # state of a finished session. That is what anybody means by
                # "the previous session", so it is kept whatever its age.
                self._retain_generation()
            return
        if name in self.known:
            return

        first = not self.known
        self.known.add(name)
        if not (first and self.config.auto_restore):
            return

        if lock.is_held(self.config.lock_path):
            log.debug("%s is the konsole this restore launched; leaving it alone", name)
            return

        log.info("%s is the first konsole running; considering a restore", name)
        GLib.timeout_add(int(self.config.settle * 1000), self._try_restore, name)

    def _try_restore(self, name: str) -> bool:
        try:
            self.restore_into(name)
        except Exception:
            log.exception("auto-restore failed")
        return False  # one shot, not a repeating timer

    def restore_into(self, name: str) -> restore_mod.Report | None:
        instance = self._instance(name)
        if instance is None:
            log.debug("%s vanished before it could be restored into", name)
            return None

        if not is_untouched(instance):
            log.info("%s is already in use; leaving it alone", name)
            return None

        try:
            loaded = model.Snapshot.loads(self.config.state_path.read_text())
        except FileNotFoundError:
            log.info("nothing saved at %s yet; nothing to restore", self.config.state_path)
            return None
        except model.SchemaError as exc:
            log.error("saved snapshot is unusable: %s", exc)
            return None

        store = (
            Store(self.config.scrollback_dir)
            if self.config.scrollback_dir is not None
            else None
        )
        try:
            with lock.held(self.config.lock_path):
                # A new window rather than this one. Restoring into the Konsole
                # that just opened would leave its default tab sitting in front
                # of the restored ones, and nothing can close a single tab from
                # outside; see restore.close_instance.
                report = restore_mod.restore(
                    loaded, store=store, bus=self.bus, reuse=False
                )
                if report.panes:
                    self._retire(instance)
        except lock.Busy:
            log.debug("a restore is already running; not starting another")
            return None

        for warning in report.warnings:
            log.warning("%s", warning)
        log.info("%s", report.summary())
        return report

    def _retain_generation(self) -> None:
        """Archive the current snapshot now that the session behind it is over."""
        if not self.config.keep_generations:
            return
        try:
            kept = generations.retain(
                self.config.state_path,
                keep=self.config.keep_generations,
                force=True,
            )
        except OSError as exc:
            log.warning("could not keep the finished session: %s", exc)
            return
        if kept is not None:
            log.info("no konsole left; kept that session as %s", kept.name)

    def _retire(self, instance: konsole.Instance) -> None:
        """Close the empty Konsole the restored one replaces.

        Only ever called after a restore that produced panes, and only against
        a Konsole :func:`is_untouched` has vouched for, so this closes a window
        holding an idle shell and nothing else.
        """
        try:
            restore_mod.close_instance(instance)
            log.info("closed the empty konsole the restore replaced")
        except OSError as exc:
            log.warning("could not close the empty konsole: %s", exc)

    def _instance(self, name: str) -> konsole.Instance | None:
        for instance in konsole.discover(self.bus):
            if instance.service == name:
                return instance
        return None


def is_untouched(instance: konsole.Instance) -> bool:
    """Whether a Konsole is still exactly as it opened.

    The whole point of restoring on the zero-to-one transition is that it lands
    before the user has done anything, so this is the check that makes the
    mistake recoverable if it does not: a Konsole with a second tab, a split, or
    any scrollback at all is one somebody is using, and it is left alone.

    Anything unreadable counts as touched. The cost of being wrong in that
    direction is a restore that has to be asked for by hand; the cost of being
    wrong in the other is a terminal replaced out from under someone.
    """
    try:
        window_ids = instance.window_ids()
        if len(window_ids) != 1:
            return False
        tabs = instance.view_hierarchy(window_ids[0])
        if len(tabs) != 1:
            return False
        sessions = instance.session_list(window_ids[0])
        if len(sessions) != 1:
            return False
    except dbus.DBusException:
        return False

    return not _has_scrollback(instance, sessions[0])


def _has_scrollback(instance: konsole.Instance, session_id: int) -> bool:
    """Whether anything has scrolled off the top of a pane yet.

    A fresh shell has printed a prompt, which sits on the visible screen and not
    in the history file, so an untouched pane's history is empty. Anything in it
    means output has already come and gone.
    """
    from . import history

    try:
        info = instance.session_info(session_id)
    except dbus.DBusException:
        return True
    if info.pts is None:
        return False

    files = history.locate(instance.pid, info.pts)
    if files is None:
        return False
    try:
        return len(history.read_index(files.index)) > 0
    except (OSError, history.HistoryFormatError):
        return True
