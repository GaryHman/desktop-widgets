"""App-level tests (offscreen Qt, demo data): visibility filtering, toggle all / last view,
saved layouts, startup restore and migration of an existing layout.json."""
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

import keep_sync as ks  # noqa: E402
import keep_widgets as kw  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def pump(qapp, secs=0.3):
    end = time.time() + secs
    while time.time() < end:
        qapp.processEvents()
        time.sleep(0.01)


def make_app(qapp, directory, keep=None):
    engine = ks.SyncEngine(keep=keep or kw.build_demo_keep(), creds=ks.MemoryCredentialStore(),
                           directory=directory, offline_only=True)
    app = kw.KeepWidgetsApp(qapp, engine, demo=True)
    app.start()
    for _ in range(100):
        pump(qapp, 0.05)
        if len(app.widgets) == 4:
            break
    assert len(app.widgets) == 4
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


def by_title(app):
    return {w.note.title: w.note_id for w in app.widgets.values()}


def shown(app):
    return {k for k, w in app.widgets.items() if w.isVisible()}


def test_manager_items_and_show_only_groups(app):
    t = by_title(app)
    items = {i.title: i for i in app.manager_items()}
    assert set(items) == set(t) and all(i.visible for i in items.values())
    assert "pinned" in items["Call the plumber"].subtitle and "List" in items["Groceries"].subtitle
    groups = dict(app.show_only_groups())
    assert groups["Pinned notes"] == [t["Call the plumber"]]
    assert groups["Checklists"] == [t["Groceries"]]
    assert dict(groups["Notes with colour"])["Pink"] == [t["Gift ideas"]]
    app.show_only(groups["Checklists"])
    assert shown(app) == {t["Groceries"]}
    assert app.layout_store.note(t["Gift ideas"])["visible"] is False


def test_hidden_notes_keep_syncing(qapp, app):
    t = by_title(app)
    app.set_visible({t["Groceries"]: False})
    app.engine.keep.get(t["Groceries"]).add("Tea", False)
    app.worker.sync_now()
    pump(qapp, 1.5)
    w = app.widgets[t["Groceries"]]
    assert not w.isVisible() and "Tea" in [r.edit.text() for r in w.rows]


def test_toggle_all_restores_the_shown_set_not_everything(app):
    t = by_title(app)
    app.set_visible({t["Gift ideas"]: False, t["Groceries"]: False})
    before = shown(app)
    app.toggle_all()
    assert shown(app) == set()
    app.toggle_all()
    assert shown(app) == before
    app.hide_all()
    app.show_widgets()  # tray click
    assert shown(app) == before


def test_toggle_last_view_and_layouts(app):
    t = by_title(app)
    everything = set(app.widgets)
    assert not app.toggle_last_view(), "nothing to toggle yet"
    app.show_only({t["Groceries"]}, coalesce=None)
    app.toggle_last_view()
    assert shown(app) == everything
    app.toggle_last_view()
    assert shown(app) == {t["Groceries"]}
    app.save_layout("Shopping")
    app.show_all()
    app.load_layout("shopping")
    assert shown(app) == {t["Groceries"]} and app.active_layout() == "Shopping"
    app.toggle_last_view()
    assert shown(app) == everything


def test_restore_active_layout_on_start(qapp, tmp_path):
    keep = kw.build_demo_keep()
    a = make_app(qapp, tmp_path, keep)
    t = by_title(a)
    a.show_only({t["Gift ideas"]})
    a.save_layout("Gifts")
    a.show_all()
    a.layout_store.settings["restore_layout_on_start"] = True
    close(a)
    b = make_app(qapp, tmp_path, keep)
    try:
        assert shown(b) == {t["Gift ideas"]} and b.active_layout() == "Gifts"
    finally:
        close(b)
    b.layout_store.settings["restore_layout_on_start"] = False
    b.layout_store.save()
    c = make_app(qapp, tmp_path, keep)
    try:
        assert shown(c) == {t["Gift ideas"]}, "state was saved as it was left"
    finally:
        close(c)


def test_existing_layout_json_is_migrated_with_backup(qapp, tmp_path):
    keep = kw.build_demo_keep()
    ids = {n.title: n.id for n in keep.all()}
    v1 = {"notes": {ids["Groceries"]: {"geom": [222, 111, 300, 280], "visible": True, "on_top": False,
                                       "color": "Blue"},
                    ids["Gift ideas"]: {"visible": False}},
          "settings": {"filter": "all", "label": "", "interval": 90, "template": "Compact",
                       "hotkeys": {"toggle": "Ctrl+Alt+K", "show": "", "hide": ""}}}
    (tmp_path / "layout.json").write_text(json.dumps(v1), encoding="utf-8")
    a = make_app(qapp, tmp_path, keep)
    try:
        w = a.widgets[ids["Groceries"]]
        assert (w.x(), w.y(), w.width(), w.height()) == (222, 111, 300, 280)
        assert w.colour_name() == "Blue" and a.template_name() == "Compact"
        assert not a.widgets[ids["Gift ideas"]].isVisible()
        assert a.layout_store.settings["hotkeys"]["last_view"] == "Ctrl+Alt+Shift+K"
        assert list(tmp_path.glob("layout.json.v1-*.bak"))
        a.layout_store.save()
        saved = json.loads((tmp_path / "layout.json").read_text())
        assert saved["version"] == 2 and saved["notes"][ids["Groceries"]]["color"] == "Blue"
    finally:
        close(a)
