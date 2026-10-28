"""Sync core for Keep-Todoist Bridge (no Qt dependency, unit-testable).

Keeps ONE Google Keep checklist note in two-way sync with ONE Todoist project
(personal or workspace/team project), optionally limited to ONE section of that project.

* Google Keep: UNOFFICIAL API via gkeepapi (same library and login as KeepNotes Widgets).
* Todoist: official API v1 (https://api.todoist.com/api/v1), incremental /sync with
  batched, idempotent commands (each command carries a UUID).

Credentials are read from Windows Credential Manager (keyring) using the SAME entries as
KeepNotes Widgets and Todoist Widgets, so no new login is needed. Nothing secret is ever
written to state.json, config.json or bridge.log.
"""
from __future__ import annotations

import copy
import datetime as _dt
import json
import logging
import os
import random
import secrets
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable

APP_NAME = "KeepTodoistBridge"
APP_TITLE = "Keep-Todoist Bridge"

# Re-used credential entries (exactly as written by the existing widget apps)
KEEP_KEYRING_SERVICE = "KeepNotesWidgets"
KEEP_KEYRING_EMAIL_KEY = "account-email"  # value: Google email; then username=<email> -> master token
TODOIST_KEYRING_SERVICE = "TodoistWidgets"
TODOIST_KEYRING_TOKEN_KEY = "api-token"

API_BASE = "https://api.todoist.com/api/v1"
RESOURCE_TYPES = ["projects", "sections", "items", "workspaces", "user"]
TERMINAL_HTTP = {400, 403, 404, 409, 410, 422}
MAX_RETRIES = 8
SAFETY_MIN_PAIRS = 3  # never mass-delete when one side suddenly looks empty
DEFAULT_INTERVAL = 60

log = logging.getLogger("keeptodoistbridge")


# --------------------------------------------------------------------------
# Files
# --------------------------------------------------------------------------
def data_dir() -> Path:
    """%APPDATA%\\KeepTodoistBridge on Windows (override with KTB_DATA_DIR)."""
    override = os.environ.get("KTB_DATA_DIR")
    if override:
        p = Path(override)
    elif os.name == "nt":
        p = Path(os.environ.get("APPDATA", str(Path.home()))) / APP_NAME
    else:
        p = Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))) / APP_NAME
    p.mkdir(parents=True, exist_ok=True)
    return p

# --------------------------------------------------------------------------
# TLS trust fix (works on every PC, no extra packages)
# --------------------------------------------------------------------------
# gpsoauth (sign-in) trusts ONLY the Windows certificate store, while requests
# (gkeepapi sync) trusts ONLY certifi. On a fresh PC the Windows store can lack a
# Google root (Windows downloads roots lazily), and with antivirus/proxy HTTPS
# scanning the scanner's root is only in the Windows store. Either case gives
# "Could not reach Google: SSLError". Fix: build one bundle = certifi + the
# Windows ROOT/CA stores, and let both gpsoauth and requests use it.
_TRUST_FIX = "not applied"


def _build_ca_bundle() -> Path | None:
    import ssl

    parts: list[str] = []
    try:
        import certifi

        parts.append(Path(certifi.where()).read_text(encoding="ascii", errors="ignore"))
    except Exception as exc:  # noqa: BLE001
        log.warning("certifi unavailable for CA bundle: %s", type(exc).__name__)
    n_win = 0
    if os.name == "nt":
        server_auth = "1.3.6.1.5.5.7.3.1"
        for store in ("ROOT", "CA"):
            try:
                for der, enc, trust in ssl.enum_certificates(store):
                    if enc != "x509_asn":
                        continue
                    if trust is not True and server_auth not in trust:
                        continue
                    parts.append(ssl.DER_cert_to_PEM_cert(der))
                    n_win += 1
            except Exception as exc:  # noqa: BLE001
                log.warning("Could not read Windows %s store: %s", store, type(exc).__name__)
    if not parts:
        return None
    path = data_dir() / "ca-bundle.pem"
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text("\n".join(parts), encoding="ascii", errors="ignore")
    ssl.create_default_context(cafile=str(tmp))  # raises if the bundle is unusable
    os.replace(tmp, path)
    log.info("CA bundle ready: certifi + %d Windows certificates", n_win)
    return path


def _install_trust_fix() -> str:
    if os.environ.get("KTB_NO_TRUST_FIX"):
        return "disabled by KTB_NO_TRUST_FIX"
    bundle = _build_ca_bundle()
    if bundle is None:
        return "no bundle"
    # requests/gkeepapi: honour an explicit user setting, otherwise use our bundle
    if not (os.environ.get("REQUESTS_CA_BUNDLE") or os.environ.get("CURL_CA_BUNDLE")):
        os.environ["REQUESTS_CA_BUNDLE"] = str(bundle)
    # gpsoauth: keep its special TLS context, just add the extra trust anchors
    try:
        import gpsoauth

        adapter = gpsoauth.AuthHTTPAdapter
        if not getattr(adapter, "_ktb_patched", False):
            orig = adapter.init_poolmanager

            def init_poolmanager(self, *args, **kwargs):
                orig(self, *args, **kwargs)
                ctx = self.poolmanager.connection_pool_kw.get("ssl_context")
                if ctx is not None:
                    try:
                        ctx.load_verify_locations(cafile=str(bundle))
                    except Exception as exc:  # noqa: BLE001
                        log.warning("Could not add CA bundle to gpsoauth: %s", type(exc).__name__)

            adapter.init_poolmanager = init_poolmanager
            adapter._ktb_patched = True
    except Exception as exc:  # noqa: BLE001
        return f"requests only (gpsoauth patch failed: {type(exc).__name__})"
    return f"applied ({bundle})"


try:
    _TRUST_FIX = _install_trust_fix()
except Exception as _exc:  # noqa: BLE001  never block startup over this
    _TRUST_FIX = f"failed: {type(_exc).__name__}"
    log.warning("TLS trust fix not applied: %s", _TRUST_FIX)




def keepnotes_widgets_dir() -> Path:
    override = os.environ.get("KNW_DATA_DIR")
    if override:
        return Path(override)
    if os.name == "nt":
        return Path(os.environ.get("APPDATA", str(Path.home()))) / "KeepNotesWidgets"
    return Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))) / "KeepNotesWidgets"


def read_json(path: Path, default: Any) -> Any:
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        return default
    except (OSError, ValueError) as exc:
        log.warning("Could not read %s (%s); using defaults", path.name, exc)
        return default


def atomic_write_json(path: Path, data: Any) -> None:
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=1, default=str)
    os.replace(tmp, path)


def norm(text: str) -> str:
    """Matching key: case-insensitive, trimmed."""
    return (text or "").strip().casefold()


def to_ts(value: Any) -> float:
    """datetime / ISO string / number -> epoch seconds (0 when unknown)."""
    if value is None or value == "":
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, _dt.datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=_dt.timezone.utc)
        return value.timestamp()
    try:
        s = str(value).replace("Z", "+00:00")
        d = _dt.datetime.fromisoformat(s)
        if d.tzinfo is None:
            d = d.replace(tzinfo=_dt.timezone.utc)
        return d.timestamp()
    except ValueError:
        return 0.0


def new_keep_item_id() -> str:
    """Same id format gkeepapi generates for new nodes."""
    return "{:x}.{:016x}".format(int(time.time() * 1000), random.getrandbits(64))


# --------------------------------------------------------------------------
# Credentials (shared with KeepNotes Widgets / Todoist Widgets)
# --------------------------------------------------------------------------
class KeepCredentialStore:
    def get(self) -> tuple[str, str] | None:
        import keyring

        email = keyring.get_password(KEEP_KEYRING_SERVICE, KEEP_KEYRING_EMAIL_KEY)
        if not email:
            return None
        token = keyring.get_password(KEEP_KEYRING_SERVICE, email)
        return (email, token) if token else None

    def set(self, email: str, master_token: str) -> None:
        import keyring

        keyring.set_password(KEEP_KEYRING_SERVICE, KEEP_KEYRING_EMAIL_KEY, email)
        keyring.set_password(KEEP_KEYRING_SERVICE, email, master_token)


class TodoistCredentialStore:
    def get(self) -> str | None:
        import keyring

        return keyring.get_password(TODOIST_KEYRING_SERVICE, TODOIST_KEYRING_TOKEN_KEY) or None

    def set(self, token: str) -> None:
        import keyring

        keyring.set_password(TODOIST_KEYRING_SERVICE, TODOIST_KEYRING_TOKEN_KEY, token)


class MemoryKeepCreds(KeepCredentialStore):
    def __init__(self, email=None, token=None):
        self._v = (email, token) if email and token else None

    def get(self):
        return self._v

    def set(self, email, master_token):
        self._v = (email, master_token)


class MemoryTodoistCreds(TodoistCredentialStore):
    def __init__(self, token=None):
        self._t = token

    def get(self):
        return self._t

    def set(self, token):
        self._t = token


# Helpers copied from the widget apps (connect dialogs use them)
class AuthError(Exception):
    pass


def clean_oauth_cookie(value: str) -> str:
    value = value.strip()
    if value.lower().startswith("oauth_token="):
        value = value.split("=", 1)[1]
    return value.strip().strip(";").strip().strip('"').strip("'").strip()


def exchange_oauth_token(email: str, oauth_token: str, android_id: str) -> str:
    import gpsoauth

    token = clean_oauth_cookie(oauth_token)
    if not token:
        raise AuthError("The oauth_token value is empty.")
    try:
        res = gpsoauth.exchange_token(email.strip(), token, android_id)
    except Exception as exc:  # noqa: BLE001
        detail = str(exc).splitlines()[0][:200] if str(exc) else ""
        log.warning("Token exchange failed: %s: %s (trust fix: %s)", type(exc).__name__, detail, _TRUST_FIX)
        raise AuthError(
            f"Could not reach Google: {type(exc).__name__}"
            + (f" - {detail}" if detail else "")
            + ". Check your internet connection (or antivirus HTTPS scanning); details are in bridge.log."
        ) from exc
    master = res.get("Token") if isinstance(res, dict) else None
    if not master:
        err = res.get("Error", "unknown error") if isinstance(res, dict) else "unknown error"
        raise AuthError(
            f"Google refused the token exchange ({err}). The oauth_token cookie is single-use and "
            "expires after a few minutes: sign in on the EmbeddedSetup page again and paste the "
            "fresh value right away.")
    return master


def looks_like_master_token(value: str) -> bool:
    return value.strip().startswith("aas_et/") and len(value.strip()) > 40


def clean_token(value: str) -> str:
    value = value.strip().strip('"').strip("'").strip()
    if value.lower().startswith("bearer "):
        value = value[7:].strip()
    return value


def looks_like_token(value: str) -> bool:
    v = clean_token(value)
    return len(v) >= 30 and all(c.isalnum() for c in v)


# --------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------
class ApiError(Exception):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class TodoistAuthError(ApiError):
    pass


class NotConfigured(Exception):
    pass


class SafetyStop(Exception):
    pass


def friendly_error(exc: BaseException, side: str = "") -> str:
    name = type(exc).__name__
    where = f" to {side}" if side else ""
    if name in ("ConnectionError", "Timeout", "ConnectTimeout", "ReadTimeout", "gaierror"):
        return f"offline (no connection{where})"
    if name in ("LoginException", "BrowserLoginRequiredException"):
        return "Google rejected the login - reconnect Google Keep in Settings"
    if isinstance(exc, TodoistAuthError):
        return "Todoist rejected the API token - reconnect Todoist in Settings"
    if isinstance(exc, (ApiError, NotConfigured, SafetyStop, AuthError)):
        return str(exc)
    msg = str(exc).splitlines()[0][:160] if str(exc) else ""
    return f"{name}: {msg}" if msg else name


# --------------------------------------------------------------------------
# Todoist HTTP client (same API usage as Todoist Widgets)
# --------------------------------------------------------------------------
class TodoistClient:
    def __init__(self, token: str, session=None, base: str = API_BASE, timeout: float = 25):
        if session is None:
            import requests

            session = requests.Session()
        self._token = token
        self.session = session
        self.base = base
        self.timeout = timeout

    def __repr__(self) -> str:  # never show the token
        return "TodoistClient(<token hidden>)"

    def _req(self, method: str, path: str, allow_404: bool = False, **kw):
        headers = {"Authorization": f"Bearer {self._token}"}
        r = self.session.request(method, self.base + path, headers=headers, timeout=self.timeout, **kw)
        code = r.status_code
        if code == 401:
            raise TodoistAuthError("Todoist rejected the API token (HTTP 401)", 401)
        if code == 404 and allow_404:
            return None
        if code == 429:
            raise ApiError("Todoist rate limit reached (HTTP 429), will retry", 429)
        if code >= 400:
            raise ApiError(f"Todoist returned HTTP {code}", code)
        if not r.content:
            return None
        return r.json()

    def sync(self, sync_token: str, commands: list[dict] | None = None,
             resource_types: list[str] | None = None) -> dict:
        data = {"sync_token": sync_token or "*",
                "resource_types": json.dumps(resource_types or RESOURCE_TYPES)}
        if commands:
            data["commands"] = json.dumps(commands)
        return self._req("POST", "/sync", data=data) or {}

    def whoami(self) -> dict:
        return self.sync("*", resource_types=["user"]).get("user") or {}

    def get_task(self, task_id: str) -> dict | None:
        return self._req("GET", f"/tasks/{task_id}", allow_404=True)

    def completed_ids(self, project_id: str, days: int = 30) -> set[str]:
        until = _dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(minutes=5)
        since = until - _dt.timedelta(days=days)
        out: set[str] = set()
        cursor = None
        for _ in range(20):
            params = {"since": since.strftime("%Y-%m-%dT%H:%M:%SZ"),
                      "until": until.strftime("%Y-%m-%dT%H:%M:%SZ"),
                      "project_id": project_id, "limit": 200}
            if cursor:
                params["cursor"] = cursor
            res = self._req("GET", "/tasks/completed/by_completion_date", params=params) or {}
            for it in res.get("items", []) or []:
                tid = it.get("id") or it.get("task_id")
                if tid:
                    out.add(str(tid))
            cursor = res.get("next_cursor")
            if not cursor:
                break
        return out


# --------------------------------------------------------------------------
# Google Keep adapter (wraps a gkeepapi.Keep object)
# --------------------------------------------------------------------------
class KeepAdapter:
    def __init__(self, keep=None, keep_factory: Callable[[], Any] | None = None):
        if keep is None:
            if keep_factory is None:
                import gkeepapi

                keep_factory = gkeepapi.Keep
            keep = keep_factory()
        self.keep = keep
        self.authenticated = False
        self.email: str | None = None

    def authenticate(self, email: str, master_token: str, device_id: str) -> None:
        self.keep.authenticate(email, master_token, state=None, sync=False, device_id=device_id)
        self.authenticated = True
        self.email = email

    def sync(self) -> None:
        try:
            self.keep.sync()
        except Exception as exc:  # noqa: BLE001
            if type(exc).__name__ == "ResyncRequiredException":
                log.warning("Google asked for a full Keep resync")
                self.keep.sync(resync=True)
            else:
                raise

    # -- reading ----------------------------------------------------------
    def _note(self, note_id: str):
        from gkeepapi import node as gn

        n = self.keep.get(note_id)
        if n is None or not isinstance(n, gn.List) or n.trashed or n.deleted:
            return None
        return n

    def checklists(self) -> list[dict]:
        from gkeepapi import node as gn

        out = [{"id": n.id, "title": n.title or "(untitled list)", "archived": bool(n.archived)}
               for n in self.keep.all()
               if isinstance(n, gn.List) and not n.trashed and not n.deleted]
        return sorted(out, key=lambda d: d["title"].casefold())

    def items(self, note_id: str) -> dict[str, dict] | None:
        note = self._note(note_id)
        if note is None:
            return None
        return {i.id: {"text": i.text or "", "checked": bool(i.checked),
                       "updated": to_ts(i.timestamps.updated)} for i in note.items}

    def title(self, note_id: str) -> str | None:
        note = self._note(note_id)
        return None if note is None else (note.title or "")

    # -- writing ----------------------------------------------------------
    def _find(self, note, item_id):
        from gkeepapi import node as gn

        for c in note.children:
            if c.id == item_id and isinstance(c, gn.ListItem):
                return c
        return None

    def add_item(self, note_id: str, item_id: str, text: str, checked: bool) -> None:
        from gkeepapi import node as gn

        note = self._note(note_id)
        if note is None:
            raise NotConfigured("The Keep list is gone")
        existing = self._find(note, item_id)
        if existing is not None and not existing.deleted:
            return  # idempotent replay
        node = gn.ListItem(parent_id=note.id, parent_server_id=note.server_id, id_=item_id)
        node.checked = bool(checked)
        node.text = text
        items = list(note.items)
        if items:  # bottom of the list
            node.sort = min(int(i.sort) for i in items) - gn.List.SORT_DELTA
        note.append(node, True)
        note.touch(True)

    def set_text(self, note_id: str, item_id: str, text: str) -> None:
        note = self._note(note_id)
        item = self._find(note, item_id) if note is not None else None
        if item is not None and not item.deleted and item.text != text:
            item.text = text

    def set_checked(self, note_id: str, item_id: str, checked: bool) -> None:
        note = self._note(note_id)
        item = self._find(note, item_id) if note is not None else None
        if item is not None and not item.deleted and bool(item.checked) != bool(checked):
            item.checked = bool(checked)

    def delete_item(self, note_id: str, item_id: str) -> None:
        note = self._note(note_id)
        item = self._find(note, item_id) if note is not None else None
        if item is not None and not item.deleted:
            item.delete()
            note.touch(True)

    def create_list(self, title: str) -> str:
        node = self.keep.createList(title, [])
        return node.id


# --------------------------------------------------------------------------
# Engine
# --------------------------------------------------------------------------
def _empty_state() -> dict:
    return {"version": 1, "binding": {"keep_note_id": None, "todoist_project_id": None},
            "pairs": [],  # [{keep_id, task_id, text, checked}] = mapping + last-seen snapshot
            "todoist": {"sync_token": "*", "projects": {}, "sections": {}, "workspaces": {}, "items": {}},
            "td_queue": [], "keep_queue": [], "last_sync": None,
            # section the linked tasks were last kept in (None = whole project, no section filter)
            "section": None,
            # Keep items deliberately left out of the sync (their task lives in another section)
            "ignored_keep_ids": []}


class BridgeEngine:
    """All network access happens on the caller's thread (the SyncWorker)."""

    def __init__(self, keep: KeepAdapter | None = None, keep_creds: KeepCredentialStore | None = None,
                 todoist_creds: TodoistCredentialStore | None = None,
                 todoist_factory: Callable[[str], Any] | None = None,
                 directory: Path | str | None = None):
        self._keep = keep
        self.keep_creds = keep_creds if keep_creds is not None else KeepCredentialStore()
        self.todoist_creds = todoist_creds if todoist_creds is not None else TodoistCredentialStore()
        self.todoist_factory = todoist_factory or (lambda token: TodoistClient(token))
        self.dir = Path(directory) if directory else data_dir()
        self.dir.mkdir(parents=True, exist_ok=True)
        self.state_path = self.dir / "state.json"
        self.config_path = self.dir / "config.json"
        self.config = read_json(self.config_path, {})
        self.config.setdefault("interval", DEFAULT_INTERVAL)
        self.config.setdefault("keep_note_id", None)
        self.config.setdefault("todoist_project_id", None)
        self.config.setdefault("todoist_section_id", None)  # missing/None = no section (1.0.0 behaviour)
        self.state = _empty_state()
        loaded = read_json(self.state_path, None)
        if isinstance(loaded, dict) and loaded.get("version") == 1:
            for k, v in loaded.items():
                self.state[k] = v
            td = self.state["todoist"]
            if "sections" not in td:  # state from 1.0.0: fetch sections with one full sync
                td["sections"] = {}
                td["sync_token"] = "*"
        self.td = None
        self.status = "idle"  # idle | ok | error | setup
        self.last_error: str | None = None
        self.last_sync: _dt.datetime | None = None
        self.lock = threading.RLock()
        self._keep_queue_applied = False
        self._keep_synced = False
        self.keep_lists: list[dict] = []

    # -- config / state ---------------------------------------------------
    @property
    def keep(self) -> KeepAdapter:
        if self._keep is None:
            self._keep = KeepAdapter()
        return self._keep

    def save_config(self) -> None:
        atomic_write_json(self.config_path, self.config)

    def save_state(self) -> None:
        with self.lock:
            data = copy.deepcopy(self.state)
        atomic_write_json(self.state_path, data)

    def android_id(self) -> str:
        """Re-use KeepNotes Widgets' device id (the master token was issued for it)."""
        other = read_json(keepnotes_widgets_dir() / "config.json", {})
        if isinstance(other, dict) and other.get("android_id"):
            return other["android_id"]
        if not self.config.get("android_id"):
            self.config["android_id"] = secrets.token_hex(8)
            self.save_config()
        return self.config["android_id"]

    def set_binding(self, keep_note_id: str, project_id: str, interval: int | None = None,
                    keep_title: str = "", project_name: str = "", section_id: str | None = None,
                    section_name: str = "") -> None:
        with self.lock:
            self.config["keep_note_id"] = keep_note_id
            self.config["todoist_project_id"] = project_id
            self.config["keep_title"] = keep_title
            self.config["project_name"] = project_name
            self.config["todoist_section_id"] = section_id or None
            self.config["section_name"] = section_name if section_id else ""
            if interval:
                self.config["interval"] = max(15, int(interval))
            self.save_config()
            self._check_binding()

    def _check_binding(self) -> None:
        b = self.state["binding"]
        want = {"keep_note_id": self.config.get("keep_note_id"),
                "todoist_project_id": self.config.get("todoist_project_id")}
        if b != want:
            if b.get("keep_note_id") or b.get("todoist_project_id"):
                log.info("Sync pair changed; starting over with a first-run merge "
                         "(nothing is deleted on either side)")
            dropped = len(self.state["td_queue"]) + len(self.state["keep_queue"])
            if dropped:
                log.warning("Dropped %d queued edits that belonged to the previous pair", dropped)
            td = self.state["todoist"]
            if b.get("keep_note_id") != want["keep_note_id"]:
                self.state["ignored_keep_ids"] = []
            self.state.update(binding=want, pairs=[], td_queue=[], keep_queue=[],
                              section=self.section_id())
            td["items"] = {}
            td["sync_token"] = "*"
            self.save_state()

    def relink(self) -> None:
        """Forget the mapping and match both sides by text again on the next sync (deletes nothing)."""
        with self.lock:
            self.state["binding"] = {"keep_note_id": None, "todoist_project_id": None}
            self._check_binding()
            self.state["ignored_keep_ids"] = []
            self.save_state()
        log.info("Mapping cleared by the user; the next sync re-matches items by text")

    def section_id(self) -> str | None:
        return self.config.get("todoist_section_id") or None

    def is_configured(self) -> bool:
        return bool(self.config.get("keep_note_id") and self.config.get("todoist_project_id"))

    # -- connections ------------------------------------------------------
    def _ensure_keep(self) -> None:
        if self.keep.authenticated:
            return
        creds = self.keep_creds.get()
        if not creds:
            raise NotConfigured("Google Keep is not connected - open Settings")
        self.keep.authenticate(creds[0], creds[1], self.android_id())

    def _ensure_todoist(self) -> None:
        if self.td is not None:
            return
        token = self.todoist_creds.get()
        if not token:
            raise NotConfigured("Todoist is not connected - open Settings")
        self.td = self.todoist_factory(token)

    def keep_sign_in(self, email: str, master_token: str) -> None:
        with self.lock:
            self.keep.authenticate(email.strip(), master_token.strip(), self.android_id())
            self.keep_creds.set(email.strip(), master_token.strip())
            self._keep_synced = False
        log.info("Connected to Google Keep as %s", email.strip())

    def todoist_set_token(self, token: str) -> dict:
        token = clean_token(token)
        client = self.todoist_factory(token)
        user = client.whoami()
        if not user.get("id"):
            raise ApiError("Todoist did not return a user for this token")
        with self.lock:
            self.td = client
            self.todoist_creds.set(token)
        log.info("Connected to Todoist as %s", user.get("full_name") or user.get("id"))
        return user

    def connected(self) -> dict:
        return {"keep": self.keep_creds.get() is not None, "todoist": self.todoist_creds.get() is not None}

    # -- sources for the Settings dialog ----------------------------------
    def refresh_sources(self) -> dict:
        """Fetch Keep checklists and Todoist projects (incl. workspace projects)."""
        out: dict = {"keep": None, "todoist": None, "workspaces": {}, "sections": {}, "errors": []}
        with self.lock:
            try:
                self._ensure_keep()
                self.keep.sync()
                self._keep_synced = True
                self.keep_lists = self.keep.checklists()
                out["keep"] = self.keep_lists
            except Exception as exc:  # noqa: BLE001
                out["errors"].append("Google Keep: " + friendly_error(exc, "Google"))
            try:
                self._ensure_todoist()
                self._todoist_sync()
                out["todoist"] = self.project_choices()
                out["workspaces"] = dict(self.state["todoist"]["workspaces"])
                out["sections"] = self.sections_by_project()
            except Exception as exc:  # noqa: BLE001
                out["errors"].append("Todoist: " + friendly_error(exc, "Todoist"))
            self.save_state()
        return out

    def project_choices(self) -> list[dict]:
        td = self.state["todoist"]
        ws = td["workspaces"]
        out = []
        for pid, p in td["projects"].items():
            wid = p.get("workspace_id")
            group = f"Team: {ws.get(wid, 'Workspace')}" if wid else "Personal"
            out.append({"id": pid, "name": p.get("name", ""), "group": group,
                        "inbox": bool(p.get("inbox")), "workspace_id": wid, "order": p.get("order") or 0})
        out.sort(key=lambda d: (d["group"] != "Personal", d["group"].casefold(), not d["inbox"],
                                d.get("order", 0), d["name"].casefold()))
        return out

    def sections_by_project(self) -> dict[str, list[dict]]:
        """{project_id: [{id, name}, ...]} in Todoist's order (from the local sync cache)."""
        secs = self.state["todoist"].get("sections") or {}
        out: dict[str, list[dict]] = {}
        for sid, s in list(secs.items()):
            out.setdefault(s.get("project_id"), []).append(
                {"id": sid, "name": s.get("name", ""), "order": s.get("order") or 0})
        for lst in out.values():
            lst.sort(key=lambda d: (d["order"], d["name"].casefold()))
        return out

    def create_todoist_section(self, name: str, project_id: str) -> str:
        name = (name or "").strip()
        if not name:
            raise ApiError("A section needs a name")
        if not project_id:
            raise ApiError("Choose a Todoist project first")
        with self.lock:
            self._ensure_todoist()
            temp = str(uuid.uuid4())
            cmd = {"type": "section_add", "temp_id": temp, "uuid": str(uuid.uuid4()),
                   "args": {"name": name, "project_id": project_id}}
            resp = self.td.sync(self.state["todoist"]["sync_token"], [cmd])
            st = (resp.get("sync_status") or {}).get(cmd["uuid"])
            if st != "ok":
                err = st.get("error") if isinstance(st, dict) else "no answer"
                raise ApiError(f"Todoist could not create the section ({err})")
            sid = (resp.get("temp_id_mapping") or {}).get(temp)
            self._merge_todoist(resp)
            if sid and sid not in self.state["todoist"]["sections"]:
                self.state["todoist"]["sections"][sid] = {"name": name, "project_id": project_id, "order": 0}
            self.save_state()
        log.info("Created Todoist section %r", name)
        return sid

    def create_keep_list(self, title: str) -> str:
        with self.lock:
            self._ensure_keep()
            nid = self.keep.create_list(title.strip() or "Todoist")
            self.keep.sync()
            self.keep_lists = self.keep.checklists()
        log.info("Created Keep list %r", title)
        return nid

    def create_todoist_project(self, name: str, workspace_id: str | None = None) -> str:
        with self.lock:
            self._ensure_todoist()
            temp = str(uuid.uuid4())
            args = {"name": name.strip() or "Google Keep"}
            if workspace_id:
                args["workspace_id"] = workspace_id
            cmd = {"type": "project_add", "temp_id": temp, "uuid": str(uuid.uuid4()), "args": args}
            resp = self.td.sync(self.state["todoist"]["sync_token"], [cmd])
            st = (resp.get("sync_status") or {}).get(cmd["uuid"])
            if st != "ok":
                err = st.get("error") if isinstance(st, dict) else "no answer"
                raise ApiError(f"Todoist could not create the project ({err})")
            pid = (resp.get("temp_id_mapping") or {}).get(temp)
            self._merge_todoist(resp)
            if pid and pid not in self.state["todoist"]["projects"]:
                self.state["todoist"]["projects"][pid] = {"name": args["name"], "workspace_id": workspace_id}
            self.save_state()
        log.info("Created Todoist project %r", name)
        return pid

    # -- Todoist sync -----------------------------------------------------
    def _todoist_sync(self) -> None:
        td = self.state["todoist"]
        queue = self.state["td_queue"]
        cmds = [{k: v for k, v in c.items() if k != "tries"} for c in queue]
        token = td["sync_token"]
        try:
            resp = self.td.sync(token, cmds)
        except ApiError as exc:
            if exc.status == 400 and token != "*" and not cmds:
                log.warning("Todoist sync token rejected; doing a full sync")
                td["sync_token"] = "*"
                resp = self.td.sync("*", cmds)
            else:
                raise
        statuses = resp.get("sync_status") or {}
        mapping = resp.get("temp_id_mapping") or {}
        sent = {c["uuid"] for c in cmds}
        keep = []
        for c in queue:
            if c["uuid"] not in sent:
                keep.append(c)
                continue
            st = statuses.get(c["uuid"])
            if st == "ok":
                continue
            if isinstance(st, dict):
                code = st.get("http_code")
                if code in TERMINAL_HTTP or code is None:
                    log.error("Todoist rejected %s (%s); dropped", c["type"],
                              st.get("error_tag") or st.get("error"))
                    continue
            c["tries"] = c.get("tries", 0) + 1
            if c["tries"] >= MAX_RETRIES:
                log.error("Giving up on %s after %d tries", c["type"], c["tries"])
                continue
            keep.append(c)
        self.state["td_queue"] = keep
        if mapping:
            for p in self.state["pairs"]:
                if p["task_id"] in mapping:
                    p["task_id"] = mapping[p["task_id"]]
            for c in keep:
                if c["args"].get("id") in mapping:
                    c["args"]["id"] = mapping[c["args"]["id"]]
        self._merge_todoist(resp)

    def _merge_todoist(self, resp: dict) -> None:
        td = self.state["todoist"]
        pid = self.config.get("todoist_project_id")
        td.setdefault("sections", {})
        if resp.get("full_sync"):
            td["projects"], td["items"], td["sections"] = {}, {}, {}
        for p in resp.get("projects", []) or []:
            if p.get("is_deleted") or p.get("is_archived"):
                td["projects"].pop(p["id"], None)
            else:
                td["projects"][p["id"]] = {"name": p.get("name", ""), "workspace_id": p.get("workspace_id"),
                                           "inbox": bool(p.get("inbox_project")),
                                           "order": p.get("child_order") or 0}
        for s in resp.get("sections", []) or []:
            if s.get("is_deleted") or s.get("is_archived"):
                td["sections"].pop(s["id"], None)
            else:
                td["sections"][s["id"]] = {"name": s.get("name", ""), "project_id": s.get("project_id"),
                                           "order": s.get("section_order") or 0}
        ws = resp.get("workspaces")
        if ws:
            for w in (ws.values() if isinstance(ws, dict) else ws):
                if isinstance(w, dict) and w.get("id"):
                    if w.get("is_deleted"):
                        td["workspaces"].pop(w["id"], None)
                    else:
                        td["workspaces"][w["id"]] = w.get("name", "Workspace")
        for it in resp.get("items", []) or []:
            iid = it["id"]
            if it.get("is_deleted") or it.get("project_id") != pid:
                td["items"].pop(iid, None)
                continue
            due = it.get("due") or {}
            td["items"][iid] = {"content": it.get("content", ""), "checked": bool(it.get("checked")),
                                "section_id": it.get("section_id") or None,
                                "updated_at": it.get("updated_at"), "completed_at": it.get("completed_at"),
                                "recurring": bool(due.get("is_recurring"))}
        if resp.get("sync_token"):
            td["sync_token"] = resp["sync_token"]

    def _add_args(self, content: str) -> dict:
        """item_add arguments: the bound project and, when chosen, the bound section."""
        args = {"content": content, "project_id": self.config["todoist_project_id"]}
        if self.section_id():
            args["section_id"] = self.section_id()
        return args

    def _td_cmd(self, ctype: str, args: dict, temp_id: str | None = None) -> dict:
        c = {"type": ctype, "uuid": str(uuid.uuid4()), "args": args, "tries": 0}
        if temp_id:
            c["temp_id"] = temp_id
        self.state["td_queue"].append(c)
        return c

    def _resolve_missing(self, task_id: str, base: dict, cache: dict) -> str | None:
        """A mapped task vanished from the project cache: completed, deleted or moved?"""
        if _is_temp(task_id):
            return "pending"  # add not confirmed yet (see _fix_unmapped_adds)
        if base.get("checked"):
            return "completed"  # completed tasks drop out of a full sync; nothing changed
        try:
            task = self.td.get_task(task_id)
        except ApiError as exc:
            if exc.status in (403, 404):
                task = None
            else:
                raise
        if task:
            if task.get("is_deleted"):
                return "deleted"
            if task.get("project_id") != self.config.get("todoist_project_id"):
                log.info("Task %s was moved to another project; treating it as removed", task_id)
                return "deleted"
            if task.get("checked"):
                return "completed"
            self._merge_todoist({"items": [task]})
            return "active"
        if "completed_ids" not in cache:
            cache["completed_ids"] = self.td.completed_ids(self.config["todoist_project_id"])
        return "completed" if task_id in cache["completed_ids"] else "deleted"

    # -- the sync cycle ---------------------------------------------------
    def sync(self) -> dict:
        with self.lock:
            try:
                res = self._sync_locked()
            except Exception as exc:  # noqa: BLE001
                side = getattr(exc, "_side", "")
                self.status = "setup" if isinstance(exc, NotConfigured) else "error"
                self.last_error = friendly_error(exc, side)
                if isinstance(exc, (NotConfigured, SafetyStop)):
                    log.warning("Sync stopped: %s", self.last_error)
                else:
                    log.warning("Sync failed: %s", self.last_error)
                self.save_state()
                return {"ok": False, "error": self.last_error, "conflicts": [], "changes": 0,
                        "queued": len(self.state["td_queue"]) + len(self.state["keep_queue"])}
            self.status, self.last_error = "ok", None
            self.last_sync = _dt.datetime.now()
            self.state["last_sync"] = self.last_sync.isoformat(timespec="seconds")
            self.save_state()
            return res

    def _side(self, fn, side):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001
            raise _tag(exc, side)

    def _sync_locked(self) -> dict:
        if not self.is_configured():
            raise NotConfigured("Choose a Keep list and a Todoist project in Settings")
        self._check_binding()
        note_id = self.config["keep_note_id"]
        project_id = self.config["todoist_project_id"]
        self._side(self._ensure_keep, "Google")
        self._side(self._ensure_todoist, "Todoist")

        # 1. Google Keep: pull (and push anything still pending in memory)
        keep_err = None
        try:
            self.keep.sync()
            self._keep_synced = True
            if self._keep_queue_applied:
                self.state["keep_queue"] = []
        except Exception as exc:  # noqa: BLE001
            keep_err = _tag(exc, "Google")
        # 2. Todoist: flush queued commands, pull changes
        self._side(self._todoist_sync, "Todoist")
        if keep_err is not None:
            raise keep_err
        if not self._keep_queue_applied:
            self._replay_keep_queue(note_id)
        self._fix_unmapped_adds()

        kitems = self.keep.items(note_id)
        if kitems is None:
            raise NotConfigured("The Keep list was not found (deleted or trashed?) - choose another list "
                                "in Settings. Nothing was deleted in Todoist.")
        if project_id not in self.state["todoist"]["projects"]:
            raise NotConfigured("The Todoist project was not found (deleted, archived or no access?) - "
                                "choose another project in Settings. Nothing was deleted in Keep.")
        sec = self.section_id()
        if sec and (self.state["todoist"]["sections"].get(sec) or {}).get("project_id") != project_id:
            raise NotConfigured("The Todoist section was not found (deleted, archived or moved?) - "
                                "choose another section in Settings. Nothing was deleted.")

        conflicts: list[str] = []
        changes = self._reconcile(note_id, project_id, kitems, conflicts)
        self.save_state()

        # 3. push (both sides are tried; edits that fail stay queued in state.json)
        push_err = None
        if self.state["keep_queue"]:
            try:
                self.keep.sync()
                self.state["keep_queue"] = []
            except Exception as exc:  # noqa: BLE001
                log.warning("Keep push failed; %d edit(s) queued", len(self.state["keep_queue"]))
                push_err = _tag(exc, "Google")
        if self.state["td_queue"]:
            try:
                self._todoist_sync()
                self._fix_unmapped_adds()
            except Exception as exc:  # noqa: BLE001
                log.warning("Todoist push failed; %d command(s) queued", len(self.state["td_queue"]))
                push_err = push_err or _tag(exc, "Todoist")
        if push_err is not None:
            raise push_err
        if changes:
            log.info("Sync done: %d change(s), %d conflict(s)", changes, len(conflicts))
        return {"ok": True, "error": None, "conflicts": conflicts, "changes": changes,
                "queued": len(self.state["td_queue"]) + len(self.state["keep_queue"])}

    # -- Keep op queue ----------------------------------------------------
    def _keep_op(self, op: dict) -> None:
        self._apply_keep_op(op)
        self.state["keep_queue"].append(op)
        self._keep_queue_applied = True

    def _apply_keep_op(self, op: dict) -> None:
        k, nid, iid = self.keep, op["note_id"], op["item_id"]
        if op["op"] == "add":
            k.add_item(nid, iid, op["text"], op["checked"])
        elif op["op"] == "set_text":
            k.set_text(nid, iid, op["text"])
        elif op["op"] == "set_checked":
            k.set_checked(nid, iid, op["checked"])
        elif op["op"] == "delete":
            k.delete_item(nid, iid)

    def _replay_keep_queue(self, note_id: str) -> None:
        """After a restart: re-apply edits whose push to Google never succeeded."""
        q = self.state["keep_queue"]
        if q:
            log.info("Re-applying %d queued Keep edit(s)", len(q))
            for op in q:
                try:
                    self._apply_keep_op(op)
                except Exception:  # noqa: BLE001
                    log.exception("Could not re-apply queued Keep edit")
            try:
                self.keep.sync()
                self.state["keep_queue"] = []
            except Exception as exc:  # noqa: BLE001
                raise _tag(exc, "Google")
        self._keep_queue_applied = True

    def _fix_unmapped_adds(self) -> None:
        """An item_add acknowledged without temp_id_mapping (a retried UUID): find the real task."""
        queued_temps = {c.get("temp_id") for c in self.state["td_queue"]}
        items = self.state["todoist"]["items"]
        mapped = {p["task_id"] for p in self.state["pairs"]}
        for p in self.state["pairs"]:
            tid = p["task_id"]
            if tid in items or tid in queued_temps or not _is_temp(tid):
                continue
            match = next((i for i, t in items.items()
                          if i not in mapped and norm(t["content"]) == norm(p["text"])), None)
            if match:
                p["task_id"] = match
                mapped.add(match)
                log.info("Matched a re-sent task to its Todoist id by text")
            else:  # the add never happened: send it again (new UUID, same temp id)
                self._td_cmd("item_add", self._add_args(p["text"].strip()), temp_id=tid)
                log.info("Re-sending a task add that Todoist did not confirm")

    # -- reconcile --------------------------------------------------------
    def _reconcile(self, note_id: str, project_id: str, kitems: dict, conflicts: list) -> int:
        titems = self.state["todoist"]["items"]
        pairs = self.state["pairs"]
        changes = 0
        cache: dict = {}
        queued_temps = {c.get("temp_id"): c for c in self.state["td_queue"] if c.get("temp_id")}

        # Resolve missing Todoist tasks first (completed vs deleted)
        status: dict[str, str] = {}
        for p in pairs:
            tid = p["task_id"]
            if tid in titems:
                status[tid] = "active"
            elif tid in queued_temps:
                status[tid] = "pending"
            else:
                status[tid] = self._resolve_missing(tid, p, cache)
                if status[tid] == "completed" and tid not in titems:
                    titems[tid] = {"content": p["text"], "checked": True, "updated_at": None,
                                   "completed_at": None, "recurring": False, "synthetic": True}

        # Section scope: keep linked tasks in the chosen section
        sec = self.section_id()
        old_sec = self.state.get("section")
        ignored = set(self.state.get("ignored_keep_ids") or [])
        if sec:
            moving = {c["args"].get("id") for c in self.state["td_queue"] if c["type"] == "item_move"}
            scoped = []
            for p in pairs:
                t = titems.get(p["task_id"])
                if t is None or t.get("synthetic") or t.get("checked") or t.get("section_id") == sec:
                    scoped.append(p)
                    continue
                if t.get("section_id") is None or t.get("section_id") == old_sec:
                    if p["task_id"] not in moving:
                        self._td_cmd("item_move", {"id": p["task_id"], "section_id": sec})
                    t["section_id"] = sec
                    log.info("Task %r moved into the chosen Todoist section", p["text"][:60])
                    changes += 1
                    scoped.append(p)
                else:  # lives in another named section: leave both sides alone, stop syncing it
                    ignored.add(p["keep_id"])
                    log.info("Task %r is in another Todoist section; unlinked (nothing moved or deleted)",
                             p["text"][:60])
                    changes += 1
            self.state["pairs"] = pairs = scoped
        self.state["section"] = sec
        ignored &= set(kitems)
        self.state["ignored_keep_ids"] = sorted(ignored)

        # Safety: one side suddenly empty -> do not mass-delete
        if len(pairs) >= SAFETY_MIN_PAIRS:
            if not any(p["keep_id"] in kitems for p in pairs):
                raise SafetyStop(f"Safety stop: all {len(pairs)} linked items are gone from the Keep list. "
                                 "Nothing was deleted in Todoist. If this is intended, use Settings > "
                                 "Re-link to start over.")
            if all(status.get(p["task_id"]) == "deleted" for p in pairs):
                raise SafetyStop(f"Safety stop: all {len(pairs)} linked tasks are gone from the Todoist "
                                 "project. Nothing was deleted in Keep. If this is intended, use Settings > "
                                 "Re-link to start over.")

        keep_pairs = []
        for p in pairs:
            kid, tid = p["keep_id"], p["task_id"]
            k = kitems.get(kid)
            st = status.get(tid)
            if st == "pending":
                cmd = queued_temps.get(tid)
                if k is None:
                    self.state["td_queue"] = [c for c in self.state["td_queue"]
                                              if c is not cmd and c["args"].get("id") != tid]
                    log.info("Keep item deleted before its task reached Todoist; add cancelled")
                    changes += 1
                    continue
                if cmd is not None and k["text"].strip() and k["text"] != p["text"]:
                    cmd["args"]["content"] = k["text"].strip()
                    p["text"] = k["text"]
                if bool(k["checked"]) != bool(p["checked"]):
                    self._td_cmd("item_close" if k["checked"] else "item_uncomplete", {"id": tid})
                    p["checked"] = bool(k["checked"])
                keep_pairs.append(p)
                continue
            t = titems.get(tid) if st in ("active", "completed") else None
            if k is None and t is None:
                log.info("Item removed on both sides")
                changes += 1
                continue
            if k is None:
                log.info("Keep item %r deleted -> deleting its Todoist task", p["text"][:60])
                if t.get("content") != p["text"] or bool(t.get("checked")) != bool(p["checked"]):
                    msg = f"Conflict: {p['text'][:60]!r} deleted in Keep but edited in Todoist; delete wins"
                    log.warning(msg)
                    conflicts.append(msg)
                self._td_cmd("item_delete", {"id": tid})
                titems.pop(tid, None)
                changes += 1
                continue
            if t is None:
                log.info("Todoist task %r deleted -> deleting its Keep item", p["text"][:60])
                if k["text"] != p["text"] or bool(k["checked"]) != bool(p["checked"]):
                    msg = f"Conflict: {p['text'][:60]!r} deleted in Todoist but edited in Keep; delete wins"
                    log.warning(msg)
                    conflicts.append(msg)
                self._keep_op({"op": "delete", "note_id": note_id, "item_id": kid})
                changes += 1
                continue
            changes += self._merge_pair(note_id, p, k, t, conflicts)
            keep_pairs.append(p)
        self.state["pairs"] = pairs = keep_pairs

        # New items on either side
        mapped_k = {p["keep_id"] for p in pairs}
        mapped_t = {p["task_id"] for p in pairs}
        new_t: dict[str, list[str]] = {}
        for tid, t in titems.items():
            if tid in mapped_t or t.get("synthetic") or t.get("checked"):
                continue
            if sec and t.get("section_id") != sec:
                continue
            new_t.setdefault(norm(t["content"]), []).append(tid)
        for kid, k in kitems.items():
            if kid in mapped_k or kid in ignored or not k["text"].strip():
                continue
            cands = new_t.get(norm(k["text"]))
            if cands:  # same item exists on both sides: link, no duplicate
                tid = cands.pop(0)
                p = {"keep_id": kid, "task_id": tid, "text": None, "checked": None}
                self._merge_pair(note_id, p, k, titems[tid], conflicts, first=True)
                pairs.append(p)
                log.info("Linked %r (same text on both sides)", k["text"][:60])
                changes += 1
                continue
            temp = str(uuid.uuid4())
            self._td_cmd("item_add", self._add_args(k["text"].strip()), temp_id=temp)
            if k["checked"]:
                self._td_cmd("item_close", {"id": temp})
            pairs.append({"keep_id": kid, "task_id": temp, "text": k["text"], "checked": bool(k["checked"])})
            log.info("Keep item %r added -> new Todoist task", k["text"][:60])
            changes += 1
        for tids in new_t.values():
            for tid in tids:
                t = titems[tid]
                kid = new_keep_item_id()
                self._keep_op({"op": "add", "note_id": note_id, "item_id": kid, "text": t["content"],
                               "checked": False})
                pairs.append({"keep_id": kid, "task_id": tid, "text": t["content"], "checked": False})
                log.info("Todoist task %r added -> new Keep item", t["content"][:60])
                changes += 1
        return changes

    def _merge_pair(self, note_id: str, p: dict, k: dict, t: dict, conflicts: list, first: bool = False) -> int:
        changes = 0
        kid, tid = p["keep_id"], p["task_id"]
        k_ts = float(k.get("updated") or 0)
        t_ts = max(to_ts(t.get("updated_at")), to_ts(t.get("completed_at")))
        label = (k["text"] or t.get("content") or "")[:60]

        def pick(field, base, kv, tv):
            if kv == tv:
                return kv
            kc, tc = (True, True) if first else (kv != base, tv != base)
            if kc and not tc:
                return kv
            if tc and not kc:
                return tv
            winner = "Keep" if k_ts >= t_ts else "Todoist"
            if not first:
                msg = (f"Conflict on {label!r} ({field}): changed in both apps; last write wins -> {winner}")
                log.warning(msg)
                conflicts.append(msg)
            return kv if winner == "Keep" else tv

        # text
        new_text = pick("text", p["text"], k["text"], t.get("content", ""))
        if not new_text.strip():
            new_text = t.get("content", "") or k["text"]
        if new_text.strip() != t.get("content"):
            self._td_cmd("item_update", {"id": tid, "content": new_text.strip()})
            t["content"] = new_text.strip()
            changes += 1
        if new_text != k["text"]:
            self._keep_op({"op": "set_text", "note_id": note_id, "item_id": kid, "text": new_text})
            changes += 1
        # checked
        kchk, tchk = bool(k["checked"]), bool(t.get("checked"))
        new_chk = pick("checked", p["checked"], kchk, tchk)
        if new_chk != tchk:
            if new_chk:
                self._td_cmd("item_close", {"id": tid})
                if t.get("recurring"):
                    log.info("Recurring task %r completed in Keep -> occurrence closed in Todoist; "
                             "Keep item unchecked again (Todoist rescheduled it)", label)
                    new_chk = False
                else:
                    t["checked"] = True
            else:
                self._td_cmd("item_uncomplete", {"id": tid})
                t["checked"] = False
                t.pop("synthetic", None)
            changes += 1
        if new_chk != kchk:
            self._keep_op({"op": "set_checked", "note_id": note_id, "item_id": kid, "checked": new_chk})
            changes += 1
        p["text"], p["checked"] = new_text, new_chk
        return changes


def _tag(exc: BaseException, side: str) -> BaseException:
    try:
        exc._side = side
    except Exception:  # noqa: BLE001
        pass
    return exc


def _is_temp(task_id: str) -> bool:
    try:
        uuid.UUID(task_id)
        return True
    except (ValueError, AttributeError, TypeError):
        return task_id.startswith("tmp-")


# --------------------------------------------------------------------------
# Background worker
# --------------------------------------------------------------------------
class SyncWorker(threading.Thread):
    """Runs every sync and every network job on one thread.

    ``callback(kind, payload)`` with kind in {"synced", "job"}.
    """

    def __init__(self, engine: BridgeEngine, callback, interval: float | None = None):
        super().__init__(name="BridgeSync", daemon=True)
        self.engine = engine
        self.callback = callback
        self.interval = max(15.0, float(interval or engine.config.get("interval") or DEFAULT_INTERVAL))
        self._wake = threading.Event()
        self._stop_evt = threading.Event()
        self._now = True
        self._next = 0.0
        self._jobs: list[tuple[str, Callable[[], Any]]] = []
        self._jobs_lock = threading.Lock()

    def set_interval(self, seconds: float) -> None:
        self.interval = max(15.0, float(seconds))
        self._next = time.monotonic() + self.interval
        self._wake.set()

    def sync_now(self) -> None:
        self._now = True
        self._wake.set()

    def run_job(self, name: str, fn: Callable[[], Any]) -> None:
        with self._jobs_lock:
            self._jobs.append((name, fn))
        self._wake.set()

    def stop(self) -> None:
        self._stop_evt.set()
        self._wake.set()

    def run(self) -> None:
        while not self._stop_evt.is_set():
            with self._jobs_lock:
                jobs, self._jobs = self._jobs, []
            for name, fn in jobs:
                try:
                    self.callback("job", {"name": name, "ok": True, "result": fn()})
                except Exception as exc:  # noqa: BLE001
                    log.warning("%s failed: %s", name, friendly_error(exc))
                    self.callback("job", {"name": name, "ok": False, "error": friendly_error(exc)})
            now = time.monotonic()
            if self._now or now >= self._next:
                self._now = False
                result = self.engine.sync()
                self._next = time.monotonic() + self.interval
                self.callback("synced", {"result": result, "status": self.engine.status,
                                         "error": self.engine.last_error, "last_sync": self.engine.last_sync})
                continue
            self._wake.wait(timeout=max(0.05, self._next - now))
            self._wake.clear()
