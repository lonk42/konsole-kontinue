"""Parser for Konsole's ``viewHierarchy()`` layout strings.

Konsole describes each tab's splitter tree as a compact string, produced by
``ViewSplitter::getChildWidgetsLayout()``::

    QString ViewSplitter::getChildWidgetsLayout()
    {
        // children joined by '|', each either a view id or a nested splitter
        if (orientation() == Qt::Orientation::Horizontal)
            layoutString = '[' + layoutString + ']';
        else
            layoutString = '{' + layoutString + '}';
        return QStringLiteral("(%1)").arg(id()) + layoutString;
    }

which gives the grammar::

    node     := view | splitter
    view     := INT                       # a TerminalDisplay id
    splitter := '(' INT ')' body
    body     := '[' children ']'          # Qt::Horizontal
              | '{' children '}'          # Qt::Vertical
    children := node ('|' node)*

So ``(0)[0|(1){1|2}]`` is splitter 0, laid out horizontally, holding view 0
alongside splitter 1, which stacks views 1 and 2 vertically.

Note that the ids here are *view* ids, which are not session ids. Konsole
exposes no direct mapping between the two; see ``konsole.map_views_to_sessions``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterator, Literal, Union

Orientation = Literal["horizontal", "vertical"]

# Konsole's delimiters encode the splitter orientation.
_OPEN_TO_ORIENTATION: dict[str, Orientation] = {"[": "horizontal", "{": "vertical"}
_ORIENTATION_TO_DELIMS: dict[Orientation, tuple[str, str]] = {
    "horizontal": ("[", "]"),
    "vertical": ("{", "}"),
}
_CLOSE_FOR = {"[": "]", "{": "}"}


class HierarchyParseError(ValueError):
    """Raised when a layout string does not match Konsole's grammar."""

    def __init__(self, message: str, source: str, position: int) -> None:
        super().__init__(f"{message} at position {position} in {source!r}")
        self.source = source
        self.position = position


@dataclass(frozen=True)
class View:
    """A single terminal pane, identified by its Konsole view id."""

    view_id: int

    def views(self) -> Iterator[View]:
        yield self

    def splitters(self) -> Iterator[Splitter]:
        return iter(())

    def unparse(self) -> str:
        return str(self.view_id)


@dataclass(frozen=True)
class Splitter:
    """A splitter node holding two or more children in one orientation."""

    splitter_id: int
    orientation: Orientation
    children: tuple[Node, ...] = field(default_factory=tuple)

    def views(self) -> Iterator[View]:
        for child in self.children:
            yield from child.views()

    def splitters(self) -> Iterator[Splitter]:
        yield self
        for child in self.children:
            yield from child.splitters()

    def unparse(self) -> str:
        open_delim, close_delim = _ORIENTATION_TO_DELIMS[self.orientation]
        inner = "|".join(child.unparse() for child in self.children)
        return f"({self.splitter_id}){open_delim}{inner}{close_delim}"


Node = Union[View, Splitter]


class _Parser:
    def __init__(self, source: str) -> None:
        self._source = source
        self._pos = 0

    def parse(self) -> Node:
        node = self._parse_node()
        if self._pos != len(self._source):
            self._fail("trailing input")
        return node

    # -- grammar ---------------------------------------------------------

    def _parse_node(self) -> Node:
        if self._peek() == "(":
            return self._parse_splitter()
        return View(self._parse_int())

    def _parse_splitter(self) -> Splitter:
        self._expect("(")
        splitter_id = self._parse_int()
        self._expect(")")

        open_delim = self._peek()
        if open_delim not in _OPEN_TO_ORIENTATION:
            self._fail("expected '[' or '{' after splitter id")
        self._advance()

        children = [self._parse_node()]
        while self._peek() == "|":
            self._advance()
            children.append(self._parse_node())

        self._expect(_CLOSE_FOR[open_delim])
        return Splitter(
            splitter_id=splitter_id,
            orientation=_OPEN_TO_ORIENTATION[open_delim],
            children=tuple(children),
        )

    # -- primitives ------------------------------------------------------

    def _parse_int(self) -> int:
        start = self._pos
        while self._peek() is not None and self._source[self._pos].isdigit():
            self._pos += 1
        if start == self._pos:
            self._fail("expected an integer")
        return int(self._source[start : self._pos])

    def _peek(self) -> str | None:
        if self._pos >= len(self._source):
            return None
        return self._source[self._pos]

    def _advance(self) -> None:
        self._pos += 1

    def _expect(self, char: str) -> None:
        if self._peek() != char:
            self._fail(f"expected {char!r}")
        self._advance()

    def _fail(self, message: str) -> None:
        raise HierarchyParseError(message, self._source, self._pos)


def parse(layout: str) -> Node:
    """Parse one tab's layout string into a tree.

    A tab with no splits is a bare splitter holding a single view, e.g.
    ``(0)[0]``. Konsole always wraps the tab root in a splitter, so the
    result is a :class:`Splitter` in practice, but a lone view parses too.
    """
    return _Parser(layout).parse()


def unparse(node: Node) -> str:
    """Render a tree back to Konsole's layout string. Inverse of :func:`parse`."""
    return node.unparse()
