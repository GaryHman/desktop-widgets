# KeepNotes Widgets

Your Google Keep notes and checklists as small **Windows Sticky Notes-style widgets** on the
desktop, with one widget per note. Edits go both ways: changes you make in a widget are sent to Google
Keep, and changes made in Keep (phone, web) show up in the widgets.

![widgets](screenshot-widgets.png)

> **Unofficial API warning.** This app uses [gkeepapi](https://github.com/kiwiz/gkeepapi), an
> *unofficial* Google Keep API. Google does not support it and can change or block it at any
> time. If that happens, sync pauses (the tray icon gets an orange pause badge) and your cached
> notes stay visible and editable. Your edits are sent once sync works again.

## Requirements

* Windows 10/11
* Python 3.11 or newer with the `py` launcher. If you don't have Python yet, install it with
  `winget install Python.Python.3.12`.

## Install

**Option 1 (recommended): `install.bat`.** Double-click it. It will:

1. create a private virtual environment in `.venv` next to the app,
2. install the pinned packages from `requirements.txt` (PySide6-Essentials, gkeepapi, gpsoauth,
   keyring),
3. add a Start Menu shortcut called **KeepNotes Widgets**,
4. ask whether to start with Windows. Run `install.bat /startup` to say yes without being asked,
   or `install.bat /quiet` to skip the question.

**Option 2: your system Python.** Run this one line:

```
py -m pip install -r requirements.txt
```

Then start the app with `run.bat`.

`run.bat` uses `.venv` if it exists. Otherwise it uses the system Python, as long as that Python
can import the packages. It starts the app with `pythonw`, so no console window stays open.

## First start: connect your Google account

On first start, the **Connect Google Keep** window opens. You can reopen it later from the tray
icon: **Settings… > Sign in / switch account…**.

![setup](screenshot-setup.png)

Google no longer accepts a plain email + password login for this API, so the app uses the
documented gpsoauth "oauth_token" route:

1. Click **Open Google sign-in page**, or open <https://accounts.google.com/EmbeddedSetup> in
   Chrome or Edge.
2. Sign in with your Google account and click **I agree**. The page may keep spinning
   afterwards; that is normal.
3. Press **F12** to open the developer tools. Go to **Application > Cookies >
   https://accounts.google.com**. In Firefox, go to **Storage > Cookies**.
4. Find the cookie named **`oauth_token`** and copy its **value**. It starts with `oauth2_4/`.
5. In the app, enter your Gmail address, keep **oauth_token cookie** selected, paste the value
   and click **Sign in**. The cookie works only once and expires within minutes, so paste it
   straight away. If you get "BadAuthentication", repeat steps 1 to 5.

The app exchanges the cookie for a long-lived **master token** with
`gpsoauth.exchange_token(email, oauth_token, android_id)` and then logs in with
`keep.authenticate(email, master_token)`.

**Already have a master token?** It starts with `aas_et/`. Choose **Master token** and paste it.

**Where the login is stored:** your email and master token are saved only in **Windows Credential
Manager** (generic credential `KeepNotesWidgets`). They are never written to a file and never
logged. To remove them, use **Settings… > Sign out**, or delete the `KeepNotesWidgets` entries in
*Control Panel > Credential Manager > Windows Credentials*.

## Using the widgets

| Do this | How |
|---|---|
| Move a note | Drag the coloured strip at the top |
| Resize | Drag the grip in the bottom-right corner |
| Edit the title or text | Click and type. Changes are saved about 2 seconds after you stop typing |
| Tick a checklist item | Click its checkbox. Checked items move to the bottom with a strike-through |
| Add a checklist item | Type in **+ List item** and press Enter |
| Delete a checklist item | Click the small **✕** at the end of the item |
| New note | **+** on any note, or tray **New note** / **New list** |
| Colour, pin, always on top, opacity, lock | **⋯** menu on the note |
| Hide a note | **✕** in the header. It stays in Keep and keeps syncing. Bring it back with tray **Show / hide notes**, **Toggle last view** or the manager |
| Move to trash | **⋯ > Move to trash…** asks you to confirm first. You can restore the note from Keep's Trash |

Every widget is **always on top** by default. You can turn this off per note in the **⋯** menu.

### Tray icon menu

* **Status line:** shows "Synced 16:55" when all is well. Otherwise it shows "Sync paused: …
  showing cached notes" or "Not signed in".
* **Sync now**, **Show all notes**, **Hide all notes**, **Toggle all notes**
* **Show / hide notes** (a checklist of every note), **Layouts**, **Toggle last view**,
  **Notes & layouts…** (see below)
* **New note**, **New list**
* **Settings…:** choose which notes get a widget (all notes, pinned notes only, or notes with one
  label), set the sync interval, sign in, switch account or sign out, open the data folder,
  export or import settings, and open **About**.
* **Start with Windows:** adds or removes a shortcut in your Startup folder (`shell:startup`).
* **About…**, **Quit**

## Choose what is shown: notes, layouts and "Toggle last view"

You don't have to choose between all notes and none. Hidden notes keep syncing in the
background, so they are up to date the moment you show them again.

![manager](screenshot-manager.png)

**Show or hide single notes**

* **Tray icon > Show / hide notes** lists every note with a tick. Click one to show or hide it.
* **Tray icon > Notes & layouts…** opens the manager window. Tick or untick notes there,
  search by title, label or colour, or use **Show all**,
  **Hide all**, **Invert** and **Show only ▾**. Show only offers pinned notes, checklists, text notes, a label, a colour and the current search
  results. Double-click a note to bring it to the front.
* **✕** on a note hides just that note.

![tray](screenshot-tray.png)

**Toggle last view** (Ctrl+Alt+Shift+K by default, also in the tray menu and the manager) switches
between what is on screen now and what was on screen before your last change. That includes which
notes are shown, where they are and how big they are. Press it again to switch back. This
makes it a quick undo after hiding a note by accident, or a way to flip between two
arrangements. Several ticks in the manager within a few seconds count as one change.

**Toggle all notes** (Ctrl+Alt+K) now brings back exactly the notes that were shown
before you hid them, not every note. A click on the tray icon does the same. Use **Show all
notes** if you really want every note on screen.

**Saved layouts.** A layout stores which notes are shown plus each one's position, size,
always-on-top, opacity and lock setting.

| Do this | How |
|---|---|
| Save what is on screen | Manager **Save as new…**, or tray **Layouts > Save current layout as…** |
| Switch layout | Tray **Layouts > name**, double-click it in the manager, or its own hotkey |
| Change a layout | Arrange the notes, select the layout, click **Update** |
| Rename or delete | **Rename…** / **Delete…** in the manager. Deleting a layout removes only the layout, never your notes |
| Hotkey per layout | Select it, click **Hotkey…** and press for example Ctrl+Alt+1 |
| Start with a layout | Tick **Load the active layout when the app starts**. Untick it and the app reopens exactly as you left it (the default) |

When you load a layout, notes that are not in it are hidden. Notes that are new since you
saved the layout stay visible until you update the layout.

**Monitors.** If a monitor is unplugged or the resolution changes, any note that ends up off
screen is moved onto the nearest screen. The same happens when a layout or the last view is
loaded on a smaller setup.

**Per-note options (⋯ menu):** **Opacity** (100–50%) and **Lock position and size**, which
stops accidental dragging and hides the resize grip.


## Templates

**Settings > Template** changes the look of all notes immediately (the drop-down previews live;
Cancel undoes it). The note colours (from Keep or your own per-note choice) work in every template.

| Template | Look |
|---|---|
| **Classic Sticky** (default) | The original Windows Sticky Notes look |
| **Minimal Paper** | White paper, thin border, serif title, a thin colour line on top |
| **Dark Glass** | Dark, slightly see-through notes with rounded corners and a tinted header |
| **Pastel Rounded** | Large rounded corners and soft pastel colours |
| **Compact** | Smaller font and padding, for many notes on screen |

![templates](templates.png)

## Global hotkeys

Shortcuts that work in any program, set in **Settings > Global hotkeys** (click a field, press
the combination; the clear button removes it):

| Action | Default |
|---|---|
| Toggle all notes (hide if any is visible, otherwise bring back the notes that were shown) | **Ctrl+Alt+K** |
| Show all notes | none |
| Hide all notes | none |
| Toggle last view | **Ctrl+Alt+Shift+K** |
| Open the manager window | none |
| Load a saved layout | none; set one per layout in the manager (**Hotkey…**) |

A shortcut needs at least two modifiers (for example Ctrl+Alt+…), a Win key combination, or
F13–F24. If Windows or another program already uses it, Settings tells you so and the shortcut
stays inactive until you pick another one. Todoist Widgets uses Ctrl+Alt+T, so the two apps
do not clash.

## How sync works

* The app syncs in the background **every 60 seconds** (you can change this in Settings) and
  **about 2 seconds after each edit**.
* **Conflicts:** if the same note changed both here and in Keep since the last sync, the last
  write wins. The version from Google is kept, a warning goes to the log, and a tray
  notification appears.
* **Offline:** the full note state is cached in `%APPDATA%\KeepNotesWidgets\state.json`. When
  Google can't be reached, you keep seeing and editing the cached notes. Unsynced edits are kept,
  even across restarts, and are uploaded when the connection is back.
* **Colours:** Keep colours are mapped to the Sticky Notes palette. The default is yellow. A
  colour you pick in a widget is always remembered locally in `layout.json`, and the app also
  tries to send it to Keep. gkeepapi has a known issue where colour changes sometimes don't stick
  in Keep; the widget keeps your colour anyway.

## Files

| Where | What |
|---|---|
| `keep_widgets.py` | The app: widgets, tray and dialogs (PySide6) |
| `keep_sync.py` | The sync layer: gkeepapi, offline cache and conflict handling |
| `install.bat`, `run.bat`, `uninstall.bat`, `shortcut.ps1` | Install, start, uninstall and shortcut helpers |
| `win_hotkeys.py` | Global hotkeys (Win32 RegisterHotKey via ctypes, no extra packages) |
| `widget_layouts.py`, `widget_manager.py` | Visibility, saved layouts, toggle last view, monitor handling, the manager window (same files in Todoist Widgets) |
| `tests/`, `tools/make_screenshots.py` | Unit tests (a mocked Keep server) and the headless UI test |
| `%APPDATA%\KeepNotesWidgets\layout.json` | Position, size, visibility, on-top, opacity, lock and colour per note, saved layouts, the last view, plus settings |
| `%APPDATA%\KeepNotesWidgets\layout.json.*.bak` | Automatic backups made before an upgrade or an import |
| `%APPDATA%\KeepNotesWidgets\state.json` | Offline cache of your notes. No password or token |
| `%APPDATA%\KeepNotesWidgets\keepnotes.log` | Log file. Look here if sync pauses |

You can try the look without signing in by running `run.bat --demo`. This shows 4 sample notes
and syncs nothing.

## Uninstall

Quit the app from the tray, then run `uninstall.bat`. It removes the shortcuts and `.venv`. Your
notes are not touched: they live in Google Keep. To remove everything else, delete
`%APPDATA%\KeepNotesWidgets` and the `KeepNotesWidgets` entries in Credential Manager.

## Troubleshooting: nothing starts

If a start does nothing, check the log file in the data folder. Since this version a missing or
broken Python package shows a message box and writes `ERROR Startup failed` to the log instead
of exiting silently. A `.venv` folder left behind by an interrupted `install.bat` is skipped by
`run.bat` and the Start Menu shortcut; run `install.bat` again to repair it, or delete the `.venv`
folder to use your system Python.

## Limitations

* The Keep API is unofficial and may break without warning (see above).
* Images, drawings, reminders, collaborators and nested (indented) checklist items are not shown
  or edited in the widgets. They are left untouched in Keep.
* Archived and trashed notes are not shown.
* With many notes, "All notes" means many widgets. Hide the ones you don't need (tray **Show /
  hide notes** or the manager), save layouts, or use **Settings > Only pinned notes** or a label.

## Changelog

### 3.0.0 (2026-10-26)

* **Choose which notes are shown:** a tray checklist (**Show / hide notes**) and a new **Notes &
  layouts** manager with search and "Show only" (pinned, checklists, text notes, by label, by
  colour, search results). Hidden notes keep syncing.
* **Toggle last view** (Ctrl+Alt+Shift+K, tray, manager): switch between the current and the
  previous set of shown notes and their positions.
* **Saved layouts:** save, load, update, rename and delete named layouts. Each one can have its own
  global hotkey, and the app can load the active layout on start.
* **Toggle all notes** and a tray-icon click now bring back the notes you had shown, not every note.
* **Off-screen notes** are moved back onto a screen when monitors change.
* **Per-note opacity** and **Lock position and size** in the ⋯ menu.
* **Settings export / import** (no login data in the file; a backup is kept before importing).
* **About** window with the version, data folder and disclaimer. A one-time tray tip explains
  the new features.
* Global hotkeys: new actions **Toggle last view** and **Open the manager window**. A new default is
  only assigned if you don't already use that combination.
* `layout.json` is upgraded to version 2. The old file is copied to
  `layout.json.v1-<date>.bak` first. A file that can't be read is kept as
  `layout.json.corrupt-<date>.bak` instead of being overwritten.

