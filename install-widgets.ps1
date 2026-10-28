# install-widgets.ps1 - sets up KeepNotes Widgets, Todoist Widgets, Home Panel and Process Radar on THIS PC.
# Started by install-widgets.bat (same folder). The app folders are found relative to this file (Widgets\, Software\, Skills & Tools\), so it
# works on any PC that has the shared opencode-state folder, whatever the user name or drive.
#
#   install-widgets.bat                 normal install (asks questions)
#   install-widgets.bat --dry-run       only show what would happen, change nothing
#   install-widgets.bat --startup=no    do not ask about "Start with Windows" (answer No for all)
#
# What it does: find Python 3.11+ (offers winget install), pip install --user each app's
# requirements into that Python (no .venv in the shared folder), create Start Menu shortcuts
# (pythonw.exe + script, app icon), optionally "Start with Windows" shortcuts (the same Startup
# shortcut the apps' tray menu uses). It never copies tokens, logins or settings.
$ErrorActionPreference = 'Continue'
$DryRun = $false
$StartupMode = 'ask'
$TestLnkDir = $null
foreach ($a in $args) {
    switch -Regex ($a) {
        '^(--|/|-)dry-?run$' { $DryRun = $true }
        '^(--|/|-)startup=(ask|yes|no)$' { $StartupMode = $Matches[2] }
        '^--test-shortcut-dir=(.+)$' { $TestLnkDir = $Matches[1] }   # testing only: shortcuts go there
        default { Write-Host "Unknown option: $a" }
    }
}
$Root = $PSScriptRoot
$LogFile = Join-Path $env:TEMP 'install-widgets.log'
$MinPy = [version]'3.11'

function Log([string]$msg = '', [string]$color = '') {
    if ($color) { Write-Host $msg -ForegroundColor $color } else { Write-Host $msg }
    try { Add-Content -LiteralPath $LogFile -Value $msg -Encoding UTF8 } catch { }
}
function Run-Native([string]$exe, [string[]]$argv) {
    # runs a console program, shows + logs its output, returns the exit code
    Log ("> `"$exe`" " + ($argv -join ' '))
    & $exe @argv 2>&1 | ForEach-Object { Log ("  " + "$_") }
    return $LASTEXITCODE
}
function Ask-YesNo([string]$question) {
    $ans = Read-Host "$question [y/N]"
    Log "$question [y/N] -> '$ans'"
    return ($ans -match '^(y|yes|j|ja)$')
}

Add-Content -LiteralPath $LogFile -Value "`r`n===== install-widgets $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') =====" -Encoding UTF8
Log "=== Widgets installer ===" 'Cyan'
Log "Shared folder : $Root"
Log "This PC       : $env:COMPUTERNAME, user $env:USERNAME"
Log "Log file      : $LogFile"
if ($DryRun) { Log "DRY RUN: nothing will be installed, created or changed." 'Yellow' }
Log ""

$Apps = @(
    [pscustomobject]@{ Name = 'KeepNotes Widgets'; Folder = 'KeepNotesWidgets'; Script = 'keep_widgets.py'
        Imports = 'gkeepapi, gpsoauth, PySide6, keyring'; Desc = 'KeepNotes Widgets - Google Keep notes on your desktop'
        SignIn = 'KeepNotes Widgets: the setup window opens on first start; sign in to Google Keep again (master token or OAuth token).' },
    [pscustomobject]@{ Name = 'Todoist Widgets'; Folder = 'TodoistWidgets'; Script = 'todoist_widgets.py'
        Imports = 'requests, PySide6, keyring'; Desc = 'Todoist Widgets - Todoist projects on your desktop'
        SignIn = 'Todoist Widgets: paste your Todoist API token again (Todoist > Settings > Integrations > Developer).' },
    [pscustomobject]@{ Name = 'Home Panel'; Folder = 'HomePanel'; Script = 'home_panel.py'
        Imports = 'PySide6'; Desc = 'Home Panel - Google Home on your desktop'
        SignIn = 'Home Panel: sign in to Google once inside the panel window (it uses its own Edge/Chrome profile on each PC).' },
    [pscustomobject]@{ Name = 'Process Radar'; Folder = 'ProcessRadar'; Script = 'process_radar.py'
        Imports = 'PySide6.QtWidgets, psutil'; Desc = 'Process Radar - see what is running in the background'
        NoStartup = $true
        SignIn = 'Process Radar: nothing to sign in to. Ctrl+Alt+P shows/hides it (change under Settings if you type AltGr+P).' }
)

# ---------------------------------------------------------------- 1. app folders
Log "[1/6] App folders" 'Cyan'
$Found = @()
foreach ($app in $Apps) {
    $dir = $null; foreach ($base in @($Root, (Split-Path -Parent $Root), (Join-Path (Split-Path -Parent (Split-Path -Parent $Root)) 'Skills & Tools'))) { $c = Join-Path $base $app.Folder; if (Test-Path -LiteralPath (Join-Path $c $app.Script)) { $dir = $c; break } }; if (-not $dir) { $dir = Join-Path $Root $app.Folder }   # Widgets\, Software\ or Skills & Tools\
    $script = Join-Path $dir $app.Script
    if (Test-Path -LiteralPath $script) {
        $app | Add-Member -NotePropertyName Dir -NotePropertyValue $dir
        $app | Add-Member -NotePropertyName ScriptPath -NotePropertyValue $script
        $Found += $app
        Log "  OK   $($app.Name): $dir"
    } else {
        Log "  MISSING  $($app.Name): $script not found - skipped" 'Yellow'
    }
}
if (-not $Found) { Log "No app folders found next to this file. Nothing to do." 'Red'; exit 1 }

# ---------------------------------------------------------------- 2. Python
function Get-PyInfo([string]$exe, [string[]]$pre = @()) {
    try {
        $out = & $exe @pre -c "import sys; print(sys.executable); print('%d.%d.%d' % sys.version_info[:3])" 2>$null
        if ($LASTEXITCODE -ne 0 -or -not $out -or $out.Count -lt 2) { return $null }
        $pyexe = "$($out[0])".Trim()
        if ($pyexe -like '*\WindowsApps\*') { return $null }   # Microsoft Store alias, not a real Python
        $pyw = Join-Path (Split-Path -Parent $pyexe) 'pythonw.exe'
        return [pscustomobject]@{ Exe = $pyexe; Pythonw = $pyw; Version = [version]"$($out[1])".Trim() }
    } catch { return $null }
}
function Find-Python {
    $cands = @()
    $pyl = Get-Command py -ErrorAction SilentlyContinue
    if (-not $pyl) {
        foreach ($p in "$env:LOCALAPPDATA\Programs\Python\Launcher\py.exe", "$env:WINDIR\py.exe") {
            if (Test-Path -LiteralPath $p) { $pyl = Get-Item -LiteralPath $p; break }
        }
    }
    if ($pyl) {
        $exe = if ($pyl.Source) { $pyl.Source } else { $pyl.FullName }
        $i = Get-PyInfo $exe @('-3'); if ($i) { $i | Add-Member -NotePropertyName Via -NotePropertyValue "py launcher ($exe)"; $cands += $i }
    }
    foreach ($c in @(Get-Command python -All -ErrorAction SilentlyContinue)) {
        $i = Get-PyInfo $c.Source; if ($i) { $i | Add-Member -NotePropertyName Via -NotePropertyValue "PATH ($($c.Source))"; $cands += $i }
    }
    $globs = @("$env:LOCALAPPDATA\Programs\Python\Python3*\python.exe", "$env:SystemDrive\Python3*\python.exe",
               "$env:ProgramFiles\Python3*\python.exe")
    foreach ($g in $globs) {
        foreach ($f in @(Get-ChildItem -Path $g -ErrorAction SilentlyContinue)) {
            $i = Get-PyInfo $f.FullName; if ($i) { $i | Add-Member -NotePropertyName Via -NotePropertyValue "found at $($f.FullName)"; $cands += $i }
        }
    }
    $seen = @{}; $uniq = @()
    foreach ($c in $cands) { $k = $c.Exe.ToLower(); if (-not $seen[$k]) { $seen[$k] = 1; $uniq += $c } }
    return $uniq
}
Log ""
Log "[2/6] Python 3.11 or newer" 'Cyan'
$all = @(Find-Python)
foreach ($c in $all) { Log ("  found Python {0}  {1}  via {2}" -f $c.Version, $c.Exe, $c.Via) }
# prefer the py launcher's default if new enough, else the newest suitable one
$Py = $all | Where-Object { $_.Version -ge $MinPy -and (Test-Path -LiteralPath $_.Pythonw) } | Select-Object -First 1
$newest = $all | Where-Object { $_.Version -ge $MinPy -and (Test-Path -LiteralPath $_.Pythonw) } | Sort-Object Version -Descending | Select-Object -First 1
if ($Py -and $Py.Via -notlike 'py launcher*' -and $newest) { $Py = $newest }
if (-not $Py) {
    Log "  No Python $MinPy+ found." 'Yellow'
    $winget = Get-Command winget -ErrorAction SilentlyContinue
    if ($DryRun) {
        Log "  (dry run) would offer: winget install -e --id Python.Python.3.14 --scope user"
    } elseif (-not $winget) {
        Log "  winget is not available. Install Python 3.14 from https://www.python.org/downloads/windows/" 'Red'
        Log "  (tick 'Add python.exe to PATH' and the py launcher), then run install-widgets.bat again." 'Red'
        exit 1
    } elseif (Ask-YesNo "  Install Python 3.14 now with winget (python.org, for this user only)?") {
        $rc = Run-Native $winget.Source @('install', '-e', '--id', 'Python.Python.3.14', '--scope', 'user',
                                          '--accept-package-agreements', '--accept-source-agreements')
        if ($rc -ne 0) {
            Log "  winget returned $rc - trying Python.Python.3.13 ..." 'Yellow'
            $rc = Run-Native $winget.Source @('install', '-e', '--id', 'Python.Python.3.13', '--scope', 'user',
                                              '--accept-package-agreements', '--accept-source-agreements')
        }
        $env:Path = [Environment]::GetEnvironmentVariable('Path', 'Machine') + ';' + [Environment]::GetEnvironmentVariable('Path', 'User')
        $all = @(Find-Python)
        $Py = $all | Where-Object { $_.Version -ge $MinPy -and (Test-Path -LiteralPath $_.Pythonw) } | Sort-Object Version -Descending | Select-Object -First 1
        if (-not $Py) { Log "  Python still not found after winget (exit code $rc). Close this window and run install-widgets.bat again." 'Red'; exit 1 }
    } else {
        Log "  Cancelled: Python is required." 'Red'; exit 1
    }
}
if ($Py) { Log ("  USING Python {0}: {1}" -f $Py.Version, $Py.Exe) 'Green'; Log "        pythonw : $($Py.Pythonw)" }

# ---------------------------------------------------------------- 3. packages
Log ""
Log "[3/6] Python packages (pip install --user, no .venv in the shared folder)" 'Cyan'
$PipFailed = @()
if ($Py) {
    & $Py.Exe -m pip --version *> $null
    if ($LASTEXITCODE -ne 0) {
        if ($DryRun) { Log "  (dry run) pip is missing - would run: python -m ensurepip --user" }
        else { [void](Run-Native $Py.Exe @('-m', 'ensurepip', '--user', '--upgrade')) }
    }
    foreach ($app in $Found) {
        $req = Join-Path $app.Dir 'requirements.txt'
        Log "  -- $($app.Name)"
        if (-not (Test-Path -LiteralPath $req)) { Log "     no requirements.txt - skipped" 'Yellow'; continue }
        $pipArgs = @('-m', 'pip', 'install', '--user', '--disable-pip-version-check', '-r', $req)
        if ($DryRun) { $pipArgs += '--dry-run' }
        $rc = Run-Native $Py.Exe $pipArgs
        if ($rc -ne 0) { $PipFailed += $app.Name; Log "     pip FAILED for $($app.Name) (exit code $rc) - see the messages above." 'Red' }
    }
    if (-not $DryRun) {
        foreach ($app in $Found) {
            & $Py.Exe -c "import $($app.Imports)" *> $null
            $ok = ($LASTEXITCODE -eq 0)
            $app | Add-Member -NotePropertyName Ready -NotePropertyValue $ok -Force
            if ($ok) { Log "  import check OK: $($app.Name) ($($app.Imports))" 'Green' }
            else { Log "  import check FAILED: $($app.Name) ($($app.Imports))" 'Red' }
        }
    } else {
        foreach ($app in $Found) {
            & $Py.Exe -c "import $($app.Imports)" *> $null
            Log ("  currently importable for {0}: {1}" -f $app.Name, $(if ($LASTEXITCODE -eq 0) { 'yes' } else { 'no' }))
        }
    }
}

# ---------------------------------------------------------------- 4. running instances
Log ""
Log "[4/6] Already running?" 'Cyan'
$procs = @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object { $_.CommandLine })
foreach ($app in $Found) {
    $hits = @($procs | Where-Object { $_.CommandLine -like "*$($app.ScriptPath)*" })
    if ($hits) { Log ("  WARNING: {0} is already running from this folder (PID {1}). Quit it from its tray icon" -f $app.Name, (($hits | ForEach-Object ProcessId) -join ', ')) 'Yellow'
                 Log "           and start it again after this installer so it uses the new setup." 'Yellow' }
    else { Log "  $($app.Name): not running" }
}

# ---------------------------------------------------------------- 5. Start Menu shortcuts
$Shell = New-Object -ComObject WScript.Shell
function Get-LnkDir([string]$folderKind) {
    if ($TestLnkDir) { $d = Join-Path $TestLnkDir $folderKind; New-Item -ItemType Directory -Force -Path $d | Out-Null; return $d }
    return [Environment]::GetFolderPath($folderKind)
}
function Ensure-Shortcut($app, [string]$folderKind) {
    $dir = Get-LnkDir $folderKind
    $lnk = Join-Path $dir ($app.Name + '.lnk')
    $icon = Join-Path $app.Dir 'icon.ico'
    $wantArgs = '"' + $app.ScriptPath + '"'
    if (Test-Path -LiteralPath $lnk) {
        $cur = $Shell.CreateShortcut($lnk)
        $targetOk = Test-Path -LiteralPath $cur.TargetPath
        if ($targetOk -and $cur.TargetPath -like "$Root\*") {
            # a .venv inside the shared folder may come from the other PC: only keep it if it works here
            $vpy = Join-Path (Split-Path -Parent $cur.TargetPath) 'python.exe'
            & $vpy -c "import $($app.Imports)" *> $null
            if ($LASTEXITCODE -ne 0) { $targetOk = $false; Log "  shortcut uses $($cur.TargetPath), which does not work on this PC" 'Yellow' }
        }
        if ($targetOk -and ($cur.Arguments -like "*$($app.ScriptPath)*")) {
            Log "  kept existing (already correct): $lnk"
            Log "        -> $($cur.TargetPath) $($cur.Arguments)"
            return 'kept'
        }
        Log "  existing shortcut is outdated or broken: $lnk -> $($cur.TargetPath) $($cur.Arguments)" 'Yellow'
        if ($DryRun) { Log "  (dry run) would replace it (old one saved to %TEMP%\install-widgets-old-shortcuts)"; return 'would-replace' }
        $bk = Join-Path $env:TEMP 'install-widgets-old-shortcuts'
        New-Item -ItemType Directory -Force -Path $bk | Out-Null
        Copy-Item -LiteralPath $lnk -Destination (Join-Path $bk ("{0}-{1}-{2}" -f $folderKind, (Get-Date -Format yyyyMMddHHmmss), (Split-Path -Leaf $lnk))) -Force
    }
    if ($DryRun) {
        Log "  (dry run) would create: $lnk"
        Log "        -> $($Py.Pythonw) $wantArgs  (icon $icon)"
        return 'would-create'
    }
    $s = $Shell.CreateShortcut($lnk)
    $s.TargetPath = $Py.Pythonw
    $s.Arguments = $wantArgs
    $s.WorkingDirectory = $app.Dir
    $s.IconLocation = $icon + ',0'
    $s.Description = $app.Desc
    $s.Save()
    $chk = $Shell.CreateShortcut($lnk)
    Log "  created: $lnk" 'Green'
    Log "        -> $($chk.TargetPath) $($chk.Arguments)  (icon $($chk.IconLocation))"
    return 'created'
}
Log ""
Log "[5/6] Start Menu shortcuts" 'Cyan'
if ($Py) { foreach ($app in $Found) { [void](Ensure-Shortcut $app 'Programs') } }

# ---------------------------------------------------------------- 6. Start with Windows
Log ""
Log "[6/6] Start with Windows" 'Cyan'
$startupDir = Get-LnkDir 'Startup'
if ($Py) {
    foreach ($app in $Found) {
        if ($app.NoStartup) { Log "  $($app.Name): on-demand tool - not offered for Start with Windows"; continue }
        $lnk = Join-Path $startupDir ($app.Name + '.lnk')
        if (Test-Path -LiteralPath $lnk) {
            Log "  $($app.Name): already enabled - checking the shortcut"
            [void](Ensure-Shortcut $app 'Startup')
            continue
        }
        if ($DryRun) { Log "  $($app.Name): not enabled - (dry run) would ask 'Start $($app.Name) with Windows? [y/N]'"; continue }
        $yes = switch ($StartupMode) { 'yes' { $true } 'no' { $false } default { Ask-YesNo "  Start $($app.Name) automatically with Windows?" } }
        if ($yes) { [void](Ensure-Shortcut $app 'Startup') } else { Log "  $($app.Name): not enabled (you can switch it on later in its tray menu)" }
    }
}

# ---------------------------------------------------------------- browser + summary
Log ""
Log "Browser for Home Panel" 'Cyan'
$browsers = @()
foreach ($b in @(@('Microsoft Edge', 'Microsoft\Edge\Application\msedge.exe'), @('Google Chrome', 'Google\Chrome\Application\chrome.exe'))) {
    foreach ($base in @(${env:ProgramFiles(x86)}, $env:ProgramFiles, $env:ProgramW6432, $env:LOCALAPPDATA)) {
        if ($base -and (Test-Path -LiteralPath (Join-Path $base $b[1]))) { $browsers += "$($b[0]): $(Join-Path $base $b[1])"; break }
    }
}
if ($browsers) { $browsers | Select-Object -Unique | ForEach-Object { Log "  found $_" 'Green' } }
else { Log "  WARNING: neither Microsoft Edge nor Google Chrome was found - Home Panel needs one of them." 'Yellow' }

Log ""
Log "=== Summary ===" 'Cyan'
if ($DryRun) { Log "Dry run finished - nothing was changed." 'Yellow' }
elseif ($PipFailed) { Log ("Package installation FAILED for: " + ($PipFailed -join ', ') + ". Fix the error above and run install-widgets.bat again.") 'Red' }
else { Log "Done. Start the apps from the Start Menu: $(($Found | ForEach-Object Name) -join ', ')." 'Green' }
Log ""
Log "IMPORTANT - sign in again on this PC." 'Yellow'
Log "Logins are NOT copied: tokens are kept in Windows Credential Manager and the browser profile" 'Yellow'
Log "on each PC, and settings/layout live in %APPDATA% / %LOCALAPPDATA% of each PC:" 'Yellow'
foreach ($app in $Found) { Log "  * $($app.SignIn)" }
Log ""
Log "Note: the app folders are shared between your PCs. Do not run install.bat inside them on a"
Log "second PC (it would create a .venv in the shared folder); this installer is all you need."
if ($DryRun) { exit 0 }
if ($PipFailed) { exit 2 }
exit 0
