"""Tests for turning a snapshot back into something Konsole will accept.

The parts that talk to Konsole are exercised by hand against a throwaway
instance; what is worth pinning here is the translation into Konsole's layout
format and the assumption the proportion pass rests on.
"""

from __future__ import annotations

from pathlib import Path

from kontinue import model, restore
from kontinue.hierarchy import parse


def pane(**kwargs) -> model.Pane:
    return model.Pane(view_id=kwargs.pop("view_id", 0), **kwargs)


def test_a_single_pane_tab_becomes_a_splitter_with_one_widget() -> None:
    """Konsole wraps even an unsplit tab in a splitter, and so must the layout."""
    root = model.Split(splitter_id=0, orientation="horizontal", children=[pane()])

    assert restore.to_layout(root) == {
        "Orientation": "Horizontal",
        "Widgets": [{"SessionRestoreId": 0}],
    }


def test_orientation_is_capitalised_for_konsole() -> None:
    """Konsole compares against the string "Horizontal", not "horizontal"."""
    vertical = model.Split(splitter_id=0, orientation="vertical", children=[pane()])
    assert restore.to_layout(vertical)["Orientation"] == "Vertical"


def test_nesting_is_preserved(tmp_path: Path) -> None:
    inner = model.Split(
        splitter_id=1,
        orientation="vertical",
        children=[pane(cwd=str(tmp_path)), pane(cwd=str(tmp_path))],
    )
    root = model.Split(
        splitter_id=0, orientation="horizontal", children=[pane(cwd=str(tmp_path)), inner]
    )

    layout = restore.to_layout(root)

    assert layout["Orientation"] == "Horizontal"
    assert layout["Widgets"][0] == {
        "SessionRestoreId": 0,
        "WorkingDirectory": str(tmp_path),
    }
    assert layout["Widgets"][1]["Orientation"] == "Vertical"
    assert len(layout["Widgets"][1]["Widgets"]) == 2


def test_sessions_are_created_fresh() -> None:
    """A session id from a Konsole that has since exited means nothing, so the
    layout must ask for a new session rather than name an old one."""
    root = model.Split(
        splitter_id=0, orientation="horizontal", children=[pane(session_id=42)]
    )
    assert restore.to_layout(root)["Widgets"][0]["SessionRestoreId"] == 0


def test_a_working_directory_that_has_gone_is_dropped(tmp_path: Path) -> None:
    """Konsole starts the pane in the user's home if the directory is missing,
    which is better than failing, but only if we do not pass a dead path."""
    root = model.Split(
        splitter_id=0,
        orientation="horizontal",
        children=[pane(cwd=str(tmp_path / "deleted-since-capture"))],
    )
    assert "WorkingDirectory" not in restore.to_layout(root)["Widgets"][0]


def test_a_working_directory_that_still_exists_is_kept(tmp_path: Path) -> None:
    root = model.Split(
        splitter_id=0, orientation="horizontal", children=[pane(cwd=str(tmp_path))]
    )
    assert restore.to_layout(root)["Widgets"][0]["WorkingDirectory"] == str(tmp_path)


def test_splitter_walk_order_matches_konsoles_own() -> None:
    """Sizes are applied by matching the saved tree against the rebuilt one by
    position, which only works if both are walked in the same order."""
    layout = "(0)[(4){(6)[0|8]|6}|(1){1|2}]"
    tree = parse(layout)

    saved = model.build_layout(tree, proportions={})
    assert [split.splitter_id for split in restore._walk_splits(saved)] == [
        split.splitter_id for split in tree.splitters()
    ]


def test_report_summary_mentions_scrollback_only_when_there_is_some() -> None:
    quiet = restore.Report(tabs=2, panes=3)
    assert "scrollback" not in quiet.summary()

    noisy = restore.Report(tabs=2, panes=3, scrollback_panes=3, scrollback_lines=900)
    assert "900 scrollback line(s) into 3 pane(s)" in noisy.summary()


# -- the tab that gets dropped at login -----------------------------------


class FakeInstance:
    """Just enough of a Konsole to drive the adopt path.

    ``hierarchies`` is answered in turn, so a first empty answer stands for a
    Konsole that has claimed its bus name but not yet built its window.
    """

    def __init__(self, hierarchies: list[list[str]]) -> None:
        self.hierarchies = list(hierarchies)
        self.calls = 0

    def window_ids(self) -> list[int]:
        return [1]

    def view_hierarchy(self, window_id: int) -> list[str]:
        self.calls += 1
        if len(self.hierarchies) > 1:
            return self.hierarchies.pop(0)
        return self.hierarchies[0]


def test_an_unbuilt_first_tab_is_reported_not_adopted() -> None:
    """The login race: Konsole is on the bus but its window is not up yet.

    Adopting nothing used to drop tab one entirely, splits included, while the
    rest of the tabs restored and the run reported success. The caller has to
    be told so it can rebuild the tab instead.
    """
    tab = model.Tab(
        root=model.Split(splitter_id=0, orientation="horizontal", children=[pane()]),
        raw_hierarchy="(0)[0]",
    )
    report = restore.Report()

    adopted = restore._adopt_launched_tab(
        FakeInstance([[]]), 1, tab, None, report
    )

    assert adopted is False
    assert report.tabs == 0
    assert any("rebuilding that tab" in warning for warning in report.warnings)


def test_waits_are_long_enough_to_outlast_a_login() -> None:
    """A restore lands while the whole session is starting, so five seconds
    of patience is not enough; these poll, so a fast machine pays nothing."""
    assert restore.SHELL_READY_TIMEOUT >= 15
    assert restore.WINDOW_READY_TIMEOUT >= 15
    assert restore.TAB_READY_TIMEOUT >= 15
    assert restore.KONSOLE_START_TIMEOUT >= 20


def test_await_window_gives_up_rather_than_hanging() -> None:
    """A Konsole that never builds a window must not block the restore forever."""
    import time

    instance = FakeInstance([[]])
    original = restore.WINDOW_READY_TIMEOUT
    restore.WINDOW_READY_TIMEOUT = 0.3
    try:
        started = time.monotonic()
        assert restore.await_window(instance) is False
        assert time.monotonic() - started < 5
    finally:
        restore.WINDOW_READY_TIMEOUT = original


def test_await_window_returns_as_soon_as_the_window_appears() -> None:
    instance = FakeInstance([[], [], ["(0)[0]"]])
    assert restore.await_window(instance) is True
