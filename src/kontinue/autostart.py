"""Install the watcher as a desktop autostart entry.

Restoring on login only helps if something is running at login to do it, and
until now that meant copying a file into place by hand. This turns it into
``kontinue install``.

**Why an autostart ``.desktop`` file rather than a systemd user unit.** The
watcher has to reach the session bus, and on a desktop that does not run its
own session manager the bus is wherever ``dbus-run-session`` happened to put it,
typically a random path under ``/tmp`` rather than ``$XDG_RUNTIME_DIR/bus``.
Anything started outside the session, a user unit included, cannot find it. A
KDE autostart entry is launched by the session itself and inherits the whole
environment, which is the one route that works everywhere Konsole does. That
matters most on exactly the setups this tool exists for, where there is no
session manager to restore Konsole in the first place.
"""

from __future__ import annotations

import logging
import os
import shutil
import sys
from pathlib import Path

log = logging.getLogger(__name__)

ENTRY_NAME = "kontinue-watch.desktop"

# Processes known to run XDG autostart entries. Being launched by one of these
# is the only reason an entry in ~/.config/autostart ever runs.
AUTOSTART_PROCESSORS = (
    "ksmserver",
    "plasma_session",
    "gnome-session-binary",
    "gnome-session",
    "xfce4-session",
    "lxqt-session",
    "mate-session",
    "cinnamon-session",
)


def autostart_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return Path(base) / "autostart"


def entry_path() -> Path:
    return autostart_dir() / ENTRY_NAME


def command() -> str:
    """The command the entry should run.

    ``kontinue`` on ``PATH`` when there is one, because that survives the
    interpreter moving underneath a virtualenv. Otherwise the running
    interpreter and ``-m``, which is what a ``pip install --user`` without
    ``~/.local/bin`` on ``PATH`` leaves you with, and which would otherwise
    produce an entry that silently never starts.
    """
    found = shutil.which("kontinue")
    if found:
        return found
    return f"{sys.executable} -m kontinue"


def contents(interval: int | None = None, no_scrollback: bool = False) -> str:
    exec_line = f"{command()} watch"
    if interval is not None:
        exec_line += f" --interval {interval}"
    if no_scrollback:
        exec_line += " --no-scrollback"
    return f"""[Desktop Entry]
Type=Application
Name=kontinue
Comment=Save Konsole's arrangement on a timer, and restore it when Konsole next opens
Exec={exec_line}
Icon=utilities-terminal
Terminal=false
X-KDE-autostart-phase=2
NoDisplay=true
"""


def processor(proc_root: Path | None = None) -> str | None:
    """Whatever is running that will actually launch an autostart entry.

    ``None`` means nothing will, and the entry is inert. That is not an exotic
    case: it is exactly the hand-rolled ``kwin_wayland & plasmashell`` session
    this tool exists to serve. Whatever reads ``~/.config/autostart`` is the
    same session manager whose absence stops Konsole restoring itself, so the
    setups that need kontinue most are the ones where dropping a ``.desktop``
    file into place quietly does nothing.

    Detected by looking for a running process rather than by asking the
    environment, because ``XDG_CURRENT_DESKTOP`` is set to ``KDE`` even on a
    session with no session manager at all.
    """
    proc = Path("/proc") if proc_root is None else proc_root
    if not proc.is_dir():
        return None
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            name = (entry / "comm").read_text().strip()
        except OSError:
            continue
        if name in AUTOSTART_PROCESSORS:
            return name
    return None


def manual_start_line() -> str:
    """The line to add to a session startup script when nothing autostarts."""
    return f"{command()} watch &"


def watcher_pids(proc_root: Path | None = None, uid: int | None = None) -> list[int]:
    """Pids of any ``kontinue watch`` already running for this user.

    Read from ``/proc`` rather than tracked in a pid file, because the watcher
    is started by the session and can equally be started by hand, and a stale
    pid file would report a watcher that is not there.
    """
    found = []
    proc = Path("/proc") if proc_root is None else proc_root
    want_uid = os.getuid() if uid is None else uid
    if not proc.is_dir():
        return found
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            argv = (entry / "cmdline").read_bytes().split(b"\0")
            stat = entry.stat()
        except OSError:
            continue
        if stat.st_uid != want_uid:
            continue
        parts = [part.decode("utf-8", "replace") for part in argv if part]
        if not parts:
            continue
        if "watch" not in parts:
            continue
        if any(part.endswith("kontinue") or part == "kontinue" for part in parts):
            found.append(int(entry.name))
    return found


def install(interval: int | None = None, no_scrollback: bool = False) -> Path:
    """Write the autostart entry, replacing any existing one."""
    path = entry_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(contents(interval=interval, no_scrollback=no_scrollback))
    path.chmod(0o644)
    return path


def uninstall() -> Path | None:
    """Remove the autostart entry. ``None`` if there was not one."""
    path = entry_path()
    if not path.exists():
        return None
    path.unlink()
    return path


def installed() -> Path | None:
    path = entry_path()
    return path if path.exists() else None
