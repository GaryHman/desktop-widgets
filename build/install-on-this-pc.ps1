# Replaces the old Python-run widget apps on THIS PC with the installed .exe versions.
# Never deletes: old shortcuts are moved to Grokbot stuff\.archive and logged in Grokbot stuff\.logs\MOVELOG.md.
$ErrorActionPreference = 'Continue'
$root = $PSScriptRoot
$g    = $root; while ($g -and -not ((Test-Path -LiteralPath (Join-Path $g '.logs')) -and (Test-Path -LiteralPath (Join-Path $g '.archive')))) { $g = Split-Path $g -Parent }   # ...\Grokbot stuff (first parent with .logs + .archive)
$ts   = Get-Date -Format 'yyyyMMdd-HHmm'
$irr  = Join-Path $g ".archive\widgets-python-shortcuts-$ts"
$movelog = Join-Path $g '.logs\MOVELOG.md'
$cfg  = Import-PowerShellDataFile (Join-Path $root 'apps.psd1')

# 1. stop the old python/pythonw instances
$old = Get-CimInstance Win32_Process | Where-Object { $_.Name -match '^pythonw?\.exe$' -and $_.CommandLine -match 'keep_widgets\.py|todoist_widgets\.py|keep_todoist_bridge\.py' }
foreach ($p in $old) { "stop old: pid $($p.ProcessId) $($p.CommandLine)"; Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue }
Start-Sleep 2

# 2. move old shortcuts (pythonw-based) out of Startup / Start Menu
$sm = Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs'
$sh = New-Object -ComObject WScript.Shell
$cands = @()
foreach ($n in 'KeepNotes Widgets','Todoist Widgets','Keep-Todoist Bridge') {
  $cands += @{ Path = Join-Path $sm "$n.lnk"; Sub = 'Start Menu' }
  $cands += @{ Path = Join-Path $sm "Startup\$n.lnk"; Sub = 'Startup' }
}
foreach ($c in $cands) {
  if (-not (Test-Path -LiteralPath $c.Path)) { continue }
  $t = $sh.CreateShortcut($c.Path).TargetPath
  if ($t -notmatch 'python') { "keep (not python): $($c.Path) -> $t"; continue }
  $dest = Join-Path $irr $c.Sub
  New-Item -ItemType Directory -Force $dest | Out-Null
  Move-Item -LiteralPath $c.Path -Destination $dest
  "moved: $($c.Path) -> $dest"
  if (-not (Test-Path $movelog)) { "# MOVELOG`n`n| When | From | To | Why |`n|---|---|---|---|" | Set-Content $movelog -Encoding UTF8 }
  "| $(Get-Date -Format 'yyyy-MM-dd HH:mm') | ``$($c.Path)`` | ``$dest`` | old pythonw shortcut ($t), replaced by installed .exe version |" | Add-Content $movelog -Encoding UTF8
}

# 3. run the installers silently (per user, no admin)
foreach ($a in $cfg.Apps) {
  $setup = Join-Path $root "installers\$($a.Exe)-Setup-$($a.Ver).exe"
  if (-not (Test-Path $setup)) { "MISSING installer: $setup"; continue }
  $tasks = if ($a.Key -eq 'bridge') { '' } else { 'autostart' }   # bridge: autostart after it is configured
  $log = Join-Path $root "logs\setup-$($a.Exe).log"
  $p = Start-Process $setup -ArgumentList '/VERYSILENT','/SUPPRESSMSGBOXES','/NORESTART','/CURRENTUSER',"/TASKS=`"$tasks`"","/LOG=`"$log`"" -Wait -PassThru
  "installed $($a.Name) $($a.Ver): exit $($p.ExitCode)"
}

# 4. start them
foreach ($a in $cfg.Apps) {
  $exe = Join-Path $env:LOCALAPPDATA "Programs\$($a.Name)\$($a.Exe).exe"
  if (Test-Path $exe) { Start-Process $exe -WorkingDirectory (Split-Path $exe); "started $exe" } else { "NOT FOUND $exe" }
}
