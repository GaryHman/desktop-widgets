# Keep-Todoist Bridge

A small Windows tray app that keeps **one Google Keep checklist** in **two-way sync** with **one
Todoist project**. The project can be personal or a team (workspace) project. Add, rename, check,
uncheck or delete an item on either side, and the other side follows within the sync interval
(60 seconds by default).

![settings](screenshot-settings.png)

*(The screenshot uses sample data from the test fakes.)*

> **Unofficial API warning.** Google Keep has no public API for personal accounts. This app
> uses [gkeepapi](https://github.com/kiwiz/gkeepapi), an *unofficial* library and the same one
> KeepNotes Widgets uses. Google does not support it and can change or block it at any time.
> When that happens, the tray icon gets a red badge, the status line shows the error, and
> nothing is deleted. Todoist uses its **official** API v1.

## Requirements

* Windows 10/11
* Python 3.11 or newer with the `py` launcher. To install it, run `winget install Python.Python.3.12`.
* A Google Keep account and a Todoist account. If **KeepNotes Widgets** and **Todoist Widgets**
  are already signed in on this PC, you don't need to log in again (see *Logins* below).

## Install

**Option 1 (recommended): `install.bat`.** Double-click it. It will:

1. create a private virtual environment in `.venv` next to the app,
2. install the pinned packages from `requirements.txt` (PySide6-Essentials, gkeepapi, gpsoauth,
   requests, keyring),
3. add a Start Menu shortcut called **Keep-Todoist Bridge**,
4. ask whether to start with Windows. `install.bat /startup` answers yes without asking, and
   `install.bat /quiet` skips the question.

**Option 2: your system Python.** Run `py -m pip install -r requirements.txt`, then start the app
with `run.bat`.

`run.bat` uses `.venv` if it is complete. Otherwise it uses a system Python that has the packages.
It starts the app with `pythonw`, so no console window stays open.

To uninstall, quit the app from the tray icon, then run `uninstall.bat`. It removes the shortcuts
and `.venv`. Your list, your project and the shared logins are not touched.

## Setup

1. Start **Keep-Todoist Bridge** from the Start Menu. The Settings window opens on first start.
   After that, left-click the tray icon or choose **Settings…** to open it.
2. **Accounts.** Both lines should say *Connected*. If one says *Not connected*, click
   **Connect Google Keep…** or **Connect Todoist…**. These are the same connect dialogs as in the
   widget apps: the Keep `oauth_token` cookie or a master token, and the Todoist API token from
   *Settings > Integrations > Developer*.
3. **Keep checklist.** Pick the list by its title. The app stores the note's ID, so renaming the
   list later is fine. **New list…** creates a new, empty Keep checklist.
4. **Todoist project.** Every project is listed, grouped as *Personal › …* or
   *Team: &lt;workspace&gt; › …*. **New project…** creates a project, either personal or inside
   one of your workspaces.
5. **Sync every** sets how often to sync (default 60 s, minimum 15 s). You can also turn on
   **Start with Windows** here or in the tray menu.
6. Click **Save** or **Sync now**.

The tray menu shows a status line, either **Synced HH:MM** or the current error, followed by
**Sync now**, **Settings…**, **Open log**, **Start with Windows** and **Quit**.

### Logins: reused, never stored in files

The app reads the Windows Credential Manager entries that the widget apps already created:

| Service | keyring service | keyring user name | Content |
|---|---|---|---|
| Google Keep | `KeepNotesWidgets` | `account-email` | your Google email |
| Google Keep | `KeepNotesWidgets` | *your Google email* | gkeepapi master token |
| Todoist | `TodoistWidgets` | `api-token` | Todoist API token |

It also reuses the Android device ID from `%APPDATA%\KeepNotesWidgets\config.json`, which the
Keep master token was issued for, and only reads that file. If you connect through this app's
dialogs, the shared entries are updated, so the widget apps use the new login too.

## How sync works

Files live in `%APPDATA%\KeepTodoistBridge\` and contain no secrets:

* `config.json`: the chosen Keep note ID, the Todoist project ID and the interval.
* `state.json`: the mapping *Keep item ID ↔ Todoist task ID*, plus the last-seen text and checked
  state of every linked item (the snapshot). It also holds the Todoist sync token, a small cache of
  the project's tasks, and the queues of edits that have not been delivered yet.
* `bridge.log`: what was synced, every conflict, and every error. It rotates at 512 KB.

Each cycle:

1. **Pull** both sides. Keep is pulled with gkeepapi. Todoist uses an incremental `/sync`, which
   also sends any queued commands first.
2. **Compare** each linked item with the snapshot. A field that changed on one side is copied to
   the other side.

   | Change | Keep → Todoist | Todoist → Keep |
   |---|---|---|
   | added | `item_add` in the project | new list item at the bottom |
   | renamed | `item_update` content | item text |
   | checked / completed | `item_close` | item checked |
   | unchecked / reopened | `item_uncomplete` | item unchecked |
   | deleted | `item_delete` (that one task) | that one item deleted |

3. **Push** the planned edits to both sides. The plan is saved to `state.json` before anything is
   sent.

**First run (or after *Start over…*).** Items are matched by text, case-insensitive with
surrounding spaces ignored. Matched items are linked without creating duplicates. Unmatched
Keep items become tasks, and unmatched open tasks become Keep items. When a matched pair differs,
for example one side is checked and the other is not, the newer timestamp wins.

**Conflicts.** If the same field changed on both sides since the last sync, **last write wins**:
the app compares Keep's item update time with Todoist's `updated_at` (or `completed_at`). Each
conflict is written to `bridge.log` and announced with a tray notification. A delete on one side
beats an edit on the other side, and that is logged too.

**Recurring Todoist tasks.** Checking one in Keep sends `item_close`, which closes only the
current occurrence. Todoist then moves the task to its next date, so the Keep item is unchecked
again.

**Completed vs. deleted.** Todoist's full sync leaves out completed tasks. When a linked task
disappears, the app checks it directly (`GET /tasks/{id}` and the project's recently completed
tasks) before it deletes anything in Keep. A task moved to another project counts as removed
from this list.

**Offline.** If either service can't be reached, the cycle stops and the tray shows
`offline (…)`. Nothing is lost: changes are found by comparing against the snapshot, so they are
picked up on the next successful cycle. Edits that were already planned are queued in
`state.json` and survive a restart. Every Todoist command has a fixed UUID, so a retry never runs
twice on Todoist's side. A task added during a lost connection is matched back instead of being
created twice.

**Safety rules**

* The app never deletes, trashes or archives the Keep note or the Todoist project. If either one
  disappears, sync stops with an error and the other side is left alone.
* If *all* linked items (3 or more) vanish from one side at once, sync stops with a **safety
  stop** and deletes nothing. If you emptied the list on purpose, use **Settings > Start over…**.
* When you switch to a different list or project, the app re-matches by text and deletes nothing.

### Todoist sections (1.1.0)

Settings has a **Section** dropdown under **Todoist project**. It lists the project's existing
sections (refreshed when you change the project or click **Refresh lists**), plus **(No section)**.
**New section…** creates one in the chosen project (a name is required; the bridge never creates
sections with an empty name).

* **(No section)** (the default, and what 1.0.0 did): the whole project syncs and new tasks are
  added without a section, so Todoist shows them at the top of the project under no heading.
* **A section**: only tasks in that section sync. New tasks are created in it. Linked tasks that
  have no section (or were in the previously chosen section) are moved into it with the Sync API
  `item_move` command (`item_update` cannot change a section). Linked tasks in your other sections
  are left where they are and stop syncing; their Keep items stay as they are. Nothing is deleted.
* The choice is saved as `todoist_section_id` in `config.json`; a missing value means no section.

## Limitations

* **Unofficial Keep API.** Google can break gkeepapi or require a new login at any time.
* Only the item **text** and **checked** state are synced. Todoist due dates, priorities,
  labels, descriptions, sections, assignees and comments are left alone in Todoist and do not
  appear in Keep.
* Hierarchy is flattened. Indented Keep items and Todoist sub-tasks are synced as plain items.
  Deleting a Todoist parent task also deletes its sub-tasks, and their Keep items follow.
* Item **order** is not synced. New items go to the bottom on the Keep side and to the project
  root on the Todoist side.
* Tasks that were already completed in Todoist before the first sync are not copied to Keep. Only
  open tasks are.
* Last-write-wins depends on the clocks of Google and Todoist. Two edits seconds apart on different
  devices can resolve either way, and every case is logged.
* Edits made while the app is not running are synced at the next start. If the same field was
  changed on both sides in the meantime, last-write-wins applies.
* When the safety stop fires because you really did empty the list, you have to choose
  **Start over…**. Afterwards the remaining items on the other side are copied back to the empty
  side instead of being deleted.
* Team projects: you need permission to add and complete tasks in that workspace project. A
  rejected command is logged and dropped.
* Empty Keep items are skipped until they have text.
* Requests are kept well inside Todoist's rate limits: one or two `/sync` calls per cycle.

## Tests

```
py -m pip install -r requirements-dev.txt
py -m pytest -q tests
```

The tests use an in-memory fake Keep server and a fake Todoist Sync API with incremental sync
tokens, temp IDs, UUID idempotency, recurring tasks and an offline mode. They cover the first-run
merge, add/rename/check/uncheck/delete in both directions, conflicts, the offline queue across a
restart, a lost response and the safety stops. One test exercises the real gkeepapi `Keep` object
without network access.

## Files

| File | Purpose |
|---|---|
| `keep_todoist_bridge.py` | tray app, Settings dialog, connect dialogs (PySide6) |
| `bridge_core.py` | sync engine, Todoist client, Keep adapter, worker thread (no Qt) |
| `install.bat` / `run.bat` / `uninstall.bat` / `shortcut.ps1` | install, start, remove, shortcuts |
| `requirements.txt` / `requirements-dev.txt` | pinned runtime packages / plus pytest |
| `tests/` | pytest suite with the fakes |
| `icon.ico` | tray and shortcut icon |
