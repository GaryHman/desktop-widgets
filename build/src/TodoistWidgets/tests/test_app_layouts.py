"""App-level tests (offscreen Qt, demo data): visibility filtering, toggle all / last view,
saved layouts (incl. accent colour), startup restore and migration of an existing layout.json."""
import datetime as dt
import json
import os
import sys
import time
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from PySide6.QtWidgets import QApplication  # noqa: E402

import todoist_widgets as tw  # noqa: E402

TODAY = dt.date(2026, 10, 22)
ALL = {"project:inbox", "project:work", "project:home"}


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def pump(qapp, secs=0.3):
    end = time.time() + secs
    while time.time() < end:
        qapp.processEvents()
        time.sleep(0.01)


def make_app(qapp, directory):
    app = tw.TodoistWidgetsApp(qapp, tw.build_demo_engine(directory, today=TODAY), demo=True)
    app.start()
    for _ in range(100):
        pump(qapp, 0.05)
        if set(app.widgets) == ALL:
            break
    assert set(app.widgets) == ALL
    return app


def close(app):
    app.quit()
    for w in list(app.widgets.values()):
        w.close()


@pytest.fixture
def app(qapp, tmp_path):
    a = make_app(qapp, tmp_path)
    yield a
    close(a)


def shown(app):
    return {k for k, w in app.widgets.items() if w.isVisible()}


def test_manager_items_and_show_only_groups(app):
    items = {i.key: i for i in app.manager_items()}
    assert set(items) == ALL and items["project:work"].title == "Work"
    assert items["project:work"].subtitle == "Project · 6 open"
    g = dict(app.show_only_groups())
    assert set(g["Projects"]) == ALL and g["Sections"] == []
    assert set(g["Widgets with priority 1 tasks"]) == {"project:work", "project:home"}
    app.show_only(g["Widgets with priority 1 tasks"])
    assert shown(app) == {"project:work", "project:home"}
    assert app.layout_store.widget("project:inbox")["visible"] is False


def test_toggle_all_restores_the_shown_set(app):
    app.set_visible({"project:inbox": False})
    app.toggle_all()
    assert shown(app) == set()
    app.toggle_all()
    assert shown(app) == {"project:work", "project:home"}


def test_layout_carries_accent_colour_and_toggle_last_view(app):
    work = app.widgets["project:work"]
    app.show_only({"project:work"}, coalesce=None)
    work.set_colour("teal")
    app.save_layout("Work only")
    work.set_colour(None)
    app.show_all()
    assert work.accent_colour() == "#4073FF"
    app.load_layout("Work only")
    assert shown(app) == {"project:work"} and work.accent_colour() == "#158FAD"
    app.toggle_last_view()
    assert shown(app) == ALL and work.accent_colour() == "#4073FF"


def test_restore_active_layout_on_start(qapp, tmp_path):
    a = make_app(qapp, tmp_path)
    a.show_only({"project:home"})
    a.save_layout("Home")
    a.show_all()
    a.layout_store.settings["restore_layout_on_start"] = True
    close(a)
    b = make_app(qapp, tmp_path)
    try:
        assert shown(b) == {"project:home"}
    finally:
        close(b)


def test_existing_layout_json_is_migrated_with_backup(qapp, tmp_path):
    v1 = {"widgets": {"project:work": {"geom": [222, 111, 340, 380], "visible": True, "on_top": False,
                                       "color": "Purple"},
                      "project:inbox": {"visible": False}},
          "settings": {"mode": "project", "theme": "Dark", "interval": 90, "show_completed": [],
                       "hotkeys": {"toggle": "Ctrl+Alt+T", "show": "Ctrl+Alt+Shift+T", "hide": ""}}}
    (tmp_path / "layout.json").write_text(json.dumps(v1), encoding="utf-8")
    a = make_app(qapp, tmp_path)
    try:
        w = a.widgets["project:work"]
        assert (w.x(), w.y(), w.width(), w.height()) == (222, 111, 340, 380)
        assert w.accent_colour() == "#884DFF", "legacy colour override still works"
        assert a.theme_name() == "Dark" and not a.widgets["project:inbox"].isVisible()
        hk = a.layout_store.settings["hotkeys"]
        assert hk["show"] == "Ctrl+Alt+Shift+T" and hk["last_view"] == "", "user's hotkey not stolen"
        assert list(tmp_path.glob("layout.json.v1-*.bak"))
    finally:
        close(a)
