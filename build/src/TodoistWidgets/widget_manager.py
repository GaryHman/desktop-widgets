"""Widget visibility, saved layouts and "toggle last view" for the desktop-widget apps (Qt part).

Shared by KeepNotes Widgets and Todoist Widgets (each app ships its own identical copy of this
file). The pure logic lives in widget_layouts.py; this file wires it to windows, the tray menu
and the "Widgets & layouts" manager window.

The app class mixes in LayoutHostMixin and provides:
  layout_store (.data, .items, .item(key), .save_soon(), .save()), widgets {key: widget}, tray,
  hotkeys, APP_TITLE, NOUN / NOUN_PLURAL, LAYOUT_FIELDS, app_icon(),
  manager_items() -> [ManagerItem], show_only_groups() -> [(label, keys | [(label, keys)])]
Every widget mixes in WidgetStateMixin and has .key, .lay, .grip.
"""
from __future__ import annotations

import platform
from dataclasses import dataclass, field

from PySide6.QtCore import QTimer, Qt
from PySide6.QtGui import QAction, QGuiApplication, QKeySequence
from PySide6.QtWidgets import (QCheckBox, QDialog, QDialogButtonBox, QGridLayout, QGroupBox,
                               QHBoxLayout, QInputDialog, QKeySequenceEdit, QLabel, QLineEdit,
                               QListWidget, QListWidgetItem, QMenu, QMessageBox, QPushButton,
                               QSystemTrayIcon, QToolButton, QVBoxLayout)

import widget_layouts as wl
import win_hotkeys as hk

TRAY_LIST_LIMIT = 40


@dataclass
class ManagerItem:
    key: str
    title: str
    subtitle: str = ""
    visible: bool = True
    search: str = ""  # extra text matched by the search box (labels, project name, ...)
    tags: set = field(default_factory=set)


def screen_rects() -> list[wl.Rect]:
    out = []
    for s in QGuiApplication.screens():
        g = s.availableGeometry()
        out.append((g.x(), g.y(), g.width(), g.height()))
    return out


# --------------------------------------------------------------------------
# Per-widget window state
# --------------------------------------------------------------------------
class WidgetStateMixin:
    """Applies the layout fields in self.lay (visible, geom, on_top, opacity, locked) to the window."""

    def apply_state(self) -> None:
        lay = self.lay
        g = lay.get("geom")
        if isinstance(g, (list, tuple)) and len(g) == 4:
            x, y, w, h = wl.clamp_rect(tuple(int(v) for v in g), screen_rects())
            w, h = max(self.minimumWidth(), w), max(self.minimumHeight(), h)
            cur = self.geometry()
            if (cur.x(), cur.y(), cur.width(), cur.height()) != (x, y, w, h):
                self.resize(w, h)
                self.move(x, y)
        want_top = bool(lay.get("on_top", True))
        if bool(self.windowFlags() & Qt.WindowStaysOnTopHint) != want_top:
            self.setWindowFlag(Qt.WindowStaysOnTopHint, want_top)
        self.apply_opacity()
        self.apply_locked()
        if lay.get("visible", True):
            if not self.isVisible():
                self.show()
        elif self.isVisible():
            self.hide()

    def apply_opacity(self) -> None:
        try:
            pct = int(self.lay.get("opacity", 100))
        except (TypeError, ValueError):
            pct = 100
        pct = min(100, max(20, pct))
        if abs(self.windowOpacity() - pct / 100) > 0.004:
            self.setWindowOpacity(pct / 100)

    def apply_locked(self) -> None:
        self.grip.setVisible(not self.lay.get("locked", False))

    def is_locked(self) -> bool:
        return bool(self.lay.get("locked", False))

    def add_layout_actions(self, menu: QMenu) -> None:
        """Opacity + lock entries for the widget's ⋯ menu."""
        op = menu.addMenu("Opacity")
        cur = int(self.lay.get("opacity", 100) or 100)
        for pct in wl.OPACITY_STEPS:
            a = op.addAction(f"{pct}%")
            a.setCheckable(True)
            a.setChecked(pct == cur)
            a.triggered.connect(lambda _=False, p=pct: self.set_opacity(p))
        lock = menu.addAction("Lock position and size")
        lock.setCheckable(True)
        lock.setChecked(self.is_locked())
        lock.triggered.connect(self.set_locked)

    def set_opacity(self, pct: int) -> None:
        if pct >= 100:
            self.lay.pop("opacity", None)
        else:
            self.lay["opacity"] = int(pct)
        self.app.layout_store.save_soon()
        self.apply_opacity()

    def set_locked(self, on: bool) -> None:
        if on:
            self.lay["locked"] = True
        else:
            self.lay.pop("locked", None)
        self.app.layout_store.save_soon()
        self.apply_locked()


# --------------------------------------------------------------------------
# App side
# --------------------------------------------------------------------------
class LayoutHostMixin:
    NOUN = "widget"
    NOUN_PLURAL = "widgets"
    CONTENT_NOUN = "widgets"  # what a deleted layout never touches, e.g. "notes", "projects or tasks"
    LAYOUT_FIELDS = wl.BASE_FIELDS
    APP_VERSION = "0"
    APP_ID = "app"

    def _init_layouts(self) -> None:
        self.manager_dlg: WidgetManagerDialog | None = None
        self._startup_layout_pending = bool(self.layout_store.settings.get("restore_layout_on_start"))
        self._bind_layout_data()
        self._clamp_timer = QTimer(self)
        self._clamp_timer.setSingleShot(True)
        self._clamp_timer.setInterval(700)
        self._clamp_timer.timeout.connect(self.clamp_all)
        self._watched_screens: set[int] = set()
        gapp = QGuiApplication.instance()
        gapp.screenAdded.connect(self._screens_changed)
        gapp.screenRemoved.connect(self._screens_changed)
        self._watch_screens()

    def _bind_layout_data(self) -> None:
        self.book = wl.LayoutBook(self.layout_store.data)
        self.history = wl.ViewHistory(self.layout_store.data["view_history"])

    # -- monitors ---------------------------------------------------------------
    def _watch_screens(self) -> None:
        for s in QGuiApplication.screens():
            if id(s) not in self._watched_screens:
                self._watched_screens.add(id(s))
                s.availableGeometryChanged.connect(self._screens_changed)

    def _screens_changed(self, *args) -> None:
        """Monitor added/removed or resolution/taskbar changed: re-check positions shortly after."""
        self._watch_screens()
        self._clamp_timer.start()

    def clamp_all(self) -> int:
        """Move widgets that ended up off-screen back onto the nearest screen. Returns how many."""
        screens = screen_rects()
        moved = 0
        for w in self.widgets.values():
            g = w.geometry()
            rect = (g.x(), g.y(), g.width(), g.height())
            new = wl.clamp_rect(rect, screens)
            if new != rect:
                w.resize(new[2], new[3])
                w.move(new[0], new[1])
                w.lay["geom"] = list(new)
                moved += 1
        if moved:
            self.layout_store.save_soon()
        return moved

    # -- views --------------------------------------------------------------------
    def current_view(self) -> dict:
        return wl.capture_view(self.layout_store.items, list(self.widgets), self.LAYOUT_FIELDS)

    def _apply_all_states(self) -> None:
        for w in self.widgets.values():
            w.apply_state()

    def change_view(self, mutate, coalesce: str | None = None, record: bool = True) -> bool:
        """Run mutate() on the item store, apply it to the windows, remember the old view."""
        before = self.current_view()
        mutate()
        self._apply_all_states()
        after = self.current_view()
        changed = before != after
        if changed and record:
            self.history.record(before, coalesce)
        if changed:
            self.layout_store.save_soon()
        self.views_changed()
        return changed

    def views_changed(self) -> None:
        if self.manager_dlg is not None and self.manager_dlg.isVisible():
            self.manager_dlg.refresh()

    def set_visible(self, mapping: dict, coalesce: str | None = None) -> bool:
        def mutate():
            for key, on in mapping.items():
                if key in self.widgets:
                    self.widgets[key].lay["visible"] = bool(on)
        return self.change_view(mutate, coalesce)

    def show_only(self, keys, coalesce: str | None = "manager") -> bool:
        keys = set(keys)
        return self.set_visible({k: k in keys for k in self.widgets}, coalesce)

    def hide_widget_key(self, key: str) -> None:
        self.set_visible({key: False})

    def show_all(self) -> None:
        self.set_visible({k: True for k in self.widgets})
        for w in self.widgets.values():
            w.raise_()

    def hide_all(self) -> None:
        vis = [k for k, w in self.widgets.items() if w.lay.get("visible", True)]
        if vis:
            self.layout_store.settings["toggle_restore"] = vis
        self.set_visible({k: False for k in self.widgets})

    def show_widgets(self) -> None:
        """Tray click / toggle: raise what is visible, or bring back what was visible before
        the last 'hide all' (falls back to all) — never forces every widget on screen."""
        vis = [w for w in self.widgets.values() if w.lay.get("visible", True)]
        if vis:
            for w in vis:
                w.show()
                w.raise_()
            return
        stash = [k for k in self.layout_store.settings.get("toggle_restore") or [] if k in self.widgets]
        if stash:
            self.set_visible({k: True for k in stash})
            for k in stash:
                self.widgets[k].raise_()
        else:
            self.show_all()

    def toggle_all(self) -> None:
        if any(w.isVisible() for w in self.widgets.values()):
            self.hide_all()
        else:
            self.show_widgets()

    def toggle_last_view(self) -> bool:
        target = self.history.swap(self.current_view())
        if target is None:
            self.tray.showMessage(self.APP_TITLE, "There is no previous view yet. It is remembered "
                                  f"as soon as you show, hide or rearrange {self.NOUN_PLURAL}.",
                                  QSystemTrayIcon.Information, 4000)
            return False
        keys = set(self.widgets) | (set(target) & set(self.layout_store.items))
        self.change_view(lambda: wl.apply_view(self.layout_store.items, target, keys,
                                               self.LAYOUT_FIELDS, others="keep"), record=False)
        return True

    # -- named layouts ------------------------------------------------------------
    def active_layout(self) -> str:
        n = self.layout_store.settings.get("active_layout") or ""
        return n if self.book.exists(n) else ""

    def load_layout(self, name: str, record: bool = True) -> bool:
        lay = self.book.get(name)
        if lay is None:
            return False
        view = lay["widgets"]
        keys = set(self.widgets) | (set(view) & set(self.layout_store.items))
        self.change_view(lambda: wl.apply_view(self.layout_store.items, view, keys,
                                               self.LAYOUT_FIELDS, others="hide"), record=record)
        self.layout_store.settings["active_layout"] = self.book._find(name)
        self.layout_store.save_soon()
        self.views_changed()
        return True

    def save_layout(self, name: str, overwrite: bool = False) -> str:
        saved = self.book.save(name, self.current_view(), overwrite=overwrite)
        self.layout_store.settings["active_layout"] = saved
        self.layout_store.save()
        self.views_changed()
        return saved

    def rename_layout(self, old: str, new: str) -> str:
        n = self.book.rename(old, new)
        self.layout_store.save()
        self.apply_hotkeys(notify=False)
        self.views_changed()
        return n

    def delete_layout(self, name: str) -> None:
        self.book.delete(name)
        self.layout_store.save()
        self.apply_hotkeys(notify=False)
        self.views_changed()

    def set_layout_hotkey(self, name: str, text: str) -> str:
        """Returns '' on success or a readable reason (the old hotkey is kept on failure)."""
        text = hk.normalise(text) if text else ""
        if text:
            try:
                hk.parse_hotkey(text)
            except ValueError as exc:
                return str(exc)
        old = (self.book.get(name) or {}).get("hotkey", "")
        self.book.set_hotkey(name, text)
        errors = self.apply_hotkeys(notify=False)
        mine = [e for e in errors if e[0] == f"layout:{self.book._find(name)}"]
        if mine:
            self.book.set_hotkey(name, old)
            self.apply_hotkeys(notify=False)
            return mine[0][2]
        self.layout_store.save()
        return ""

    def restore_startup_layout(self) -> None:
        """Called after widgets exist for the first time."""
        if not self._startup_layout_pending or not self.widgets:
            return
        self._startup_layout_pending = False
        name = self.active_layout()
        if name:
            self.load_layout(name, record=False)

    def hotkey_extra(self) -> dict:
        return self.book.hotkeys()

    def handle_layout_hotkey(self, action: str) -> bool:
        if action == "last_view":
            self.toggle_last_view()
        elif action == "manager":
            self.open_manager()
        elif action.startswith("layout:"):
            self.load_layout(action[7:])
        else:
            return False
        return True

    # -- settings import (export is just a file write) --------------------------
    def export_settings(self, path) -> None:
        self.layout_store.save()
        wl.atomic_write_json(path, wl.export_payload(self.layout_store.data, self.APP_ID, self.APP_VERSION))

    def import_settings(self, path) -> list[str]:
        import json
        with open(path, encoding="utf-8") as fh:
            payload = json.load(fh)
        data = wl.validate_import(payload, self.APP_ID, self.layout_store.ITEMS_KEY)
        backup = wl.backup_file(self.layout_store.path, "before-import")
        self.layout_store.replace(data)
        self._bind_layout_data()
        self.reload_after_import()
        self.layout_store.save()
        return [f"Backup of the previous settings: {backup.name}" if backup else "No previous settings file"]

    # -- UI entry points ----------------------------------------------------------
    def open_manager(self) -> None:
        if self.manager_dlg is None:
            self.manager_dlg = WidgetManagerDialog(self)
        self.manager_dlg.refresh()
        self.manager_dlg.show()
        self.manager_dlg.raise_()
        self.manager_dlg.activateWindow()

    def locate(self, key: str) -> None:
        w = self.widgets.get(key)
        if w is None:
            return
        if not w.lay.get("visible", True):
            self.set_visible({key: True})
        g = w.geometry()
        new = wl.clamp_rect((g.x(), g.y(), g.width(), g.height()), screen_rects())
        if new != (g.x(), g.y(), g.width(), g.height()):
            w.move(new[0], new[1])
        w.show()
        w.raise_()
        w.activateWindow()

    def save_layout_dialog(self, parent=None) -> str | None:
        name, ok = QInputDialog.getText(parent, f"{self.APP_TITLE} – Save layout",
                                        f"Name for this layout (which {self.NOUN_PLURAL} are shown, "
                                        "where, and how big):", text=self.active_layout() or "")
        if not ok:
            return None
        try:
            if self.book.exists(name):
                r = QMessageBox.question(parent, self.APP_TITLE, f"Replace the layout "
                                         f"'{self.book._find(name)}' with the current view?")
                if r != QMessageBox.Yes:
                    return None
                return self.save_layout(name, overwrite=True)
            return self.save_layout(name)
        except wl.LayoutError as exc:
            QMessageBox.warning(parent, self.APP_TITLE, str(exc))
            return None

    def add_layout_menus(self, menu: QMenu, before) -> None:
        """Insert 'Widgets', 'Layouts', 'Toggle last view' and the manager entry into the tray menu."""
        self.tray_widgets_menu = QMenu(f"Show / hide {self.NOUN_PLURAL}", menu)
        self.tray_widgets_menu.aboutToShow.connect(self._fill_widgets_menu)
        self.tray_layouts_menu = QMenu("Layouts", menu)
        self.tray_layouts_menu.aboutToShow.connect(self._fill_layouts_menu)
        menu.insertSeparator(before)
        menu.insertMenu(before, self.tray_widgets_menu)
        menu.insertMenu(before, self.tray_layouts_menu)
        a = QAction("Toggle last view", menu)
        a.triggered.connect(self.toggle_last_view)
        menu.insertAction(before, a)
        b = QAction(f"{self.NOUN_PLURAL.capitalize()} && layouts…", menu)
        b.triggered.connect(self.open_manager)
        menu.insertAction(before, b)
        self.tray_last_view_action, self.tray_manager_action = a, b
        # fill once so the menus are never empty (and testable without showing them)
        self._fill_widgets_menu()
        self._fill_layouts_menu()

    def _fill_widgets_menu(self) -> None:
        m = self.tray_widgets_menu
        m.clear()
        items = sorted(self.manager_items(), key=lambda i: i.title.lower())
        if not items:
            a = m.addAction(f"No {self.NOUN_PLURAL} yet")
            a.setEnabled(False)
        for it in items[:TRAY_LIST_LIMIT]:
            a = m.addAction(it.title)
            a.setCheckable(True)
            a.setChecked(it.visible)
            a.setToolTip(it.subtitle)
            a.triggered.connect(lambda on, k=it.key: self.set_visible({k: on}))
        if len(items) > TRAY_LIST_LIMIT:
            m.addAction(f"{len(items) - TRAY_LIST_LIMIT} more… (open the manager)", self.open_manager)
        m.addSeparator()
        m.addAction(f"Show all {self.NOUN_PLURAL}", self.show_all)
        m.addAction(f"Hide all {self.NOUN_PLURAL}", self.hide_all)

    def _fill_layouts_menu(self) -> None:
        m = self.tray_layouts_menu
        m.clear()
        active = self.active_layout()
        names = self.book.names()
        if not names:
            a = m.addAction("No saved layouts yet")
            a.setEnabled(False)
        for n in names:
            hot = (self.book.get(n) or {}).get("hotkey", "")
            a = m.addAction(n + (f"\t{hot}" if hot else ""))
            a.setCheckable(True)
            a.setChecked(n == active)
            a.triggered.connect(lambda _=False, name=n: self.load_layout(name))
        m.addSeparator()
        m.addAction("Save current layout as…", lambda: self.save_layout_dialog())
        m.addAction("Manage layouts…", self.open_manager)

    def show_about(self) -> None:
        from PySide6 import __version__ as qt_ver
        QMessageBox.about(None, f"About {self.APP_TITLE}", self.about_html().format(
            version=self.APP_VERSION, python=platform.python_version(), qt=qt_ver,
            data=str(self.engine.dir)))


# --------------------------------------------------------------------------
# Manager window
# --------------------------------------------------------------------------
class LayoutHotkeyDialog(QDialog):
    def __init__(self, host, name: str, parent=None):
        super().__init__(parent)
        self.host, self.name = host, name
        self.setWindowTitle(f"{host.APP_TITLE} – Hotkey for '{name}'")
        v = QVBoxLayout(self)
        v.addWidget(QLabel(f"Global hotkey that loads the layout <b>{name}</b>.<br>"
                           "Click the field and press a combination such as Ctrl+Alt+1. "
                           "Use the clear button for none."))
        self.edit = QKeySequenceEdit(QKeySequence.fromString((host.book.get(name) or {}).get("hotkey", ""),
                                                             QKeySequence.PortableText))
        self.edit.setMaximumSequenceLength(1)
        self.edit.setClearButtonEnabled(True)
        v.addWidget(self.edit)
        self.status = QLabel("")
        self.status.setWordWrap(True)
        self.status.setStyleSheet("color:#B3261E;")
        v.addWidget(self.status)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self._ok)
        bb.rejected.connect(self.reject)
        v.addWidget(bb)

    def value(self) -> str:
        return hk.normalise(self.edit.keySequence().toString(QKeySequence.PortableText))

    def _ok(self) -> None:
        err = self.host.set_layout_hotkey(self.name, self.value())
        if err:
            self.status.setText(f"This hotkey cannot be used: {err}")
            return
        self.accept()


class WidgetManagerDialog(QDialog):
    def __init__(self, host):
        super().__init__(None)
        self.host = host
        self.setWindowTitle(f"{host.APP_TITLE} – {host.NOUN_PLURAL.capitalize()} & layouts")
        self.setWindowIcon(host.app_icon())
        self.setMinimumSize(760, 480)
        self._refreshing = False
        v = QVBoxLayout(self)
        intro = QLabel(f"Tick the {host.NOUN_PLURAL} you want on your desktop. Hidden {host.NOUN_PLURAL} "
                       "keep syncing in the background. Save the current arrangement as a layout to "
                       "switch back to it with one click or a hotkey.")
        intro.setWordWrap(True)
        intro.setStyleSheet("color:#555;")
        v.addWidget(intro)
        row = QHBoxLayout()
        v.addLayout(row, 1)

        # ---- widgets ----
        g1 = QGroupBox(f"{host.NOUN_PLURAL.capitalize()} – ticked = shown")
        g1l = QVBoxLayout(g1)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search by title, label, colour…")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self._apply_search)
        g1l.addWidget(self.search)
        self.list = QListWidget()
        self.list.setObjectName("widgetList")
        self.list.itemChanged.connect(self._item_changed)
        self.list.itemDoubleClicked.connect(lambda it: host.locate(it.data(Qt.UserRole)))
        self.list.setToolTip("Double-click to bring a widget to the front")
        g1l.addWidget(self.list, 1)
        btns = QHBoxLayout()
        self.btn_all = QPushButton("Show all")
        self.btn_all.clicked.connect(lambda: self._show_only(set(host.widgets)))
        self.btn_none = QPushButton("Hide all")
        self.btn_none.clicked.connect(lambda: self._show_only(set()))
        self.btn_invert = QPushButton("Invert")
        self.btn_invert.clicked.connect(self._invert)
        self.btn_only = QToolButton()
        self.btn_only.setText("Show only ▾")
        self.btn_only.setPopupMode(QToolButton.InstantPopup)
        self.only_menu = QMenu(self.btn_only)
        self.only_menu.aboutToShow.connect(self._fill_only_menu)
        self.btn_only.setMenu(self.only_menu)
        for b in (self.btn_all, self.btn_none, self.btn_invert):
            btns.addWidget(b)
        btns.addWidget(self.btn_only)
        btns.addStretch(1)
        g1l.addLayout(btns)
        self.count_lbl = QLabel("")
        self.count_lbl.setStyleSheet("color:#666; font-size:8pt;")
        g1l.addWidget(self.count_lbl)
        row.addWidget(g1, 3)

        # ---- layouts ----
        g2 = QGroupBox("Saved layouts")
        g2l = QVBoxLayout(g2)
        self.layouts = QListWidget()
        self.layouts.setObjectName("layoutList")
        self.layouts.itemDoubleClicked.connect(lambda _it: self._load())
        self.layouts.currentRowChanged.connect(lambda _r: self._update_buttons())
        g2l.addWidget(self.layouts, 1)
        grid = QGridLayout()
        self.b_load = QPushButton("Load")
        self.b_load.clicked.connect(self._load)
        self.b_new = QPushButton("Save as new…")
        self.b_new.clicked.connect(self._save_new)
        self.b_update = QPushButton("Update")
        self.b_update.setToolTip("Replace the selected layout with what is on screen now")
        self.b_update.clicked.connect(self._update)
        self.b_rename = QPushButton("Rename…")
        self.b_rename.clicked.connect(self._rename)
        self.b_hotkey = QPushButton("Hotkey…")
        self.b_hotkey.clicked.connect(self._hotkey)
        self.b_delete = QPushButton("Delete…")
        self.b_delete.clicked.connect(self._delete)
        for i, b in enumerate((self.b_load, self.b_new, self.b_update, self.b_rename, self.b_hotkey,
                               self.b_delete)):
            grid.addWidget(b, i // 2, i % 2)
        g2l.addLayout(grid)
        self.cb_restore = QCheckBox("Load the active layout when the app starts")
        self.cb_restore.setToolTip("Off: the app reopens exactly as you left it.\n"
                                   "On: it always starts with the active saved layout.")
        self.cb_restore.toggled.connect(self._restore_toggled)
        g2l.addWidget(self.cb_restore)
        note = QLabel(f"Deleting a layout never deletes your {host.CONTENT_NOUN}.")
        note.setWordWrap(True)
        note.setStyleSheet("color:#666; font-size:8pt;")
        g2l.addWidget(note)
        row.addWidget(g2, 2)

        bottom = QHBoxLayout()
        self.b_last = QPushButton("Toggle last view")
        hot = (host.layout_store.settings.get("hotkeys") or {}).get("last_view", "")
        self.b_last.setToolTip("Switch between the current and the previous set of shown widgets"
                               + (f" ({hot})" if hot else ""))
        self.b_last.clicked.connect(host.toggle_last_view)
        bottom.addWidget(self.b_last)
        bottom.addStretch(1)
        close = QPushButton("Close")
        close.clicked.connect(self.close)
        bottom.addWidget(close)
        v.addLayout(bottom)

    # -- data -> UI ---------------------------------------------------------------
    def refresh(self) -> None:
        self._refreshing = True
        try:
            items = sorted(self.host.manager_items(), key=lambda i: i.title.lower())
            by_key = {self.list.item(i).data(Qt.UserRole): self.list.item(i) for i in range(self.list.count())}
            if [i.key for i in items] != [self.list.item(i).data(Qt.UserRole) for i in range(self.list.count())]:
                self.list.clear()
                by_key = {}
                for it in items:
                    li = QListWidgetItem()
                    li.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable | Qt.ItemIsUserCheckable)
                    li.setData(Qt.UserRole, it.key)
                    self.list.addItem(li)
                    by_key[it.key] = li
            for it in items:
                li = by_key[it.key]
                text = it.title + (f"   ·   {it.subtitle}" if it.subtitle else "")
                if li.text() != text:
                    li.setText(text)
                li.setData(Qt.UserRole + 1, f"{it.title} {it.subtitle} {it.search}".lower())
                li.setToolTip(it.subtitle)
                state = Qt.Checked if it.visible else Qt.Unchecked
                if li.checkState() != state:
                    li.setCheckState(state)
            shown = sum(1 for i in items if i.visible)
            self.count_lbl.setText(f"{shown} of {len(items)} {self.host.NOUN_PLURAL} shown")
            self._apply_search()
            # layouts
            active = self.host.active_layout()
            cur = self.layouts.currentItem().data(Qt.UserRole) if self.layouts.currentItem() else active
            self.layouts.clear()
            for n in self.host.book.names():
                hot = (self.host.book.get(n) or {}).get("hotkey", "")
                li = QListWidgetItem(n + ("   (active)" if n == active else "") + (f"   [{hot}]" if hot else ""))
                li.setData(Qt.UserRole, n)
                if n == active:
                    f = li.font()
                    f.setBold(True)
                    li.setFont(f)
                self.layouts.addItem(li)
                if n == cur:
                    self.layouts.setCurrentItem(li)
            self.cb_restore.setChecked(bool(self.host.layout_store.settings.get("restore_layout_on_start")))
            self.b_last.setEnabled(self.host.history.has_previous())
            self._update_buttons()
        finally:
            self._refreshing = False

    def _apply_search(self) -> None:
        q = self.search.text().strip().lower()
        for i in range(self.list.count()):
            li = self.list.item(i)
            li.setHidden(bool(q) and q not in (li.data(Qt.UserRole + 1) or ""))

    def _update_buttons(self) -> None:
        has = self.layouts.currentItem() is not None
        for b in (self.b_load, self.b_update, self.b_rename, self.b_hotkey, self.b_delete):
            b.setEnabled(has)

    def selected_layout(self) -> str | None:
        it = self.layouts.currentItem()
        return it.data(Qt.UserRole) if it else None

    # -- UI -> host ---------------------------------------------------------------
    def _item_changed(self, li: QListWidgetItem) -> None:
        if self._refreshing:
            return
        self.host.set_visible({li.data(Qt.UserRole): li.checkState() == Qt.Checked}, coalesce="manager")

    def _show_only(self, keys: set) -> None:
        self.host.show_only(keys, coalesce=None)

    def _invert(self) -> None:
        vis = {k for k, w in self.host.widgets.items() if w.lay.get("visible", True)}
        self.host.show_only(set(self.host.widgets) - vis, coalesce=None)

    def listed_keys(self) -> set:
        return {self.list.item(i).data(Qt.UserRole) for i in range(self.list.count())
                if not self.list.item(i).isHidden()}

    def _fill_only_menu(self) -> None:
        m = self.only_menu
        m.clear()
        q = self.search.text().strip()
        a = m.addAction(f"The search results ({len(self.listed_keys())})" if q else "The search results")
        a.setEnabled(bool(q))
        a.triggered.connect(lambda: self._show_only(self.listed_keys()))
        m.addSeparator()
        for label, target in self.host.show_only_groups():
            if isinstance(target, list):
                sub = m.addMenu(label)
                if not target:
                    x = sub.addAction("(none)")
                    x.setEnabled(False)
                for sl, keys in target:
                    sub.addAction(f"{sl} ({len(keys)})", lambda k=set(keys): self._show_only(k))
            else:
                act = m.addAction(f"{label} ({len(target)})", lambda k=set(target): self._show_only(k))
                act.setEnabled(bool(target))

    def _load(self) -> None:
        n = self.selected_layout()
        if n:
            self.host.load_layout(n)

    def _save_new(self) -> None:
        name, ok = QInputDialog.getText(self, self.host.APP_TITLE, "Name for the new layout:")
        if not ok:
            return
        try:
            self.host.save_layout(name)
        except wl.LayoutError as exc:
            QMessageBox.warning(self, self.host.APP_TITLE, str(exc))

    def _update(self) -> None:
        n = self.selected_layout()
        if n and QMessageBox.question(self, self.host.APP_TITLE,
                                      f"Replace the layout '{n}' with what is on screen now?") == QMessageBox.Yes:
            self.host.save_layout(n, overwrite=True)

    def _rename(self) -> None:
        n = self.selected_layout()
        if not n:
            return
        new, ok = QInputDialog.getText(self, self.host.APP_TITLE, f"New name for '{n}':", text=n)
        if ok:
            try:
                self.host.rename_layout(n, new)
            except wl.LayoutError as exc:
                QMessageBox.warning(self, self.host.APP_TITLE, str(exc))

    def _hotkey(self) -> None:
        n = self.selected_layout()
        if n:
            LayoutHotkeyDialog(self.host, n, self).exec()
            self.refresh()

    def _delete(self) -> None:
        n = self.selected_layout()
        if n and QMessageBox.question(
                self, self.host.APP_TITLE,
                f"Delete the layout '{n}'?\n\nOnly the saved layout is removed. Your "
                f"{self.host.CONTENT_NOUN} are not touched.") == QMessageBox.Yes:
            self.host.delete_layout(n)

    def _restore_toggled(self, on: bool) -> None:
        if self._refreshing:
            return
        self.host.layout_store.settings["restore_layout_on_start"] = bool(on)
        self.host.layout_store.save_soon()
