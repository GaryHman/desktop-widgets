"""Saved layouts, per-widget visibility, "toggle last view" and config migration.

Shared by KeepNotes Widgets and Todoist Widgets (each app ships its own identical copy of this
file, like win_hotkeys.py). Pure Python: no Qt, so all of it is unit-tested directly.

Vocabulary
* item store  - the dict in layout.json that holds one entry per widget ("notes" in KeepNotes
                Widgets, "widgets" in Todoist Widgets): {"visible", "geom", "on_top", ...}
* view        - a snapshot {key: {field: value}} of the layout fields of every widget
* layout      - a named, saved view plus an optional hotkey
"""
from __future__ import annotations

import copy
import datetime as _dt
import json
import logging
import os
import shutil
import time
from pathlib import Path
from typing import Any, Callable, Iterable

log = logging.getLogger("widget_layouts")

CONFIG_VERSION = 2
MAX_LAYOUT_NAME = 40
# Fields of one widget entry that make up its "layout". Never note content, never sync data.
BASE_FIELDS = ("visible", "geom", "on_top", "opacity", "locked")
OPACITY_STEPS = (100, 90, 80, 70, 60, 50)
MIN_VISIBLE_PX = 48  # at least this much of a widget's top strip must be on a screen


# --------------------------------------------------------------------------
# Files: load, backup, migrate (never wipe the user's config)
# --------------------------------------------------------------------------
def _stamp() -> str:
    return _dt.datetime.now().strftime("%Y%m%d-%H%M%S")


def backup_file(path: Path, tag: str) -> Path | None:
    """Copy path to '<name>.<tag>-<timestamp>.bak' next to it. Returns the copy (None if no file)."""
    if not path.exists():
        return None
    dest = path.with_name(f"{path.name}.{tag}-{_stamp()}.bak")
    n = 1
    while dest.exists():
        dest = path.with_name(f"{path.name}.{tag}-{_stamp()}-{n}.bak")
        n += 1
    shutil.copy2(path, dest)
    return dest


def atomic_write_json(path: Path, data: Any) -> None:
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=1, default=str)
    os.replace(tmp, path)


def load_config(path: Path, items_key: str, default_settings: dict) -> tuple[dict, list[str]]:
    """Read layout.json and bring it to CONFIG_VERSION.

    * missing file  -> fresh config
    * unreadable    -> the broken file is copied to *.corrupt-<time>.bak first, then defaults
    * older version -> the file is copied to *.v<old>-<time>.bak, then migrated in memory
    Unknown keys are always kept. Returns (data, notes) where notes are human-readable log lines.
    """
    notes: list[str] = []
    data: Any = {}
    if path.exists():
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
            if not isinstance(data, dict):
                raise ValueError("top level is not an object")
        except (OSError, ValueError) as exc:
            b = backup_file(path, "corrupt")
            notes.append(f"{path.name} could not be read ({exc}); kept a copy as {b.name if b else '-'}")
            data = {}
        else:
            old = data.get("version", 1)
            if not isinstance(old, int):
                old = 1
            if old < CONFIG_VERSION:
                b = backup_file(path, f"v{old}")
                notes.append(f"Upgraded {path.name} from version {old} to {CONFIG_VERSION}; "
                             f"backup: {b.name if b else '-'}")
            elif old > CONFIG_VERSION:
                notes.append(f"{path.name} is from a newer version ({old}); unknown parts are kept as-is")
    data = migrate(data, items_key, default_settings)
    return data, notes


def migrate(data: dict, items_key: str, default_settings: dict) -> dict:
    """Idempotent: fill in everything this version needs, keep everything else."""
    data = data if isinstance(data, dict) else {}
    if not isinstance(data.get(items_key), dict):
        data[items_key] = {}
    if not isinstance(data.get("settings"), dict):
        data["settings"] = {}
    s = data["settings"]
    for k, v in default_settings.items():
        s.setdefault(k, copy.deepcopy(v))
    # hotkeys: add new actions without clobbering a combination the user already uses elsewhere
    hk = s.get("hotkeys")
    if not isinstance(hk, dict):
        hk = s["hotkeys"] = {}
    for action, default in (default_settings.get("hotkeys") or {}).items():
        if action not in hk:
            used = {str(v).lower() for v in hk.values() if v}
            hk[action] = "" if default and default.lower() in used else default
    if not isinstance(data.get("layouts"), dict):
        data["layouts"] = {}
    for name, lay in list(data["layouts"].items()):
        if not isinstance(lay, dict) or not isinstance(lay.get("widgets"), dict):
            data["layouts"][name] = {"widgets": {}, "hotkey": ""}
        data["layouts"][name].setdefault("hotkey", "")
    if not isinstance(data.get("view_history"), dict):
        data["view_history"] = {"previous": None}
    data["view_history"].setdefault("previous", None)
    if s.get("active_layout") and s["active_layout"] not in data["layouts"]:
        s["active_layout"] = ""
    if not isinstance(data.get("version"), int) or data["version"] < CONFIG_VERSION:
        data["version"] = CONFIG_VERSION
    return data


# --------------------------------------------------------------------------
# Views
# --------------------------------------------------------------------------
def capture_view(items: dict, keys: Iterable[str], fields: Iterable[str]) -> dict:
    """Snapshot the layout fields of the given widget keys (missing visible -> True)."""
    fields = tuple(fields)
    view = {}
    for k in keys:
        entry = items.get(k) or {}
        snap = {f: copy.deepcopy(entry[f]) for f in fields if f in entry}
        snap.setdefault("visible", True)
        view[k] = snap
    return view


def apply_view(items: dict, view: dict, keys: Iterable[str], fields: Iterable[str],
               others: str = "hide") -> list[str]:
    """Write a view into the item store for the given keys. Returns the keys that changed.

    Widgets that are not in the view are hidden (others="hide") or left alone ("keep").
    Only layout fields are touched; nothing is deleted.
    """
    fields = tuple(fields)
    changed = []
    for k in keys:
        entry = items.setdefault(k, {})
        before = {f: entry.get(f) for f in fields}
        snap = view.get(k)
        if snap is None:
            if others == "hide":
                entry["visible"] = False
        else:
            for f in fields:
                if f in snap:
                    entry[f] = copy.deepcopy(snap[f])
                elif f not in ("visible", "geom"):
                    entry.pop(f, None)  # option not set in that view -> back to the default
        if {f: entry.get(f) for f in fields} != before:
            changed.append(k)
    return changed


def visible_keys(view: dict) -> set[str]:
    return {k for k, v in view.items() if v.get("visible", True)}


def same_view(a: dict | None, b: dict | None) -> bool:
    return (a or {}) == (b or {})


class ViewHistory:
    """'Toggle last view': remembers the view before the latest change and swaps back and forth.

    record(current) is called *before* a change. Changes with the same coalesce key within
    `window` seconds (e.g. ticking several boxes in the manager) count as one change.
    """

    def __init__(self, store: dict, clock: Callable[[], float] = time.monotonic, window: float = 4.0):
        self.store = store  # data["view_history"], persisted with layout.json
        self.clock = clock
        self.window = window
        self._last_key: str | None = None
        self._last_time = -1e9

    @property
    def previous(self) -> dict | None:
        return self.store.get("previous")

    def has_previous(self) -> bool:
        return bool(self.previous)

    def record(self, current: dict, coalesce: str | None = None) -> bool:
        now = self.clock()
        if coalesce and coalesce == self._last_key and now - self._last_time < self.window:
            self._last_time = now
            return False
        self._last_key, self._last_time = coalesce, now
        if same_view(current, self.previous) and self.previous:
            return False
        self.store["previous"] = copy.deepcopy(current)
        return True

    def swap(self, current: dict) -> dict | None:
        """Return the view to switch to and remember `current` as the new previous one."""
        prev = self.previous
        if not prev:
            return None
        self.store["previous"] = copy.deepcopy(current)
        self._last_key = None
        return copy.deepcopy(prev)


# --------------------------------------------------------------------------
# Named layouts
# --------------------------------------------------------------------------
class LayoutError(ValueError):
    pass


def clean_layout_name(name: str) -> str:
    n = " ".join((name or "").split())
    if not n:
        raise LayoutError("Enter a name for the layout.")
    if len(n) > MAX_LAYOUT_NAME:
        raise LayoutError(f"Use at most {MAX_LAYOUT_NAME} characters.")
    return n


class LayoutBook:
    """CRUD on data['layouts']. Deleting a layout only removes the layout entry, never widgets."""

    def __init__(self, data: dict):
        self.data = data

    @property
    def layouts(self) -> dict:
        return self.data.setdefault("layouts", {})

    def names(self) -> list[str]:
        return list(self.layouts)

    def _find(self, name: str) -> str | None:
        low = (name or "").strip().lower()
        return next((n for n in self.layouts if n.lower() == low), None)

    def exists(self, name: str) -> bool:
        return self._find(name) is not None

    def get(self, name: str) -> dict | None:
        n = self._find(name)
        return copy.deepcopy(self.layouts[n]) if n else None

    def save(self, name: str, view: dict, overwrite: bool = False) -> str:
        name = clean_layout_name(name)
        existing = self._find(name)
        now = _dt.datetime.now().isoformat(timespec="seconds")
        if existing and not overwrite:
            raise LayoutError(f"A layout called '{existing}' already exists.")
        if existing:
            entry = self.layouts[existing]
            entry["widgets"] = copy.deepcopy(view)
            entry["updated"] = now
            return existing
        self.layouts[name] = {"widgets": copy.deepcopy(view), "hotkey": "", "created": now, "updated": now}
        return name

    def rename(self, old: str, new: str) -> str:
        src = self._find(old)
        if not src:
            raise LayoutError(f"There is no layout called '{old}'.")
        new = clean_layout_name(new)
        clash = self._find(new)
        if clash and clash != src:
            raise LayoutError(f"A layout called '{clash}' already exists.")
        # rebuild to keep the order
        self.data["layouts"] = {(new if k == src else k): v for k, v in self.layouts.items()}
        s = self.data.get("settings", {})
        if s.get("active_layout") == src:
            s["active_layout"] = new
        return new

    def delete(self, name: str) -> dict:
        n = self._find(name)
        if not n:
            raise LayoutError(f"There is no layout called '{name}'.")
        removed = self.layouts.pop(n)
        s = self.data.get("settings", {})
        if s.get("active_layout") == n:
            s["active_layout"] = ""
        return removed

    def set_hotkey(self, name: str, text: str) -> None:
        n = self._find(name)
        if not n:
            raise LayoutError(f"There is no layout called '{name}'.")
        self.layouts[n]["hotkey"] = text or ""

    def hotkeys(self) -> dict[str, str]:
        """{'layout:<name>': 'Ctrl+Alt+1', ...} for layouts that have a hotkey."""
        return {f"layout:{n}": v.get("hotkey", "") for n, v in self.layouts.items() if v.get("hotkey")}


# --------------------------------------------------------------------------
# Monitors: keep widgets reachable when a screen disappears or changes resolution
# --------------------------------------------------------------------------
Rect = tuple[int, int, int, int]  # x, y, w, h


def _overlap(a: Rect, b: Rect) -> tuple[int, int]:
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    return (max(0, min(ax + aw, bx + bw) - max(ax, bx)), max(0, min(ay + ah, by + bh) - max(ay, by)))


def is_reachable(rect: Rect, screens: list[Rect], min_px: int = MIN_VISIBLE_PX) -> bool:
    """True if enough of the widget's top strip (its drag handle) is on some screen."""
    x, y, w, h = rect
    strip = (x, y, w, min(h, 30))
    for s in screens:
        ox, oy = _overlap(strip, s)
        if ox >= min(min_px, w) and oy >= min(10, strip[3]):
            return True
    return False


def clamp_rect(rect: Rect, screens: list[Rect], min_px: int = MIN_VISIBLE_PX) -> Rect:
    """Return rect unchanged if reachable, else moved fully onto the nearest screen (and shrunk
    to fit that screen if needed). With no screens the rect is returned unchanged."""
    if not screens or is_reachable(rect, screens, min_px):
        return rect
    x, y, w, h = rect
    cx, cy = x + w / 2, y + h / 2

    def dist(s: Rect) -> float:
        sx, sy, sw, sh = s
        dx = max(sx - cx, 0, cx - (sx + sw))
        dy = max(sy - cy, 0, cy - (sy + sh))
        return dx * dx + dy * dy

    sx, sy, sw, sh = min(screens, key=dist)
    w, h = min(w, sw), min(h, sh)
    nx = min(max(x, sx), sx + sw - w)
    ny = min(max(y, sy), sy + sh - h)
    return (int(nx), int(ny), int(w), int(h))


# --------------------------------------------------------------------------
# Settings export / import
# --------------------------------------------------------------------------
EXPORT_KIND = "widgets-settings-export"


def export_payload(data: dict, app_id: str, app_version: str) -> dict:
    """Everything in layout.json (settings, layouts, widget positions). Holds no login data."""
    out = copy.deepcopy(data)
    out["export"] = {"kind": EXPORT_KIND, "app": app_id, "app_version": app_version,
                     "exported": _dt.datetime.now().isoformat(timespec="seconds")}
    return out


def validate_import(payload: Any, app_id: str, items_key: str) -> dict:
    if not isinstance(payload, dict):
        raise LayoutError("This file is not a settings export.")
    meta = payload.get("export") or {}
    if meta.get("kind") != EXPORT_KIND:
        raise LayoutError("This file is not a settings export from this app.")
    if meta.get("app") != app_id:
        raise LayoutError(f"This export is from another app ({meta.get('app') or 'unknown'}).")
    if not isinstance(payload.get("settings"), dict) or not isinstance(payload.get(items_key, {}), dict):
        raise LayoutError("The settings export is incomplete or damaged.")
    data = copy.deepcopy(payload)
    data.pop("export", None)
    return data
