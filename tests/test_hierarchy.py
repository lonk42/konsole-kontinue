"""Tests for the viewHierarchy parser.

The sample layout strings here were captured from a real Konsole 26.04.3 while
splitting and closing panes, rather than invented, so they exercise the id
gaps and nesting that Konsole actually produces.
"""

from __future__ import annotations

import pytest

from kontinue.hierarchy import HierarchyParseError, Splitter, View, parse, unparse

# Captured live from Konsole 26.04.3.
SINGLE_PANE = "(0)[0]"
ONE_SPLIT = "(0)[0|1]"
NESTED = "(0)[0|(1){1|2}]"
DEEP = "(0)[(4){(6)[0|8]|6}|(1){(8)[1|10]|2}]"
GAPPED_IDS = "(7)[10]"


@pytest.mark.parametrize(
    "layout", [SINGLE_PANE, ONE_SPLIT, NESTED, DEEP, GAPPED_IDS, "(2)[3|4]"]
)
def test_round_trip(layout: str) -> None:
    """Parsing then unparsing must reproduce Konsole's string exactly."""
    assert unparse(parse(layout)) == layout


def test_single_pane() -> None:
    tree = parse(SINGLE_PANE)
    assert isinstance(tree, Splitter)
    assert tree.splitter_id == 0
    assert tree.orientation == "horizontal"
    assert tree.children == (View(0),)


def test_brace_means_vertical() -> None:
    """'[' is Qt::Horizontal and '{' is Qt::Vertical, per getChildWidgetsLayout."""
    assert parse("(0)[0|1]").orientation == "horizontal"
    assert parse("(0){0|1}").orientation == "vertical"


def test_nested_structure() -> None:
    tree = parse(NESTED)
    assert [type(child) for child in tree.children] == [View, Splitter]

    nested = tree.children[1]
    assert isinstance(nested, Splitter)
    assert nested.splitter_id == 1
    assert nested.orientation == "vertical"
    assert nested.children == (View(1), View(2))


def test_views_are_in_layout_order() -> None:
    """View order drives the pairing against sessionList, so it must be stable."""
    assert [view.view_id for view in parse(DEEP).views()] == [0, 8, 6, 1, 10, 2]


def test_splitters_includes_self_and_descendants() -> None:
    assert [s.splitter_id for s in parse(DEEP).splitters()] == [0, 4, 6, 1, 8]


def test_ids_need_not_be_contiguous() -> None:
    """Konsole never reuses view ids, so gaps are normal after closing panes."""
    tree = parse(GAPPED_IDS)
    assert tree.splitter_id == 7
    assert tree.children == (View(10),)


def test_multi_digit_ids() -> None:
    tree = parse("(123)[456|789]")
    assert tree.splitter_id == 123
    assert [view.view_id for view in tree.views()] == [456, 789]


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "(0)",  # splitter with no body
        "(0)[",  # unterminated
        "(0)[0",  # unterminated children
        "(0)[0}",  # mismatched delimiters
        "(0)[0|]",  # empty trailing child
        "()[0]",  # missing splitter id
        "(0)[0]junk",  # trailing input
        "(a)[0]",  # non-numeric id
    ],
)
def test_malformed_input_raises(bad: str) -> None:
    with pytest.raises(HierarchyParseError):
        parse(bad)


def test_error_reports_position() -> None:
    with pytest.raises(HierarchyParseError) as excinfo:
        parse("(0)[0}")
    assert excinfo.value.source == "(0)[0}"
    assert excinfo.value.position == 5
