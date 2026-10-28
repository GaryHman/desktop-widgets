#!/usr/bin/env python3
"""Keep-Todoist Bridge - keeps one Google Keep checklist in two-way sync with one Todoist project.

Run with pythonw (no console):  pythonw keep_todoist_bridge.py
Options:  --data-dir PATH   use another data folder (default %APPDATA%\\KeepTodoistBridge)

Google Keep access uses the UNOFFICIAL gkeepapi library. It can break if Google changes things.
"""
from __future__ import annotations

import sys

sys.dont_write_bytecode = True

if sys.version_info < (3, 11):
    sys.stderr.write("Keep-Todoist Bridge needs Python 3.11 or newer.\n")
    sys.exit(1)

import argparse
import logging
import os
import subprocess
import threading
from logging.handlers import RotatingFileHandler
from pathlib import Path

_APP_NAME = "Keep-Todoist Bridge"
APP_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(APP_DIR))

# Packaged build (PyInstaller .exe) vs. running from source with pythonw.
FROZEN = bool(getattr(sys, "frozen", False))
# Own taskbar identity, so Windows shows this app's icon and name instead of Python's.
APP_USER_MODEL_ID = "AngelsInc.KeepTodoistBridge"


def set_app_user_model_id() -> None:
    if os.name != "nt":
        return
    try:
        import ctypes

        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_USER_MODEL_ID)
    except Exception:  # noqa: BLE001  cosmetic only, never block startup
        pass


def launch_target() -> tuple[str, str, str, str]:
    """(target, arguments, working folder, icon) for a shortcut that starts this app."""
    if FROZEN:
        exe = Path(sys.executable)
        return str(exe), "", str(exe.parent), str(exe)
    return pythonw_executable(), f'"{APP_DIR / "keep_todoist_bridge.py"}"', str(APP_DIR), str(APP_DIR / "icon.ico")


def _startup_failure(exc: BaseException) -> None:
    """A required package is missing (e.g. an incomplete .venv): log it and show a message box."""
    import ctypes
    import datetime

    msg = (f"{_APP_NAME} could not start because a required Python package is missing or broken:\n\n"
           f"{exc}\n\nPython used: {sys.executable}\n\nRun install.bat in\n{APP_DIR}\n"
           "or install the packages with:  py -m pip install -r requirements.txt")
    if getattr(sys, "frozen", False):
        msg = f"{_APP_NAME} could not start:\n\n{exc}\n\nPlease reinstall {_APP_NAME}."
    try:
        d = Path(os.environ.get("KTB_DATA_DIR") or Path(os.environ.get("APPDATA", str(Path.home()))) /
                 "KeepTodoistBridge")
        d.mkdir(parents=True, exist_ok=True)
        with open(d / "bridge.log", "a", encoding="utf-8") as f:
            f.write(f"{datetime.datetime.now():%Y-%m-%d %H:%M:%S} ERROR Startup failed: {exc!r} "
                    f"(python: {sys.executable})\n")
    except OSError:
        pass
    if sys.stderr is not None:
        sys.stderr.write(msg + "\n")
    if os.name == "nt":
        try:
            ctypes.windll.user32.MessageBoxW(None, msg, _APP_NAME, 0x10)
        except Exception:  # noqa: BLE001
            pass
    sys.exit(1)


try:
    from PySide6.QtCore import QLockFile, QObject, Qt, QTimer, QUrl, Signal
    from PySide6.QtGui import QColor, QDesktopServices, QFont, QIcon, QPainter, QPixmap
    from PySide6.QtWidgets import (QApplication, QButtonGroup, QCheckBox, QComboBox, QDialog,
                                   QDialogButtonBox, QFormLayout, QGroupBox, QHBoxLayout, QInputDialog,
                                   QLabel, QLineEdit, QMenu, QMessageBox, QPushButton, QRadioButton,
                                   QSpinBox, QSystemTrayIcon, QVBoxLayout)

    import bridge_core as bc  # noqa: E402
except ImportError as _exc:  # pragma: no cover
    _startup_failure(_exc)

APP_TITLE = bc.APP_TITLE
log = logging.getLogger("keeptodoistbridge")
EMBEDDED_SETUP_URL = "https://accounts.google.com/EmbeddedSetup"
TOKEN_URL = "https://app.todoist.com/app/settings/integrations/developer"


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def setup_logging(directory: Path) -> None:
    handler = RotatingFileHandler(directory / "bridge.log", maxBytes=512_000, backupCount=2, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    log.addHandler(handler)
    log.setLevel(logging.INFO)
    if sys.stderr is not None and not getattr(sys, "frozen", False):
        log.addHandler(logging.StreamHandler())


def draw_icon_pixmap(size: int = 64, error: bool = False) -> QPixmap:
    """Yellow Keep-style card and red Todoist-style card joined by two arrows."""
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    s = size / 64.0
    p.setPen(Qt.NoPen)
    p.setBrush(QColor("#FBBC04"))
    p.drawRoundedRect(int(3 * s), int(8 * s), int(30 * s), int(40 * s), 5 * s, 5 * s)
    p.setBrush(QColor("#E44332"))
    p.drawRoundedRect(int(31 * s), int(16 * s), int(30 * s), int(40 * s), 5 * s, 5 * s)
    p.setBrush(QColor("#FFFFFF"))
    for i in range(3):
        p.drawRoundedRect(int(8 * s), int((16 + i * 9) * s), int(18 * s), int(4 * s), 2 * s, 2 * s)
        p.drawRoundedRect(int(37 * s), int((25 + i * 9) * s), int(18 * s), int(4 * s), 2 * s, 2 * s)
    if error:
        p.setBrush(QColor("#B3261E"))
        p.drawEllipse(int(38 * s), int(0), int(26 * s), int(26 * s))
        p.setBrush(QColor("#FFFFFF"))
        p.drawRect(int(49 * s), int(5 * s), int(4 * s), int(10 * s))
        p.drawRect(int(49 * s), int(18 * s), int(4 * s), int(4 * s))
    p.end()
    return pm


def app_icon(error: bool = False) -> QIcon:
    ico = APP_DIR / "icon.ico"
    if not error and ico.exists():
        icon = QIcon(str(ico))
        if not icon.isNull():
            return icon
    return QIcon(draw_icon_pixmap(64, error))


def startup_shortcut_path() -> Path | None:
    if os.name != "nt":
        return None
    return (Path(os.environ.get("APPDATA", "")) / "Microsoft" / "Windows" / "Start Menu" /
            "Programs" / "Startup" / "Keep-Todoist Bridge.lnk")


def pythonw_executable() -> str:
    exe = Path(sys.executable)
    w = exe.with_name("pythonw.exe")
    return str(w if w.exists() else exe)


def create_shortcut(lnk: Path, target: str, args: str, workdir: str, icon: str) -> None:
    env = dict(os.environ, KTB_LNK=str(lnk), KTB_TARGET=target, KTB_ARGS=args, KTB_WD=workdir, KTB_ICON=icon)
    ps = ("$s=(New-Object -ComObject WScript.Shell).CreateShortcut($env:KTB_LNK);"
          "$s.TargetPath=$env:KTB_TARGET;$s.Arguments=$env:KTB_ARGS;"
          "$s.WorkingDirectory=$env:KTB_WD;$s.IconLocation=$env:KTB_ICON;"
          "$s.Description='Keep-Todoist Bridge';$s.Save()")
    subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps], env=env, check=True,
                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), timeout=30)


def autostart_enabled() -> bool:
    lnk = startup_shortcut_path()
    return bool(lnk and lnk.exists())


def set_autostart(on: bool) -> None:
    lnk = startup_shortcut_path()
    if lnk is None:
        return
    if on:
        create_shortcut(lnk, *launch_target())
        log.info("Start with Windows enabled")
    elif lnk.exists():
        lnk.unlink()
        log.info("Start with Windows disabled")


class Bus(QObject):
    event = Signal(str, object)


# --------------------------------------------------------------------------
# Connect dialogs (copied from KeepNotes Widgets / Todoist Widgets)
# --------------------------------------------------------------------------
KEEP_SETUP_STEPS = f"""
<p><b>Google Keep is accessed through an unofficial API.</b> It can stop working if Google
changes things. Your master token gives access to your Google account; it is stored only in
<b>Windows Credential Manager</b> (the same entry KeepNotes Widgets uses), never in a file.</p>
<p><b>Option A: oauth_token cookie (recommended)</b></p>
<ol>
<li>Click <b>Open Google sign-in page</b>, or open <a href="{EMBEDDED_SETUP_URL}">{EMBEDDED_SETUP_URL}</a> in Chrome or Edge.</li>
<li>Sign in with your Google account and click <b>I agree</b>. The page may keep loading forever; that is normal.</li>
<li>Press <b>F12</b>, open <b>Application</b> &rsaquo; <b>Cookies</b> &rsaquo; <i>https://accounts.google.com</i>
(Firefox: <b>Storage</b> &rsaquo; Cookies).</li>
<li>Find the cookie <code>oauth_token</code> and copy its value (it starts with <code>oauth2_4/</code>).</li>
<li>Paste it below with your email and click <b>Sign in</b> within a few minutes (the value is single-use).</li>
</ol>
<p><b>Option B:</b> paste a master token you already have (starts with <code>aas_et/</code>).</p>
"""

TODOIST_SETUP_STEPS = f"""
<p>Todoist is accessed through the <b>official Todoist API</b> with your personal API token.
The token is stored only in <b>Windows Credential Manager</b> (the same entry Todoist Widgets uses),
never in a file.</p>
<ol>
<li>Click <b>Open Todoist token page</b>, or open
<a href="{TOKEN_URL}">Settings &rsaquo; Integrations &rsaquo; Developer</a> in Todoist.</li>
<li>Under <b>API token</b>, click <b>Copy API token</b>.</li>
<li>Paste it below and click <b>Connect</b>.</li>
</ol>
<p style="color:#666">Anyone with this token can read and change your Todoist, so treat it like a
password. If you ever need to, you can issue a new token on the same page.</p>
"""


class KeepSetupDialog(QDialog):
    finished_signin = Signal(bool, str)

    def __init__(self, app: "BridgeApp", parent=None):
        super().__init__(parent)
        self.app = app
        self.setWindowTitle(f"{APP_TITLE} – Connect Google Keep")
        self.setWindowIcon(app_icon())
        self.setMinimumWidth(560)
        v = QVBoxLayout(self)
        info = QLabel(KEEP_SETUP_STEPS)
        info.setWordWrap(True)
        info.setOpenExternalLinks(True)
        info.setTextFormat(Qt.RichText)
        v.addWidget(info)
        open_btn = QPushButton("Open Google sign-in page")
        open_btn.clicked.connect(lambda: QDesktopServices.openUrl(QUrl(EMBEDDED_SETUP_URL)))
        v.addWidget(open_btn, 0, Qt.AlignLeft)
        form = QFormLayout()
        creds = None
        try:
            creds = app.engine.keep_creds.get()
        except Exception:  # noqa: BLE001
            pass
        self.email = QLineEdit(creds[0] if creds else "")
        self.email.setPlaceholderText("you@gmail.com")
        form.addRow("Google email:", self.email)
        kind = QHBoxLayout()
        self.rb_cookie = QRadioButton("oauth_token cookie")
        self.rb_master = QRadioButton("Master token")
        self.rb_cookie.setChecked(True)
        grp = QButtonGroup(self)
        grp.addButton(self.rb_cookie)
        grp.addButton(self.rb_master)
        kind.addWidget(self.rb_cookie)
        kind.addWidget(self.rb_master)
        kind.addStretch(1)
        form.addRow("I am pasting:", kind)
        self.token = QLineEdit()
        self.token.setEchoMode(QLineEdit.Password)
        self.token.setPlaceholderText("oauth2_4/…")
        self.rb_master.toggled.connect(lambda m: self.token.setPlaceholderText("aas_et/…" if m else "oauth2_4/…"))
        form.addRow("Value:", self.token)
        v.addLayout(form)
        self.status = QLabel("")
        self.status.setWordWrap(True)
        v.addWidget(self.status)
        bb = QDialogButtonBox()
        self.ok_btn = bb.addButton("Sign in", QDialogButtonBox.AcceptRole)
        bb.addButton(QDialogButtonBox.Cancel)
        self.ok_btn.clicked.connect(self.sign_in)
        bb.rejected.connect(self.reject)
        v.addWidget(bb)
        self.finished_signin.connect(self._done)

    def sign_in(self) -> None:
        email = self.email.text().strip()
        value = self.token.text().strip()
        if "@" not in email or not value:
            self.status.setText("Enter your Google email and paste the value first.")
            return
        if self.rb_cookie.isChecked() and value.startswith("aas_et/"):
            self.rb_master.setChecked(True)
        master_mode = self.rb_master.isChecked()
        if master_mode and not bc.looks_like_master_token(value):
            self.status.setText("That does not look like a master token (it should start with aas_et/).")
            return
        self.ok_btn.setEnabled(False)
        self.status.setText("Signing in…")

        def work():
            try:
                master = value if master_mode else bc.exchange_oauth_token(email, value, self.app.engine.android_id())
                self.app.engine.keep_sign_in(email, master)
                self.finished_signin.emit(True, "")
            except Exception as exc:  # noqa: BLE001
                log.warning("Google sign-in failed: %s", type(exc).__name__)
                self.finished_signin.emit(False, bc.friendly_error(exc, "Google"))

        threading.Thread(target=work, daemon=True).start()

    def _done(self, ok: bool, msg: str) -> None:
        self.ok_btn.setEnabled(True)
        if ok:
            self.token.clear()
            self.accept()
        else:
            self.status.setText(f"<span style='color:#B3261E'>Sign-in failed: {msg}</span>")


class TodoistSetupDialog(QDialog):
    finished_signin = Signal(bool, str)

    def __init__(self, app: "BridgeApp", parent=None):
        super().__init__(parent)
        self.app = app
        self.setWindowTitle(f"{APP_TITLE} – Connect Todoist")
        self.setWindowIcon(app_icon())
        self.setMinimumWidth(520)
        v = QVBoxLayout(self)
        info = QLabel(TODOIST_SETUP_STEPS)
        info.setWordWrap(True)
        info.setOpenExternalLinks(True)
        info.setTextFormat(Qt.RichText)
        v.addWidget(info)
        open_btn = QPushButton("Open Todoist token page")
        open_btn.clicked.connect(lambda: QDesktopServices.openUrl(QUrl(TOKEN_URL)))
        v.addWidget(open_btn, 0, Qt.AlignLeft)
        form = QFormLayout()
        self.token = QLineEdit()
        self.token.setEchoMode(QLineEdit.Password)
        self.token.setPlaceholderText("Paste your 40-character API token")
        form.addRow("API token:", self.token)
        v.addLayout(form)
        self.status = QLabel("")
        self.status.setWordWrap(True)
        v.addWidget(self.status)
        bb = QDialogButtonBox()
        self.ok_btn = bb.addButton("Connect", QDialogButtonBox.AcceptRole)
        bb.addButton(QDialogButtonBox.Cancel)
        self.ok_btn.clicked.connect(self.connect_token)
        bb.rejected.connect(self.reject)
        v.addWidget(bb)
        self.finished_signin.connect(self._done)

    def connect_token(self) -> None:
        value = bc.clean_token(self.token.text())
        if not bc.looks_like_token(value):
            self.status.setText("That does not look like a Todoist API token (40 letters and digits).")
            return
        self.ok_btn.setEnabled(False)
        self.status.setText("Checking the token with Todoist…")

        def work():
            try:
                user = self.app.engine.todoist_set_token(value)
                self.finished_signin.emit(True, user.get("full_name") or user.get("email") or "")
            except Exception as exc:  # noqa: BLE001
                log.warning("Todoist token check failed: %s", type(exc).__name__)
                self.finished_signin.emit(False, bc.friendly_error(exc, "Todoist"))

        threading.Thread(target=work, daemon=True).start()

    def _done(self, ok: bool, msg: str) -> None:
        self.ok_btn.setEnabled(True)
        if ok:
            self.token.clear()
            self.accept()
        else:
            self.status.setText(f"<span style='color:#B3261E'>Could not connect: {msg}</span>")


# --------------------------------------------------------------------------
# Settings dialog
# --------------------------------------------------------------------------
class SettingsDialog(QDialog):
    def __init__(self, app: "BridgeApp"):
        super().__init__()
        self.app = app
        e = app.engine
        self.setWindowTitle(f"{APP_TITLE} – Settings")
        self.setWindowIcon(app_icon())
        self.setMinimumWidth(540)
        v = QVBoxLayout(self)

        acc = QGroupBox("Accounts")
        af = QFormLayout(acc)
        self.keep_state = QLabel()
        kb = QPushButton("Connect Google Keep…")
        kb.clicked.connect(self.connect_keep)
        row = QHBoxLayout()
        row.addWidget(self.keep_state, 1)
        row.addWidget(kb)
        af.addRow("Google Keep:", row)
        self.td_state = QLabel()
        tb = QPushButton("Connect Todoist…")
        tb.clicked.connect(self.connect_todoist)
        row = QHBoxLayout()
        row.addWidget(self.td_state, 1)
        row.addWidget(tb)
        af.addRow("Todoist:", row)
        v.addWidget(acc)

        pair = QGroupBox("What to keep in sync")
        pf = QFormLayout(pair)
        self.keep_combo = QComboBox()
        self.keep_combo.setMinimumWidth(260)
        new_list = QPushButton("New list…")
        new_list.clicked.connect(self.new_keep_list)
        row = QHBoxLayout()
        row.addWidget(self.keep_combo, 1)
        row.addWidget(new_list)
        pf.addRow("Keep checklist:", row)
        self.project_combo = QComboBox()
        self.project_combo.setMinimumWidth(260)
        new_proj = QPushButton("New project…")
        new_proj.clicked.connect(self.new_project)
        row = QHBoxLayout()
        row.addWidget(self.project_combo, 1)
        row.addWidget(new_proj)
        pf.addRow("Todoist project:", row)
        self.section_combo = QComboBox()
        self.section_combo.setMinimumWidth(260)
        self.section_combo.setToolTip("Synced tasks go into this section of the project. "
                                      "(No section) syncs the whole project, like before.")
        new_sec = QPushButton("New section…")
        new_sec.clicked.connect(self.new_section)
        row = QHBoxLayout()
        row.addWidget(self.section_combo, 1)
        row.addWidget(new_sec)
        pf.addRow("Section:", row)
        refresh = QPushButton("Refresh lists")
        refresh.clicked.connect(self.refresh)
        pf.addRow("", refresh)
        v.addWidget(pair)

        opt = QGroupBox("Options")
        of = QFormLayout(opt)
        self.interval = QSpinBox()
        self.interval.setRange(15, 3600)
        self.interval.setSuffix(" s")
        self.interval.setValue(int(e.config.get("interval") or bc.DEFAULT_INTERVAL))
        of.addRow("Sync every:", self.interval)
        self.autostart = QCheckBox("Start with Windows")
        self.autostart.setChecked(autostart_enabled())
        self.autostart.setEnabled(startup_shortcut_path() is not None)
        of.addRow("", self.autostart)
        v.addWidget(opt)

        self.status = QLabel("")
        self.status.setWordWrap(True)
        v.addWidget(self.status)

        bottom = QHBoxLayout()
        sync_btn = QPushButton("Sync now")
        sync_btn.clicked.connect(self.sync_now)
        relink = QPushButton("Start over…")
        relink.setToolTip("Forget the item links and match both sides by text again. Deletes nothing.")
        relink.clicked.connect(self.relink)
        bottom.addWidget(sync_btn)
        bottom.addWidget(relink)
        bottom.addStretch(1)
        bb = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        bb.accepted.connect(self.save)
        bb.rejected.connect(self.reject)
        bottom.addWidget(bb)
        v.addLayout(bottom)

        app.bus.event.connect(self._on_event)
        self._workspaces: dict = {}
        self._pending_select: dict = {}
        self._sections: dict = e.sections_by_project()
        self._sections_for = None  # project whose sections the section combo shows
        self.project_combo.currentIndexChanged.connect(lambda _i: self._fill_sections())
        self.update_accounts()
        self._fill(None, e.project_choices(), e.state["todoist"].get("workspaces", {}))
        self._fill_sections()
        self.refresh()

    def done(self, r):  # disconnect from the bus when closing
        try:
            self.app.bus.event.disconnect(self._on_event)
        except (RuntimeError, TypeError):
            pass
        super().done(r)

    def update_accounts(self) -> None:
        c = self.app.engine.connected()
        self.keep_state.setText("Connected" if c["keep"] else "<span style='color:#B3261E'>Not connected</span>")
        self.td_state.setText("Connected" if c["todoist"] else "<span style='color:#B3261E'>Not connected</span>")

    def connect_keep(self) -> None:
        if KeepSetupDialog(self.app, self).exec():
            self.update_accounts()
            self.refresh()

    def connect_todoist(self) -> None:
        if TodoistSetupDialog(self.app, self).exec():
            self.update_accounts()
            self.refresh()

    def refresh(self) -> None:
        self.status.setText("Loading your Keep lists, Todoist projects and sections…")
        self.app.worker.run_job("sources", self.app.engine.refresh_sources)

    def _fill(self, keep_lists, projects, workspaces) -> None:
        cfg = self.app.engine.config
        if keep_lists is not None:
            cur = self.keep_combo.currentData() or cfg.get("keep_note_id")
            self.keep_combo.clear()
            for n in keep_lists:
                self.keep_combo.addItem(n["title"] + ("  (archived)" if n.get("archived") else ""), n["id"])
            if cur and self.keep_combo.findData(cur) < 0 and cfg.get("keep_title"):
                self.keep_combo.addItem(cfg["keep_title"] + "  (not found)", cur)
            i = self.keep_combo.findData(cur)
            if i >= 0:
                self.keep_combo.setCurrentIndex(i)
        elif cfg.get("keep_note_id") and self.keep_combo.count() == 0:
            self.keep_combo.addItem(cfg.get("keep_title") or "(saved list)", cfg["keep_note_id"])
        if projects is not None:
            self._workspaces = dict(workspaces or {})
            cur = self.project_combo.currentData() or cfg.get("todoist_project_id")
            self.project_combo.clear()
            for p in projects:
                self.project_combo.addItem(f'{p["group"]} › {p["name"]}', p["id"])
            i = self.project_combo.findData(cur)
            if i >= 0:
                self.project_combo.setCurrentIndex(i)

    def _fill_sections(self) -> None:
        cfg = self.app.engine.config
        pid = self.project_combo.currentData()
        if self._sections_for == pid:
            cur = self.section_combo.currentData()
        else:
            cur = cfg.get("todoist_section_id") if pid == cfg.get("todoist_project_id") else None
        self.section_combo.blockSignals(True)
        self.section_combo.clear()
        self.section_combo.addItem("(No section)", "")
        for sec in self._sections.get(pid, []):
            self.section_combo.addItem(sec["name"] or "(unnamed section)", sec["id"])
        if cur and self.section_combo.findData(cur) < 0 and cur == cfg.get("todoist_section_id"):
            self.section_combo.addItem((cfg.get("section_name") or "(saved section)") + "  (not found)", cur)
        i = self.section_combo.findData(cur or "")
        self.section_combo.setCurrentIndex(max(0, i))
        self.section_combo.blockSignals(False)
        self._sections_for = pid

    def _on_event(self, kind: str, payload) -> None:
        if kind == "job" and payload.get("name") == "sources":
            if not payload["ok"]:
                self.status.setText(f"<span style='color:#B3261E'>{payload['error']}</span>")
                return
            r = payload["result"]
            keep_pid, keep_sec = self._sections_for, self.section_combo.currentData()
            if r["todoist"] is not None:
                self._sections = r.get("sections") or {}
            self._fill(r["keep"], r["todoist"], r["workspaces"])
            self._select_created()
            self._fill_sections()
            want = self._pending_select.pop("section", None)
            if not want and self.project_combo.currentData() == keep_pid:
                want = keep_sec  # keep an unsaved choice across "Refresh lists"
            if want and self.section_combo.findData(want) >= 0:
                self.section_combo.setCurrentIndex(self.section_combo.findData(want))
            if r["errors"]:
                self.status.setText("<span style='color:#B3261E'>" + "<br>".join(r["errors"]) + "</span>")
            else:
                n_sec = len(self._sections.get(self.project_combo.currentData(), []))
                self.status.setText(f"{len(r['keep'] or [])} Keep checklists, {len(r['todoist'] or [])} "
                                    f"Todoist projects (personal and team), {n_sec} section(s) in this project.")
        elif kind == "job" and payload.get("name") in ("new_list", "new_project", "new_section"):
            if not payload["ok"]:
                self.status.setText(f"<span style='color:#B3261E'>{payload['error']}</span>")
                return
            key = {"new_list": "note", "new_project": "project", "new_section": "section"}[payload["name"]]
            self._pending_select[key] = payload["result"]
            self.refresh()
        elif kind == "synced":
            self.status.setText(self.app.status_text())

    def _select_created(self) -> None:
        for key, combo in (("note", self.keep_combo), ("project", self.project_combo)):
            want = self._pending_select.get(key)
            if want and combo.findData(want) >= 0:
                combo.setCurrentIndex(combo.findData(want))
                self._pending_select.pop(key, None)

    def new_keep_list(self) -> None:
        title, ok = QInputDialog.getText(self, APP_TITLE, "Name of the new Google Keep checklist:")
        if ok and title.strip():
            self.status.setText("Creating the Keep list…")
            self.app.worker.run_job("new_list", lambda: self.app.engine.create_keep_list(title))

    def new_project(self) -> None:
        name, ok = QInputDialog.getText(self, APP_TITLE, "Name of the new Todoist project:")
        if not ok or not name.strip():
            return
        options = ["Personal"] + [f"Team: {n}" for n in self._workspaces.values()]
        ws_ids = [None] + list(self._workspaces.keys())
        where = 0
        if len(options) > 1:
            choice, ok = QInputDialog.getItem(self, APP_TITLE, "Create it in:", options, 0, False)
            if not ok:
                return
            where = options.index(choice)
        wid = ws_ids[where]
        self.status.setText("Creating the Todoist project…")
        self.app.worker.run_job("new_project", lambda: self.app.engine.create_todoist_project(name, wid))

    def new_section(self) -> None:
        pid = self.project_combo.currentData()
        if not pid:
            self.status.setText("<span style='color:#B3261E'>Choose a Todoist project first.</span>")
            return
        name, ok = QInputDialog.getText(self, APP_TITLE,
                                        f"Name of the new section in {self.project_combo.currentText()}:")
        if not ok:
            return
        if not name.strip():
            self.status.setText("<span style='color:#B3261E'>A section needs a name.</span>")
            return
        self.status.setText("Creating the Todoist section…")
        self.app.worker.run_job("new_section", lambda: self.app.engine.create_todoist_section(name, pid))

    def relink(self) -> None:
        if QMessageBox.question(self, APP_TITLE,
                                "Forget which Keep item belongs to which Todoist task and match them by text "
                                "again on the next sync?\n\nNothing is deleted; items that do not match are "
                                "copied to the other side.") == QMessageBox.Yes:
            self.app.worker.run_job("relink", self.app.engine.relink)
            self.app.worker.sync_now()

    def sync_now(self) -> None:
        if self._apply():
            self.status.setText("Syncing…")
            self.app.worker.sync_now()

    def _apply(self) -> bool:
        nid, pid = self.keep_combo.currentData(), self.project_combo.currentData()
        if not nid or not pid:
            self.status.setText("<span style='color:#B3261E'>Choose a Keep checklist and a Todoist project.</span>")
            return False
        e = self.app.engine
        changed = (nid, pid) != (e.config.get("keep_note_id"), e.config.get("todoist_project_id"))
        if changed and e.state.get("pairs"):
            if QMessageBox.question(self, APP_TITLE,
                                    "You chose a different list or project. The bridge will match the two by "
                                    "text and copy missing items to the other side.\n\nNothing is deleted. "
                                    "Continue?") != QMessageBox.Yes:
                return False
        sid = self.section_combo.currentData() or None
        if not changed and sid != e.section_id() and sid and e.state.get("pairs"):
            if QMessageBox.question(self, APP_TITLE,
                                    f"Synced tasks will go into the section '{self.section_combo.currentText()}'."
                                    "\n\nLinked tasks without a section (or in the previously chosen section) are "
                                    "moved there. Linked tasks in your other sections stay where they are and stop "
                                    "syncing; their Keep items are left as they are.\n\nNothing is deleted. "
                                    "Continue?") != QMessageBox.Yes:
                return False
        title = self.keep_combo.currentText().replace("  (archived)", "").replace("  (not found)", "")
        sname = self.section_combo.currentText().replace("  (not found)", "") if sid else ""
        e.set_binding(nid, pid, self.interval.value(), title, self.project_combo.currentText(), sid, sname)
        self.app.worker.set_interval(self.interval.value())
        try:
            if self.autostart.isEnabled() and self.autostart.isChecked() != autostart_enabled():
                set_autostart(self.autostart.isChecked())
        except Exception as exc:  # noqa: BLE001
            log.exception("Could not change the startup shortcut")
            QMessageBox.warning(self, APP_TITLE, f"Could not change the startup shortcut:\n{exc}")
        return True

    def save(self) -> None:
        if self._apply():
            self.app.worker.sync_now()
            self.accept()


# --------------------------------------------------------------------------
# Tray app
# --------------------------------------------------------------------------
class BridgeApp:
    def __init__(self, qapp: QApplication, engine: bc.BridgeEngine):
        self.qapp = qapp
        self.engine = engine
        self.bus = Bus()
        self.bus.event.connect(self._on_event)
        self.worker = bc.SyncWorker(engine, lambda kind, payload: self.bus.event.emit(kind, payload))
        self.tray = QSystemTrayIcon(app_icon())
        m = QMenu()
        self.status_action = m.addAction("Starting…")
        self.status_action.setEnabled(False)
        m.addSeparator()
        m.addAction("Sync now").triggered.connect(self.worker.sync_now)
        m.addAction("Settings…").triggered.connect(self.show_settings)
        m.addAction("Open log").triggered.connect(
            lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(engine.dir / "bridge.log"))))
        m.addSeparator()
        self.autostart_action = m.addAction("Start with Windows")
        self.autostart_action.setCheckable(True)
        self.autostart_action.setEnabled(startup_shortcut_path() is not None)
        self.autostart_action.setChecked(autostart_enabled())
        self.autostart_action.toggled.connect(self.toggle_autostart)
        m.addSeparator()
        m.addAction("Quit").triggered.connect(self.quit)
        self.menu = m
        self.tray.setContextMenu(m)
        self.tray.activated.connect(self._activated)
        self.tray.setToolTip(APP_TITLE)
        self.tray.show()
        self._settings: SettingsDialog | None = None
        self._last_error: str | None = None

    def status_text(self) -> str:
        e = self.engine
        if e.status == "ok" and e.last_sync:
            return f"Synced {e.last_sync:%H:%M}"
        if e.last_error:
            return e.last_error
        return "Not synced yet"

    def _activated(self, reason) -> None:
        if reason in (QSystemTrayIcon.Trigger, QSystemTrayIcon.DoubleClick):
            self.show_settings()

    def _on_event(self, kind: str, payload) -> None:
        if kind != "synced":
            return
        text = self.status_text()
        self.status_action.setText(text if len(text) < 90 else text[:87] + "…")
        self.tray.setToolTip(f"{APP_TITLE}\n{text}"[:120])
        error = self.engine.status in ("error", "setup")
        self.tray.setIcon(app_icon(error))
        res = payload.get("result") or {}
        if error and self.engine.last_error != self._last_error and self.engine.status == "error" \
                and not self.engine.last_error.startswith("offline"):
            self.tray.showMessage(APP_TITLE, self.engine.last_error, QSystemTrayIcon.Warning, 6000)
        if res.get("conflicts"):
            self.tray.showMessage(APP_TITLE, f"{len(res['conflicts'])} conflict(s) resolved (last write wins). "
                                  "See the log for details.", QSystemTrayIcon.Information, 5000)
        self._last_error = self.engine.last_error

    def show_settings(self) -> None:
        if self._settings is not None and self._settings.isVisible():
            self._settings.raise_()
            self._settings.activateWindow()
            return
        self._settings = SettingsDialog(self)
        self._settings.show()
        self._settings.raise_()
        self._settings.activateWindow()

    def toggle_autostart(self, on: bool) -> None:
        try:
            set_autostart(on)
        except Exception as exc:  # noqa: BLE001
            log.exception("Could not change the startup shortcut")
            QMessageBox.warning(None, APP_TITLE, f"Could not change the startup shortcut:\n{exc}")
            self.autostart_action.blockSignals(True)
            self.autostart_action.setChecked(autostart_enabled())
            self.autostart_action.blockSignals(False)

    def start(self) -> None:
        self.worker.start()
        c = self.engine.connected()
        if not (c["keep"] and c["todoist"] and self.engine.is_configured()):
            QTimer.singleShot(400, self.show_settings)

    def quit(self) -> None:
        self.worker.stop()
        self.worker.join(timeout=10)
        self.engine.save_state()
        self.tray.hide()
        self.qapp.quit()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=APP_TITLE)
    ap.add_argument("--data-dir", help="override the data folder (default %%APPDATA%%\\KeepTodoistBridge)")
    args = ap.parse_args(argv)
    if args.data_dir:
        os.environ["KTB_DATA_DIR"] = args.data_dir
    directory = bc.data_dir()
    setup_logging(directory)

    set_app_user_model_id()
    QApplication.setQuitOnLastWindowClosed(False)
    qapp = QApplication(sys.argv[:1])
    qapp.setApplicationName(APP_TITLE)
    qapp.setWindowIcon(app_icon())
    qapp.setFont(QFont("Segoe UI", 10))

    lock = QLockFile(str(directory / "app.lock"))
    if not lock.tryLock(200):
        QMessageBox.information(None, APP_TITLE, "Keep-Todoist Bridge is already running (see the tray icon).")
        return 0
    if not QSystemTrayIcon.isSystemTrayAvailable():
        log.warning("No system tray available")
    engine = bc.BridgeEngine(directory=directory)
    app = BridgeApp(qapp, engine)
    app.start()
    log.info("Keep-Todoist Bridge started (data folder: %s)", directory)
    rc = qapp.exec()
    lock.unlock()
    return rc


if __name__ == "__main__":
    sys.exit(main())
