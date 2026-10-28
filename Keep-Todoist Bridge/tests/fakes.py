"""In-memory fakes for Google Keep and the Todoist Sync API (no network)."""
from __future__ import annotations

import copy
import datetime as _dt
import itertools

import requests

import bridge_core as bc


class Clock:
    def __init__(self, t: float = 1_760_000_000.0):
        self.t = t

    def tick(self, s: float = 10.0) -> float:
        self.t += s
        return self.t

    def iso(self) -> str:
        return _dt.datetime.fromtimestamp(self.t, _dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


# --------------------------------------------------------------------------
# Google Keep: a "server" plus a client with local (unsynced) edits
# --------------------------------------------------------------------------
class FakeKeepServer:
    def __init__(self, clock: Clock):
        self.clock = clock
        self.notes: dict[str, dict] = {}  # id -> {title, trashed, items: {id: {text, checked, updated, deleted}}}
        self.online = True
        self.syncs = 0
        self._ids = itertools.count(1)

    def create_list(self, title: str, items=()):
        nid = f"note{next(self._ids)}"
        self.notes[nid] = {"title": title, "trashed": False, "items": {}}
        for text, checked in items:
            self.user_add(nid, text, checked)
        return nid

    # edits made by the user in the Keep app
    def user_add(self, nid, text, checked=False):
        iid = f"k{next(self._ids)}"
        self.notes[nid]["items"][iid] = {"text": text, "checked": checked, "updated": self.clock.tick(1),
                                         "deleted": False}
        return iid

    def user_edit(self, nid, iid, **kw):
        self.notes[nid]["items"][iid].update(kw, updated=self.clock.tick(1))

    def user_delete(self, nid, iid):
        self.user_edit(nid, iid, deleted=True)

    def find(self, nid, text):
        for iid, it in self.notes[nid]["items"].items():
            if it["text"] == text and not it["deleted"]:
                return iid
        return None

    def visible(self, nid):
        return {it["text"]: it["checked"] for it in self.notes[nid]["items"].values() if not it["deleted"]}


class FakeKeep(bc.KeepAdapter):
    """Implements the KeepAdapter interface against FakeKeepServer."""

    def __init__(self, server: FakeKeepServer):
        self.server = server
        self.authenticated = False
        self.email = None
        self.local: dict | None = None  # copy of server data after the last sync
        self.dirty: list[tuple] = []

    def authenticate(self, email, master_token, device_id):
        assert master_token.startswith("aas_et/")
        self.authenticated = True
        self.email = email

    def sync(self):
        if not self.server.online:
            raise requests.ConnectionError("offline")
        self.server.syncs += 1
        for nid, iid, fields in self.dirty:  # push local edits (last write wins at the server)
            items = self.server.notes[nid]["items"]
            items.setdefault(iid, {"text": "", "checked": False, "deleted": False})
            items[iid].update(fields)
        self.dirty = []
        self.local = copy.deepcopy(self.server.notes)

    def _n(self, nid):
        n = (self.local or {}).get(nid)
        return None if n is None or n["trashed"] else n

    def checklists(self):
        return [{"id": nid, "title": n["title"], "archived": False}
                for nid, n in (self.local or {}).items() if not n["trashed"]]

    def items(self, nid):
        n = self._n(nid)
        if n is None:
            return None
        return {iid: {"text": it["text"], "checked": it["checked"], "updated": it["updated"]}
                for iid, it in n["items"].items() if not it["deleted"]}

    def title(self, nid):
        n = self._n(nid)
        return None if n is None else n["title"]

    def _edit(self, nid, iid, **fields):
        fields["updated"] = self.server.clock.tick(1)
        self._n(nid)["items"].setdefault(iid, {"text": "", "checked": False, "deleted": False}).update(fields)
        self.dirty.append((nid, iid, dict(fields)))

    def add_item(self, nid, iid, text, checked):
        it = self._n(nid)["items"].get(iid)
        if it is not None and not it["deleted"]:
            return
        self._edit(nid, iid, text=text, checked=checked, deleted=False)

    def set_text(self, nid, iid, text):
        it = self._n(nid)["items"].get(iid)
        if it and not it["deleted"] and it["text"] != text:
            self._edit(nid, iid, text=text)

    def set_checked(self, nid, iid, checked):
        it = self._n(nid)["items"].get(iid)
        if it and not it["deleted"] and it["checked"] != checked:
            self._edit(nid, iid, checked=checked)

    def delete_item(self, nid, iid):
        it = self._n(nid)["items"].get(iid)
        if it and not it["deleted"]:
            self._edit(nid, iid, deleted=True)

    def create_list(self, title):
        nid = self.server.create_list(title)
        return nid


# --------------------------------------------------------------------------
# Todoist Sync API v1 (incremental sync, temp ids, idempotent UUIDs)
# --------------------------------------------------------------------------
class FakeTodoist:
    def __init__(self, clock: Clock):
        self.clock = clock
        self.version = 0
        self.projects: dict[str, dict] = {}
        self.workspaces = {"ws1": {"id": "ws1", "name": "Angels Inc"}}
        self.items: dict[str, dict] = {}
        self.sections: dict[str, dict] = {}
        self.schanged: dict[str, int] = {}
        self.changed: dict[str, int] = {}  # id -> version
        self.pchanged: dict[str, int] = {}
        self.processed: set[str] = set()
        self.online = True
        self.lose_response = False  # process the next write, then drop the connection
        self.requests: list[list[dict]] = []
        self._ids = itertools.count(1000)

    def _id(self, prefix):
        return f"{prefix}{next(self._ids)}X"  # never a UUID

    def _touch(self, iid):
        self.version += 1
        self.changed[iid] = self.version
        self.items[iid]["updated_at"] = self.clock.iso()

    def add_project(self, name, workspace_id=None):
        pid = self._id("P")
        self.projects[pid] = {"id": pid, "name": name, "workspace_id": workspace_id, "is_deleted": False,
                              "is_archived": False}
        self.version += 1
        self.pchanged[pid] = self.version
        return pid

    def add_section(self, pid, name):
        sid = self._id("S")
        self.sections[sid] = {"id": sid, "project_id": pid, "name": name, "section_order": len(self.sections) + 1,
                              "is_deleted": False, "is_archived": False}
        self.version += 1
        self.schanged[sid] = self.version
        return sid

    # edits made by the user in the Todoist app
    def user_add(self, pid, content, recurring=False, section_id=None):
        iid = self._id("T")
        self.items[iid] = {"id": iid, "project_id": pid, "section_id": section_id, "content": content,
                           "checked": False,
                           "is_deleted": False, "completed_at": None,
                           "due": {"date": "2026-10-24", "is_recurring": True} if recurring else None}
        self.clock.tick(1)
        self._touch(iid)
        return iid

    def user_edit(self, iid, **kw):
        self.items[iid].update(kw)
        if kw.get("checked"):
            self.items[iid]["completed_at"] = self.clock.iso()
        self.clock.tick(1)
        self._touch(iid)

    def find(self, pid, content):
        for iid, it in self.items.items():
            if it["project_id"] == pid and it["content"] == content and not it["is_deleted"]:
                return iid
        return None

    def visible(self, pid):
        return {it["content"]: it["checked"] for it in self.items.values()
                if it["project_id"] == pid and not it["is_deleted"]}

    # -- API ------------------------------------------------------------
    def client(self, token):
        assert token == "t" * 40
        return FakeTodoistClient(self)

    def _close(self, it):
        if (it.get("due") or {}).get("is_recurring"):
            it["due"] = {"date": "2026-10-31", "is_recurring": True}  # next occurrence
        else:
            it["checked"] = True
            it["completed_at"] = self.clock.iso()

    def sync(self, token, commands, resource_types=None):
        if not self.online:
            raise requests.ConnectionError("offline")
        commands = commands or []
        self.requests.append(copy.deepcopy(commands))
        status, mapping, temps = {}, {}, {}
        for c in commands:
            if c["uuid"] in self.processed:
                status[c["uuid"]] = "ok"  # idempotent: not executed twice
                continue
            self.processed.add(c["uuid"])
            a = dict(c["args"])
            if "id" in a and a["id"] in temps:
                a["id"] = temps[a["id"]]
            t = c["type"]
            if t == "item_add":
                pid = temps.get(a["project_id"], a["project_id"])
                iid = self.user_add(pid, a["content"], section_id=a.get("section_id"))
                temps[c["temp_id"]] = mapping[c["temp_id"]] = iid
            elif t == "section_add":
                sid = self.add_section(a["project_id"], a["name"])
                temps[c["temp_id"]] = mapping[c["temp_id"]] = sid
            elif t == "project_add":
                pid = self.add_project(a["name"], a.get("workspace_id"))
                temps[c["temp_id"]] = mapping[c["temp_id"]] = pid
            elif a.get("id") not in self.items or self.items[a["id"]]["is_deleted"]:
                status[c["uuid"]] = {"error_tag": "ITEM_NOT_FOUND", "http_code": 400, "error": "Item not found"}
                continue
            else:
                it = self.items[a["id"]]
                if t == "item_update":
                    assert set(a) <= {"id", "content"}, a  # section_id cannot be changed by item_update
                    it["content"] = a["content"]
                elif t == "item_move":
                    it["section_id"] = a.get("section_id")
                elif t == "item_close":
                    self._close(it)
                elif t == "item_uncomplete":
                    it["checked"] = False
                    it["completed_at"] = None
                elif t == "item_delete":
                    it["is_deleted"] = True
                self.clock.tick(1)
                self._touch(a["id"])
            status[c["uuid"]] = "ok"
        full = token in ("*", None, "")
        since = 0 if full else int(token)
        if full:
            items = [copy.deepcopy(i) for i in self.items.values() if not i["is_deleted"] and not i["checked"]]
            projects = [copy.deepcopy(p) for p in self.projects.values()]
            sections = [copy.deepcopy(x) for x in self.sections.values() if not x["is_deleted"]]
        else:
            sections = [copy.deepcopy(self.sections[x]) for x, v in self.schanged.items() if v > since]
            items = [copy.deepcopy(self.items[i]) for i, v in self.changed.items() if v > since]
            projects = [copy.deepcopy(self.projects[p]) for p, v in self.pchanged.items() if v > since]
        resp = {"sync_token": str(self.version), "full_sync": full, "items": items, "projects": projects, "sections": sections,
                "workspaces": list(self.workspaces.values()) if full else [],
                "sync_status": status, "temp_id_mapping": mapping}
        if self.lose_response and commands:
            self.lose_response = False
            raise requests.ConnectionError("connection reset after the server processed the request")
        return resp


class FakeTodoistClient:
    def __init__(self, server: FakeTodoist):
        self.s = server

    def sync(self, sync_token, commands=None, resource_types=None):
        return self.s.sync(sync_token, commands, resource_types)

    def whoami(self):
        return {"id": "u1", "full_name": "Test User"}

    def get_task(self, task_id):
        if not self.s.online:
            raise requests.ConnectionError("offline")
        it = self.s.items.get(task_id)
        if it is None or it["is_deleted"] or it["checked"]:
            return None  # like the REST API: only active tasks
        return copy.deepcopy(it)

    def completed_ids(self, project_id, days=30):
        return {i for i, it in self.s.items.items()
                if it["project_id"] == project_id and it["checked"] and not it["is_deleted"]}
