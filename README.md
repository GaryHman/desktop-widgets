# desktop-widgets

Monorepo for Meneer's Windows desktop widgets, shipped together as one product with one installer.

| Folder | What it is |
|---|---|
| `TodoistWidgets/` | Desktop widgets for Todoist tasks |
| `KeepNotesWidgets/` | Desktop widgets for Google Keep notes |
| `Keep-Todoist Bridge/` | Sync bridge between Google Keep and Todoist |
| `build/` | Shared build kit (PyInstaller + Inno Setup scripts) |
| `install-widgets.bat` / `.ps1` | Combined installer entry point |

Build outputs, virtual environments, logs and any tokens or credentials are excluded via `.gitignore`.

## Previous repositories

This repo replaces the separate repos, which are archived (read-only):

- https://github.com/GaryHman/todoist-widgets
- https://github.com/GaryHman/keepnotes-widgets
