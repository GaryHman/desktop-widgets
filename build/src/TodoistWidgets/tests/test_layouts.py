"""Unit tests for widget_layouts.py: config migration, views, toggle last view, saved layouts,
monitor clamping and settings export/import. Pure Python (no Qt). Shared by both apps."""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import widget_layouts as wl  # noqa: E402

ITEMS = "notes" if (Path(__file__).resolve().parents[1] / "keep_widgets.py").exists() else "widgets"
DEFAULTS = {"interval": 60, "hotkeys": {"toggle": "Ctrl+Alt+K", "show": "", "hide": "",
                                        "last_view": "Ctrl+Alt+Shift+K", "manager": ""},
            "active_layout": "", "restore_layout_on_start": False}
FIELDS = wl.BASE_FIELDS
V1 = {ITEMS: {"a": {"geom": [10, 20, 260, 240], "visible": True, "on_top": True, "color": "Pink"},
              "b": {"geom": [300, 20, 260, 240], "visible": False, "on_top": False}},
      "settings": {"filter": "label", "label": "Work", "interval": 120,
                   "hotkeys": {"toggle": "Ctrl+Alt+K", "show": "", "hide": "Ctrl+Alt+Shift+K"}},
      "something_from_the_future": {"x": 1}}


def write(path, data):
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


# ---------------------------------------------------------------- migration
def test_missing_file_gives_fresh_config(tmp_path):
    data, notes = wl.load_config(tmp_path / "layout.json", ITEMS, DEFAULTS)
    assert notes == [] and data["version"] == wl.CONFIG_VERSION
    assert data[ITEMS] == {} and data["layouts"] == {} and data["view_history"] == {"previous": None}
    assert data["settings"]["hotkeys"]["last_view"] == "Ctrl+Alt+Shift+K"
    assert list(tmp_path.iterdir()) == [], "loading must not create files"


def test_v1_config_is_backed_up_and_migrated_without_losing_anything(tmp_path):
    p = write(tmp_path / "layout.json", V1)
    data, notes = wl.load_config(p, ITEMS, DEFAULTS)
    backups = list(tmp_path.glob("layout.json.v1-*.bak"))
    assert len(backups) == 1 and json.loads(backups[0].read_text()) == V1
    assert "Upgraded" in notes[0]
    assert data[ITEMS] == V1[ITEMS], "widget positions/colours untouched"
    assert data["settings"]["filter"] == "label" and data["settings"]["interval"] == 120
    assert data["something_from_the_future"] == {"x": 1}, "unknown keys kept"
    assert data["version"] == 2 and data["layouts"] == {}
    # the user had Ctrl+Alt+Shift+K on "hide": the new default must not steal it
    assert data["settings"]["hotkeys"]["hide"] == "Ctrl+Alt+Shift+K"
    assert data["settings"]["hotkeys"]["last_view"] == ""
    assert data["settings"]["hotkeys"]["manager"] == ""
    assert json.loads(p.read_text()) == V1, "the original file is not rewritten by loading"


def test_migration_is_idempotent(tmp_path):
    p = write(tmp_path / "layout.json", V1)
    data, _ = wl.load_config(p, ITEMS, DEFAULTS)
    wl.atomic_write_json(p, data)
    data2, notes2 = wl.load_config(p, ITEMS, DEFAULTS)
    assert data2 == data and notes2 == []
    assert len(list(tmp_path.glob("*.bak"))) == 1, "no second backup for a current file"


def test_corrupt_file_is_kept_as_copy(tmp_path):
    p = tmp_path / "layout.json"
    p.write_text("{ not json", encoding="utf-8")
    data, notes = wl.load_config(p, ITEMS, DEFAULTS)
    copies = list(tmp_path.glob("layout.json.corrupt-*.bak"))
    assert len(copies) == 1 and copies[0].read_text() == "{ not json"
    assert data[ITEMS] == {} and "could not be read" in notes[0]


def test_newer_version_is_not_downgraded(tmp_path):
    p = write(tmp_path / "layout.json", {"version": 7, ITEMS: {}, "settings": {}, "new_thing": [1]})
    data, notes = wl.load_config(p, ITEMS, DEFAULTS)
    assert data["version"] == 7 and data["new_thing"] == [1] and "newer version" in notes[0]


def test_broken_parts_are_repaired_not_dropped():
    data = wl.migrate({ITEMS: [], "settings": None, "layouts": {"X": "junk", "Y": {"widgets": {"a": {}}}},
                       "view_history": 5}, ITEMS, DEFAULTS)
    assert data[ITEMS] == {} and data["settings"]["interval"] == 60
    assert data["layouts"]["X"] == {"widgets": {}, "hotkey": ""}
    assert data["layouts"]["Y"]["widgets"] == {"a": {}} and data["layouts"]["Y"]["hotkey"] == ""
    assert data["view_history"] == {"previous": None}


def test_active_layout_that_no_longer_exists_is_cleared():
    data = wl.migrate({"settings": {"active_layout": "Gone"}}, ITEMS, DEFAULTS)
    assert data["settings"]["active_layout"] == ""


# ---------------------------------------------------------------- views
def test_capture_and_apply_view():
    items = {"a": {"geom": [1, 2, 3, 4], "visible": True, "on_top": True, "opacity": 80, "text": "x"},
             "b": {"geom": [5, 6, 7, 8]}, "c": {"visible": True}}
    view = wl.capture_view(items, ["a", "b"], FIELDS)
    assert view == {"a": {"geom": [1, 2, 3, 4], "visible": True, "on_top": True, "opacity": 80},
                    "b": {"geom": [5, 6, 7, 8], "visible": True}}
    assert "text" not in view["a"], "only layout fields are captured"
    items["a"].update(visible=False, geom=[9, 9, 9, 9])
    items["a"].pop("opacity")
    items["b"]["locked"] = True
    changed = wl.apply_view(items, view, ["a", "b", "c"], FIELDS, others="hide")
    assert sorted(changed) == ["a", "b", "c"]
    assert items["a"] == {"geom": [1, 2, 3, 4], "visible": True, "on_top": True, "opacity": 80, "text": "x"}
    assert "locked" not in items["b"], "option not in the view goes back to the default"
    assert items["c"]["visible"] is False, "widgets not in a layout are hidden"
    items["c"]["visible"] = True
    assert wl.apply_view(items, view, ["a", "b", "c"], FIELDS, others="keep") == []
    assert items["c"]["visible"] is True


def test_visibility_filtering_helpers():
    view = {"a": {"visible": True}, "b": {"visible": False}, "c": {}}
    assert wl.visible_keys(view) == {"a", "c"}


# ---------------------------------------------------------------- toggle last view
class Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


def test_toggle_last_view_swaps_back_and_forth():
    store = {"previous": None}
    h = wl.ViewHistory(store, clock=Clock())
    assert not h.has_previous() and h.swap({"a": {"visible": True}}) is None
    v1 = {"a": {"visible": True}, "b": {"visible": True}}
    v2 = {"a": {"visible": True}, "b": {"visible": False}}
    assert h.record(v1)  # before changing v1 -> v2
    assert h.swap(v2) == v1 and store["previous"] == v2
    assert h.swap(v1) == v2 and store["previous"] == v1
    v1["a"]["visible"] = False
    assert store["previous"]["a"]["visible"] is True, "history keeps its own copy"


def test_coalesced_changes_count_as_one():
    clk = Clock()
    h = wl.ViewHistory({"previous": None}, clock=clk)
    v0, v1 = {"a": {"visible": True}}, {"a": {"visible": False}}
    assert h.record(v0, coalesce="manager")
    clk.t += 1
    assert not h.record(v1, coalesce="manager"), "second tick in the manager is the same change"
    assert h.previous == v0
    clk.t += 10
    assert h.record(v1, coalesce="manager"), "after the window it is a new change"
    assert h.previous == v1
    assert h.record(v0) and h.previous == v0, "no coalesce key: always recorded"


# ---------------------------------------------------------------- saved layouts
def test_layout_crud():
    data = wl.migrate({}, ITEMS, DEFAULTS)
    book = wl.LayoutBook(data)
    v = {"a": {"visible": True, "geom": [1, 2, 3, 4]}}
    assert book.save("  Work   mode ", v) == "Work mode"
    assert book.names() == ["Work mode"] and book.get("work MODE")["widgets"] == v
    with pytest.raises(wl.LayoutError):
        book.save("WORK MODE", v)
    book.save("work mode", {"a": {"visible": False}}, overwrite=True)
    assert book.get("Work mode")["widgets"] == {"a": {"visible": False}} and book.names() == ["Work mode"]
    book.save("Home", v)
    book.save("Focus", v)
    data["settings"]["active_layout"] = "Home"
    assert book.rename("home", "Evening") == "Evening"
    assert book.names() == ["Work mode", "Evening", "Focus"], "rename keeps the order"
    assert data["settings"]["active_layout"] == "Evening"
    with pytest.raises(wl.LayoutError):
        book.rename("Focus", "evening")
    assert book.rename("Focus", "FOCUS") == "FOCUS", "case-only rename is allowed"
    book.set_hotkey("Evening", "Ctrl+Alt+2")
    assert book.hotkeys() == {"layout:Evening": "Ctrl+Alt+2"}
    data[ITEMS]["a"] = {"visible": True, "geom": [1, 2, 3, 4]}
    removed = book.delete("Evening")
    assert removed["hotkey"] == "Ctrl+Alt+2" and data["settings"]["active_layout"] == ""
    assert book.names() == ["Work mode", "FOCUS"] and data[ITEMS]["a"]["visible"] is True, \
        "deleting a layout never touches the widgets"
    with pytest.raises(wl.LayoutError):
        book.delete("Evening")


@pytest.mark.parametrize("bad", ["", "   ", "x" * (wl.MAX_LAYOUT_NAME + 1)])
def test_bad_layout_names(bad):
    with pytest.raises(wl.LayoutError):
        wl.clean_layout_name(bad)


# ---------------------------------------------------------------- monitors
SCREENS = [(0, 0, 1920, 1040), (1920, 0, 1280, 984)]


def test_rect_on_screen_is_unchanged():
    assert wl.clamp_rect((100, 100, 260, 240), SCREENS) == (100, 100, 260, 240)
    assert wl.clamp_rect((1900, 50, 260, 240), SCREENS) == (1900, 50, 260, 240), "spanning two screens"
    assert wl.clamp_rect((1870, 50, 260, 240), SCREENS[:1]) == (1870, 50, 260, 240), "50px visible"
    assert wl.clamp_rect((1890, 50, 260, 240), SCREENS[:1]) == (1660, 50, 260, 240), "30px is too little"


def test_rect_on_removed_monitor_moves_to_nearest_screen():
    assert wl.clamp_rect((2500, 300, 260, 240), SCREENS[:1]) == (1660, 300, 260, 240)
    assert wl.clamp_rect((-3000, -500, 260, 240), SCREENS) == (0, 0, 260, 240)
    # header above the screen top: the drag handle is unreachable -> moved down
    assert wl.clamp_rect((100, -200, 260, 240), SCREENS) == (100, 0, 260, 240)


def test_rect_bigger_than_screen_is_shrunk():
    assert wl.clamp_rect((4000, 0, 3000, 2000), [(0, 0, 1280, 720)]) == (0, 0, 1280, 720)
    assert wl.clamp_rect((5, 5, 10, 10), []) == (5, 5, 10, 10), "no screen info -> unchanged"


# ---------------------------------------------------------------- export / import
def test_export_import_roundtrip():
    data = wl.migrate(json.loads(json.dumps(V1)), ITEMS, DEFAULTS)
    wl.LayoutBook(data).save("Work", {"a": {"visible": True}})
    payload = wl.export_payload(data, "my-app", "1.2.3")
    assert payload["export"]["app"] == "my-app" and "export" not in data
    back = wl.validate_import(json.loads(json.dumps(payload)), "my-app", ITEMS)
    assert back == json.loads(json.dumps(data))


@pytest.mark.parametrize("payload, msg", [
    ([], "not a settings export"),
    ({"settings": {}}, "not a settings export"),
    ({"export": {"kind": wl.EXPORT_KIND, "app": "other"}, "settings": {}}, "another app"),
    ({"export": {"kind": wl.EXPORT_KIND, "app": "my-app"}, "settings": 3}, "incomplete"),
])
def test_bad_imports_are_refused(payload, msg):
    with pytest.raises(wl.LayoutError, match=msg):
        wl.validate_import(payload, "my-app", ITEMS)


def test_backup_file_never_overwrites(tmp_path):
    p = write(tmp_path / "layout.json", {"a": 1})
    b1 = wl.backup_file(p, "x")
    b2 = wl.backup_file(p, "x")
    assert b1 != b2 and b1.exists() and b2.exists()
    assert wl.backup_file(tmp_path / "missing.json", "x") is None
