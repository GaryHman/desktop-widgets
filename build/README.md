# Angels Inc. desktop apps - Windows build kit

Builds KeepNotes Widgets, Todoist Widgets and Keep-Todoist Bridge as real Windows programs
(PyInstaller .exe, own icon, version info, own taskbar identity) plus an Inno Setup installer each.

Requirements: Python 3.12 venv in `.buildvenv` with PyInstaller + the packages in each app's
requirements.txt, and Inno Setup 6 (`winget install JRSoftware.InnoSetup --scope user`).

    powershell -ExecutionPolicy Bypass -File build.ps1            # all apps
    powershell -ExecutionPolicy Bypass -File build.ps1 -Only keep # one app (keep, todoist, bridge)

Output: `dist\<App>\<App>.exe` and `installers\<App>-Setup-<version>.exe`.
Versions live in `apps.psd1`. Sources in `src\` (tests included; run them with pytest).

The installers install per user to `%LOCALAPPDATA%\Programs\<App Name>` (no admin needed),
add a Start Menu shortcut, optionally start with Windows, and register an uninstaller
(Settings > Apps) with icon, publisher and version. Settings and logins
(`%APPDATA%\<App>`, Windows Credential Manager) are never touched by install or uninstall.
The installers are not code-signed, so Windows SmartScreen shows "Windows protected your PC"
on first run (More info > Run anyway) until they are signed with a code-signing certificate.
