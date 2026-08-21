"""The on-disk snapshot format.

A snapshot is plain JSON so it stays diffable and hand-editable - you should be
able to open one, fix a stale path, and restore from it. The schema mirrors
Konsole's own structure: instance -> window -> tab -> splitter tree -> pane.

``SCHEMA_VERSION`` is bumped whenever an older file can no longer be read
as-is. Restores refuse a version they do not understand rather than guessing.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Union

from .hierarchy import Node, Orientation, Splitter, View

SCHEMA_VERSION = 1


class SchemaError(ValueError):
    """Raised when a snapshot file is not readable by this version."""


@dataclass
class Pane:
    """A terminal pane: one Konsole view bound to one session."""

    view_id: int
    session_id: int | None = None
    profile: str | None = None
    cwd: str | None = None
    local_title_format: str | None = None
    remote_title_format: str | None = None
    tab_color: str | None = None
    scrollback_file: str | None = None
    scrollback_lines: int | None = None
    visible: list[str] | None = None

    def to_json(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"kind": "pane", "view_id": self.view_id}
        for key in (
            "session_id",
            "profile",
            "cwd",
            "local_title_format",
            "remote_title_format",
            "tab_color",
            "scrollback_file",
            "scrollback_lines",
            "visible",
        ):
            value = getattr(self, key)
            if value is not None:
                payload[key] = value
        return payload

    @classmethod
    def from_json(cls, payload: dict[str, Any]) -> Pane:
        return cls(
            view_id=payload["view_id"],
            session_id=payload.get("session_id"),
            profile=payload.get("profile"),
            cwd=payload.get("cwd"),
            local_title_format=payload.get("local_title_format"),
            remote_title_format=payload.get("remote_title_format"),
            tab_color=payload.get("tab_color"),
            scrollback_file=payload.get("scrollback_file"),
            scrollback_lines=payload.get("scrollback_lines"),
            visible=payload.get("visible"),
        )


@dataclass
class Split:
    """A splitter and its children, with the sizes needed to rebuild it."""

    splitter_id: int
    orientation: Orientation
    proportions: list[float] = field(default_factory=list)
    children: list[LayoutNode] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return {
            "kind": "split",
            "splitter_id": self.splitter_id,
            "orientation": self.orientation,
            "proportions": [round(value, 4) for value in self.proportions],
            "children": [child.to_json() for child in self.children],
        }

    @classmethod
    def from_json(cls, payload: dict[str, Any]) -> Split:
        return cls(
            splitter_id=payload["splitter_id"],
            orientation=payload["orientation"],
            proportions=list(payload.get("proportions", [])),
            children=[_node_from_json(child) for child in payload.get("children", [])],
        )


LayoutNode = Union[Pane, Split]


def _node_from_json(payload: dict[str, Any]) -> LayoutNode:
    kind = payload.get("kind")
    if kind == "pane":
        return Pane.from_json(payload)
    if kind == "split":
        return Split.from_json(payload)
    raise SchemaError(f"unknown layout node kind: {kind!r}")


@dataclass
class Tab:
    """One tab: a layout tree, plus the raw string it was built from.

    ``raw_hierarchy`` is kept verbatim so a snapshot stays debuggable when the
    parser and Konsole disagree - you can always see what Konsole actually said.
    """

    root: LayoutNode
    raw_hierarchy: str

    def to_json(self) -> dict[str, Any]:
        return {"raw_hierarchy": self.raw_hierarchy, "root": self.root.to_json()}

    @classmethod
    def from_json(cls, payload: dict[str, Any]) -> Tab:
        return cls(root=_node_from_json(payload["root"]), raw_hierarchy=payload["raw_hierarchy"])


@dataclass
class Window:
    """One Konsole window.

    ``mapping_quality`` records how the panes were bound to their sessions:
    ``exact`` means every pane is certainly correct, ``inferred`` means
    per-pane details may be swapped between panes of the same multi-pane tab.
    A restore should surface this rather than pretend the data is clean.
    """

    window_id: int
    tabs: list[Tab] = field(default_factory=list)
    active_session_id: int | None = None
    mapping_quality: str = "exact"

    def to_json(self) -> dict[str, Any]:
        return {
            "window_id": self.window_id,
            "active_session_id": self.active_session_id,
            "mapping_quality": self.mapping_quality,
            "tabs": [tab.to_json() for tab in self.tabs],
        }

    @classmethod
    def from_json(cls, payload: dict[str, Any]) -> Window:
        return cls(
            window_id=payload["window_id"],
            tabs=[Tab.from_json(tab) for tab in payload.get("tabs", [])],
            active_session_id=payload.get("active_session_id"),
            mapping_quality=payload.get("mapping_quality", "exact"),
        )


@dataclass
class Instance:
    """One Konsole process. The pid is recorded for debugging only - it is
    meaningless after a restart and must never be used to match on restore."""

    pid: int
    windows: list[Window] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return {"pid": self.pid, "windows": [window.to_json() for window in self.windows]}

    @classmethod
    def from_json(cls, payload: dict[str, Any]) -> Instance:
        return cls(
            pid=payload["pid"],
            windows=[Window.from_json(window) for window in payload.get("windows", [])],
        )


@dataclass
class Snapshot:
    captured_at: str
    instances: list[Instance] = field(default_factory=list)
    schema_version: int = SCHEMA_VERSION

    @classmethod
    def now(cls, instances: list[Instance]) -> Snapshot:
        return cls(
            captured_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            instances=instances,
        )

    def pane_count(self) -> int:
        return sum(
            len(list(_walk_panes(tab.root)))
            for instance in self.instances
            for window in instance.windows
            for tab in window.tabs
        )

    def tab_count(self) -> int:
        return sum(len(window.tabs) for instance in self.instances for window in instance.windows)

    def to_json(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "captured_at": self.captured_at,
            "instances": [instance.to_json() for instance in self.instances],
        }

    def dumps(self) -> str:
        return json.dumps(self.to_json(), indent=2) + "\n"

    @classmethod
    def loads(cls, text: str) -> Snapshot:
        payload = json.loads(text)
        version = payload.get("schema_version")
        if version != SCHEMA_VERSION:
            raise SchemaError(
                f"snapshot schema version {version!r} is not readable by "
                f"kontinue (expects {SCHEMA_VERSION})"
            )
        return cls(
            captured_at=payload["captured_at"],
            instances=[Instance.from_json(item) for item in payload.get("instances", [])],
            schema_version=version,
        )


def _walk_panes(node: LayoutNode):
    if isinstance(node, Pane):
        yield node
    else:
        for child in node.children:
            yield from _walk_panes(child)


def build_layout(node: Node, proportions: dict[int, list[float]]) -> LayoutNode:
    """Convert a parsed hierarchy tree into snapshot nodes.

    Panes come out bare - only the view id is known at this point. The snapshot
    pass fills in session metadata once the view-to-session mapping is resolved.
    """
    if isinstance(node, View):
        return Pane(view_id=node.view_id)
    if isinstance(node, Splitter):
        return Split(
            splitter_id=node.splitter_id,
            orientation=node.orientation,
            proportions=proportions.get(node.splitter_id, []),
            children=[build_layout(child, proportions) for child in node.children],
        )
    raise SchemaError(f"unexpected hierarchy node: {node!r}")
