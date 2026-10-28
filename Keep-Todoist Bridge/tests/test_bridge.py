"""Unit tests for the Keep-Todoist Bridge sync engine with mocked Keep and Todoist."""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import bridge_core as bc  # noqa: E402
from fakes import Clock, FakeKeep, FakeKeepServer, FakeTodoist  # noqa: E402

TOKEN = "t" * 40
MASTER = "aas_et/" + "x" * 60


class World:
    def __init__(self, tmp_path, monkeypatch):
        monkeypatch.setenv("KNW_DATA_DIR", str(tmp_path / "knw"))
        self.tmp = tmp_path
        self.clock = Clock()
        self.ks = FakeKeepServer(self.clock)
        self.td = FakeTodoist(self.clock)
        self.note = self.ks.create_list("Groceries")
        self.project = self.td.add_project("Team shopping", workspace_id="ws1")
        self.engine = self.make_engine()

    def make_engine(self):
        e = bc.BridgeEngine(keep=FakeKeep(self.ks), keep_creds=bc.MemoryKeepCreds("me@gmail.com", MASTER),
                            todoist_creds=bc.MemoryTodoistCreds(TOKEN), todoist_factory=self.td.client,
                            directory=self.tmp / "data")
        e.set_binding(self.note, self.project)
        return e

    def sync(self, expect_ok=True):
        self.clock.tick(5)
        r = self.engine.sync()
        if expect_ok:
            assert r["ok"], r
        return r

    def keep(self):
        return self.ks.visible(self.note)

    def todo(self):
        return self.td.visible(self.project)

    def state(self):
        return json.loads((self.tmp / "data" / "state.json").read_text(encoding="utf-8"))


@pytest.fixture
def w(tmp_path, monkeypatch):
    return World(tmp_path, monkeypatch)


# ---------------------------------------------------------------- first run
def test_first_run_merge_by_text_without_duplicates(w):
    w.ks.user_add(w.note, "Milk")
    w.ks.user_add(w.note, "  eggs ")
    w.ks.user_add(w.note, "Bread", checked=True)
    w.td.user_add(w.project, "milk")
    w.td.user_add(w.project, "Eggs")
    w.td.user_add(w.project, "Coffee")
    w.sync()
    k, t = w.keep(), w.todo()
    assert len(k) == 4 and len(t) == 4, (k, t)
    assert {x.strip().casefold() for x in k} == {"milk", "eggs", "bread", "coffee"}
    assert {x.strip().casefold() for x in t} == {"milk", "eggs", "bread", "coffee"}
    assert t["Bread"] is True  # checked in Keep -> completed in Todoist
    assert k["Coffee"] is False
    assert len(w.state()["pairs"]) == 4
    # second run changes nothing
    before = len(w.td.requests)
    w.sync()
    assert w.keep() == k and len(w.todo()) == 4
    assert all(not r for r in w.td.requests[before:])  # no commands sent


def test_first_run_twice_same_text_pairs_one_to_one(w):
    w.ks.user_add(w.note, "Water")
    w.ks.user_add(w.note, "Water")
    w.td.user_add(w.project, "water")
    w.sync()
    assert sum(1 for it in w.td.items.values() if it["content"].casefold() == "water") == 2
    assert len(w.state()["pairs"]) == 2


def test_state_has_no_secrets(w):
    w.ks.user_add(w.note, "Milk")
    w.sync()
    raw = (w.tmp / "data" / "state.json").read_text() + (w.tmp / "data" / "config.json").read_text()
    assert TOKEN not in raw and MASTER not in raw


# ---------------------------------------------------------------- Keep -> Todoist
@pytest.fixture
def linked(w):
    w.ks.user_add(w.note, "Milk")
    w.td.user_add(w.project, "Eggs")
    w.sync()
    return w


def test_keep_add(linked):
    w = linked
    w.ks.user_add(w.note, "Butter")
    w.sync()
    assert w.todo()["Butter"] is False


def test_keep_rename(linked):
    w = linked
    w.ks.user_edit(w.note, w.ks.find(w.note, "Milk"), text="Oat milk")
    w.sync()
    assert "Oat milk" in w.todo() and "Milk" not in w.todo()


def test_keep_check_and_uncheck(linked):
    w = linked
    iid = w.ks.find(w.note, "Milk")
    w.ks.user_edit(w.note, iid, checked=True)
    w.sync()
    assert w.todo()["Milk"] is True
    w.ks.user_edit(w.note, iid, checked=False)
    w.sync()
    assert w.todo()["Milk"] is False


def test_keep_delete(linked):
    w = linked
    w.ks.user_delete(w.note, w.ks.find(w.note, "Milk"))
    w.sync()
    assert "Milk" not in w.todo()
    tid = next(i for i, it in w.td.items.items() if it["content"] == "Milk")
    assert w.td.items[tid]["is_deleted"] is True
    assert any(c["type"] == "item_delete" for req in w.td.requests for c in req)


# ---------------------------------------------------------------- Todoist -> Keep
def test_todoist_add(linked):
    w = linked
    w.td.user_add(w.project, "Cheese")
    w.sync()
    assert w.keep()["Cheese"] is False


def test_todoist_rename(linked):
    w = linked
    w.td.user_edit(w.td.find(w.project, "Eggs"), content="Free-range eggs")
    w.sync()
    assert "Free-range eggs" in w.keep() and "Eggs" not in w.keep()


def test_todoist_complete_and_reopen(linked):
    w = linked
    tid = w.td.find(w.project, "Eggs")
    w.td.user_edit(tid, checked=True)
    w.sync()
    assert w.keep()["Eggs"] is True
    w.td.user_edit(tid, checked=False, completed_at=None)
    w.sync()
    assert w.keep()["Eggs"] is False


def test_todoist_delete(linked):
    w = linked
    w.td.user_edit(w.td.find(w.project, "Eggs"), is_deleted=True)
    w.sync()
    assert "Eggs" not in w.keep()


def test_completed_task_after_full_resync_is_not_deleted(linked):
    w = linked
    w.td.user_edit(w.td.find(w.project, "Eggs"), checked=True)
    w.sync()
    w.engine.state["todoist"]["sync_token"] = "*"  # full sync drops completed tasks
    w.sync()
    assert w.keep()["Eggs"] is True
    assert "Eggs" in w.todo()


def test_task_completed_and_dropped_out_is_detected_as_completed(linked):
    w = linked
    tid = w.td.find(w.project, "Eggs")
    w.engine.state["todoist"]["sync_token"] = "*"
    w.td.items[tid]["checked"] = True  # completed, and absent from the next full sync
    w.sync()
    assert w.keep()["Eggs"] is True and "Eggs" in w.todo()


def test_recurring_task_completed_in_keep_closes_occurrence_and_stays_unchecked(w):
    tid = w.td.user_add(w.project, "Water plants", recurring=True)
    w.sync()
    w.ks.user_edit(w.note, w.ks.find(w.note, "Water plants"), checked=True)
    w.sync()
    it = w.td.items[tid]
    assert it["checked"] is False and it["due"]["date"] == "2026-10-31"  # rescheduled, not completed
    assert w.keep()["Water plants"] is False
    w.sync()
    assert w.keep()["Water plants"] is False and w.td.items[tid]["due"]["date"] == "2026-10-31"


# ---------------------------------------------------------------- conflicts
def test_conflict_last_write_wins_todoist_newer(linked, caplog):
    w = linked
    w.ks.user_edit(w.note, w.ks.find(w.note, "Milk"), text="Milk (Keep)")
    w.clock.tick(30)
    w.td.user_edit(w.td.find(w.project, "Milk"), content="Milk (Todoist)")
    with caplog.at_level("WARNING", logger="keeptodoistbridge"):
        r = w.sync()
    assert r["conflicts"] and "Todoist" in r["conflicts"][0]
    assert "Milk (Todoist)" in w.keep() and "Milk (Todoist)" in w.todo()
    assert "Conflict" in caplog.text


def test_conflict_last_write_wins_keep_newer(linked):
    w = linked
    w.td.user_edit(w.td.find(w.project, "Eggs"), content="Eggs (Todoist)")
    w.clock.tick(30)
    w.ks.user_edit(w.note, w.ks.find(w.note, "Eggs"), text="Eggs (Keep)")
    r = w.sync()
    assert r["conflicts"]
    assert "Eggs (Keep)" in w.keep() and "Eggs (Keep)" in w.todo()


def test_conflict_logged_in_bridge_log(linked, tmp_path):
    import logging
    w = linked
    h = logging.FileHandler(tmp_path / "bridge.log", encoding="utf-8")
    bc.log.addHandler(h)
    try:
        w.ks.user_edit(w.note, w.ks.find(w.note, "Milk"), checked=True)
        w.clock.tick(30)
        w.td.user_edit(w.td.find(w.project, "Milk"), content="Milk 2L")
        w.ks.user_edit(w.note, w.ks.find(w.note, "Milk"), text="Milk 1L")
        w.sync()
    finally:
        bc.log.removeHandler(h)
        h.close()
    assert "Conflict" in (tmp_path / "bridge.log").read_text()
    assert "Milk 1L" in w.todo() and w.todo()["Milk 1L"] is True


# ---------------------------------------------------------------- offline / queue
def test_todoist_offline_then_back(linked):
    w = linked
    w.td.online = False
    w.ks.user_add(w.note, "Tea")
    r = w.sync(expect_ok=False)
    assert "offline" in r["error"] and "Todoist" in r["error"]
    assert w.engine.status == "error"
    w.td.online = True
    w.sync()
    assert w.todo()["Tea"] is False


def test_keep_offline_then_back(linked):
    w = linked
    w.ks.online = False
    w.td.user_add(w.project, "Jam")
    r = w.sync(expect_ok=False)
    assert "offline" in r["error"]
    w.ks.online = True
    w.sync()
    assert w.keep()["Jam"] is False


def test_lost_response_is_retried_with_same_uuid_and_no_duplicates(linked):
    w = linked
    w.ks.user_add(w.note, "Honey")
    w.td.lose_response = True  # processed by Todoist, answer never arrives
    r = w.sync(expect_ok=False)
    assert r["queued"] >= 1
    queued = [c["uuid"] for c in w.state()["td_queue"]]
    assert queued
    w.sync()
    assert sum(1 for it in w.td.items.values() if it["content"] == "Honey") == 1
    assert w.state()["td_queue"] == []
    resent = [c["uuid"] for req in w.td.requests for c in req]
    assert all(resent.count(u) == 2 for u in queued)  # same UUID sent twice, executed once
    assert all(not bc._is_temp(p["task_id"]) for p in w.state()["pairs"])


def test_queue_survives_restart(linked):
    w = linked
    w.ks.user_edit(w.note, w.ks.find(w.note, "Milk"), text="Soy milk")
    w.td.user_add(w.project, "Rice")
    # Both pushes fail after the plan was made: Todoist answers the read, then goes away
    orig = w.td.sync
    calls = {"n": 0}

    def flaky(token, commands, resource_types=None):
        calls["n"] += 1
        if calls["n"] == 2:
            raise bc.ApiError("Todoist returned HTTP 503", 503)
        return orig(token, commands, resource_types)

    w.td.sync = flaky
    w.ks_online_after_read = True
    real_sync = FakeKeep.sync
    state = {"n": 0}

    def keep_flaky(self):
        state["n"] += 1
        if state["n"] == 2:
            raise ConnectionError("offline")
        return real_sync(self)

    FakeKeep.sync = keep_flaky
    try:
        r = w.sync(expect_ok=False)
    finally:
        FakeKeep.sync = real_sync
        w.td.sync = orig
    st = w.state()
    assert st["td_queue"] and st["keep_queue"]
    assert "Soy milk" not in w.todo() and "Rice" not in w.keep()
    # restart: a new engine loads the queues from state.json
    w.engine = w.make_engine()
    w.sync()
    assert "Soy milk" in w.todo() and "Rice" in w.keep()
    assert w.state()["td_queue"] == [] and w.state()["keep_queue"] == []
    assert sum(1 for t in w.todo() if t == "Rice") == 1 and len(w.keep()) == 3


# ---------------------------------------------------------------- safety
def test_never_deletes_when_keep_list_is_gone(linked):
    w = linked
    w.ks.notes[w.note]["trashed"] = True
    r = w.sync(expect_ok=False)
    assert "not found" in r["error"]
    assert len(w.todo()) == 2
    assert not any(c["type"] == "item_delete" for req in w.td.requests for c in req)


def test_never_deletes_when_project_is_gone(linked):
    w = linked
    w.td.projects[w.project]["is_deleted"] = True
    w.td.version += 1
    w.td.pchanged[w.project] = w.td.version
    r = w.sync(expect_ok=False)
    assert "not found" in r["error"]
    assert len(w.keep()) == 2


def test_safety_stop_when_whole_list_emptied(w):
    for name in ("A", "B", "C"):
        w.ks.user_add(w.note, name)
    w.sync()
    for name in ("A", "B", "C"):
        w.ks.user_delete(w.note, w.ks.find(w.note, name))
    r = w.sync(expect_ok=False)
    assert "Safety stop" in r["error"]
    assert len(w.todo()) == 3


def test_changing_the_pair_resets_mapping_without_deleting(linked):
    w = linked
    other = w.ks.create_list("Other")
    w.engine.set_binding(other, w.project)
    w.sync()
    assert w.ks.visible(other) == {"Milk": False, "Eggs": False}
    assert len(w.todo()) == 2  # matched by text, no duplicates


# ---------------------------------------------------------------- sources / creation
def test_refresh_sources_lists_checklists_and_team_projects(w):
    w.td.add_project("Inbox")
    src = w.engine.refresh_sources()
    assert not src["errors"]
    assert any(n["title"] == "Groceries" for n in src["keep"])
    team = [p for p in src["todoist"] if p["id"] == w.project][0]
    assert team["group"] == "Team: Angels Inc"


def test_create_project_and_list(w):
    pid = w.engine.create_todoist_project("New shared", workspace_id="ws1")
    assert w.td.projects[pid]["workspace_id"] == "ws1"
    nid = w.engine.create_keep_list("New list")
    assert w.ks.notes[nid]["title"] == "New list"


def test_missing_credentials_reported(tmp_path, monkeypatch):
    monkeypatch.setenv("KNW_DATA_DIR", str(tmp_path / "knw"))
    clock = Clock()
    e = bc.BridgeEngine(keep=FakeKeep(FakeKeepServer(clock)), keep_creds=bc.MemoryKeepCreds(),
                        todoist_creds=bc.MemoryTodoistCreds(), todoist_factory=FakeTodoist(clock).client,
                        directory=tmp_path / "d")
    e.set_binding("n", "p")
    r = e.sync()
    assert not r["ok"] and "Google Keep is not connected" in r["error"] and e.status == "setup"


def test_reuses_keepnotes_widgets_android_id(tmp_path, monkeypatch):
    knw = tmp_path / "knw"
    knw.mkdir()
    (knw / "config.json").write_text('{"android_id": "abcdef0123456789"}')
    monkeypatch.setenv("KNW_DATA_DIR", str(knw))
    e = bc.BridgeEngine(keep=FakeKeep(FakeKeepServer(Clock())), keep_creds=bc.MemoryKeepCreds(),
                        todoist_creds=bc.MemoryTodoistCreds(), directory=tmp_path / "d")
    assert e.android_id() == "abcdef0123456789"


# ---------------------------------------------------------------- real gkeepapi adapter (no network)
def test_real_keep_adapter_item_operations():
    import gkeepapi
    keep = gkeepapi.Keep()
    a = bc.KeepAdapter(keep=keep)
    nid = keep.createList("Real list", [("One", False)]).id
    assert [c["title"] for c in a.checklists()] == ["Real list"]
    a.add_item(nid, "abc.0000000000000001", "Two", True)
    a.add_item(nid, "abc.0000000000000001", "Two", True)  # idempotent replay
    items = a.items(nid)
    assert sorted((v["text"], v["checked"]) for v in items.values()) == [("One", False), ("Two", True)]
    a.set_text(nid, "abc.0000000000000001", "Two!")
    a.set_checked(nid, "abc.0000000000000001", False)
    assert a.items(nid)["abc.0000000000000001"] == {**a.items(nid)["abc.0000000000000001"],
                                                    "text": "Two!", "checked": False}
    a.delete_item(nid, "abc.0000000000000001")
    assert list(v["text"] for v in a.items(nid).values()) == ["One"]
    keep.get(nid).trash()
    assert a.items(nid) is None


def test_real_todoist_client_hides_token_and_maps_errors():
    class R:
        def __init__(self, code, body=b"{}"):
            self.status_code, self.content = code, body

        def json(self):
            return json.loads(self.content)

    class S:
        def __init__(self, code):
            self.code, self.calls = code, []

        def request(self, method, url, **kw):
            self.calls.append((method, url, kw))
            return R(self.code)

    c = bc.TodoistClient("secret" * 7, session=S(401))
    assert "secret" not in repr(c)
    with pytest.raises(bc.TodoistAuthError):
        c.sync("*")
    assert bc.TodoistClient("x" * 40, session=S(404)).get_task("1") is None
    s = S(200)
    bc.TodoistClient("x" * 40, session=s).sync("tok", [{"type": "item_add"}])
    method, url, kw = s.calls[0]
    assert url.endswith("/api/v1/sync") and json.loads(kw["data"]["commands"])[0]["type"] == "item_add"


# ---------------------------------------------------------------- sections (1.1.0)
def _section_of(w, text):
    return w.td.items[w.td.find(w.project, text)].get("section_id")


def test_no_section_keeps_1_0_behaviour(w):
    w.ks.user_add(w.note, "Milk")
    w.sync()
    assert _section_of(w, "Milk") is None
    assert all("section_id" not in c["args"] for r in w.td.requests for c in r if c["type"] == "item_add")


def test_new_tasks_go_into_chosen_section(w):
    shop = w.td.add_section(w.project, "Shopping")
    w.sync()
    w.engine.set_binding(w.note, w.project, section_id=shop, section_name="Shopping")
    w.ks.user_add(w.note, "Milk")
    w.sync()
    assert _section_of(w, "Milk") == shop
    assert w.engine.sections_by_project()[w.project][0]["name"] == "Shopping"


def test_choosing_section_moves_unsectioned_and_leaves_other_sections(w):
    shop = w.td.add_section(w.project, "Shopping")
    other = w.td.add_section(w.project, "Work")
    w.ks.user_add(w.note, "Milk")                      # -> task without section
    w.td.user_add(w.project, "Report", section_id=other)  # -> copied to Keep in whole-project mode
    w.sync()
    assert _section_of(w, "Milk") is None and "Report" in w.keep()
    w.engine.set_binding(w.note, w.project, section_id=shop, section_name="Shopping")
    w.sync()
    w.sync()
    assert _section_of(w, "Milk") == shop        # moved with item_move
    assert _section_of(w, "Report") == other     # left alone
    assert "Report" in w.keep() and len(w.todo()) == 2  # nothing deleted, no duplicate
    assert len(w.state()["pairs"]) == 1
    # edits on the unlinked item no longer sync, and it is not re-added
    w.ks.user_edit(w.note, w.ks.find(w.note, "Report"), text="Report v2")
    w.sync()
    assert "Report v2" not in w.todo() and len(w.todo()) == 2


def test_section_only_pulls_tasks_from_that_section(w):
    shop = w.td.add_section(w.project, "Shopping")
    w.engine.set_binding(w.note, w.project, section_id=shop)
    w.td.user_add(w.project, "Eggs", section_id=shop)
    w.td.user_add(w.project, "Loose task")
    w.sync()
    assert set(w.keep()) == {"Eggs"}


def test_switching_section_moves_linked_tasks(w):
    a = w.td.add_section(w.project, "A")
    b = w.td.add_section(w.project, "B")
    w.engine.set_binding(w.note, w.project, section_id=a)
    w.ks.user_add(w.note, "Milk")
    w.sync()
    assert _section_of(w, "Milk") == a
    w.engine.set_binding(w.note, w.project, section_id=b)
    w.sync()
    assert _section_of(w, "Milk") == b and len(w.todo()) == 1


def test_missing_section_stops_sync(w):
    w.engine.set_binding(w.note, w.project, section_id="nope")
    r = w.sync(expect_ok=False)
    assert not r["ok"] and "section was not found" in r["error"]


def test_create_section_rejects_empty_name(w):
    with pytest.raises(bc.ApiError):
        w.engine.create_todoist_section("   ", w.project)
    sid = w.engine.create_todoist_section("Groceries", w.project)
    assert w.td.sections[sid]["name"] == "Groceries"


def test_state_from_1_0_gets_sections_with_full_sync(w):
    w.td.add_section(w.project, "Shopping")
    w.sync()
    st = w.state()
    del st["todoist"]["sections"]
    st.pop("section", None)
    st.pop("ignored_keep_ids", None)
    (w.tmp / "data" / "state.json").write_text(json.dumps(st), encoding="utf-8")
    e = w.make_engine()
    assert e.state["todoist"]["sync_token"] == "*"
    w.engine = e
    w.sync()
    assert [s["name"] for s in e.sections_by_project()[w.project]] == ["Shopping"]
