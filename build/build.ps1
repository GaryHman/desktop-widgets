# Builds the Windows apps (PyInstaller, windowed, one folder) and their Inno Setup installers.
# Usage:  powershell -ExecutionPolicy Bypass -File build.ps1 [-Only keep,todoist,bridge] [-SkipInstaller]
param([string[]]$Only = @(), [switch]$SkipInstaller)
$ErrorActionPreference = 'Continue'
$root = $PSScriptRoot
$py   = Join-Path $root '.buildvenv\Scripts\python.exe'
$iscc = Join-Path $env:LOCALAPPDATA 'Programs\Inno Setup 6\ISCC.exe'
if (-not (Test-Path $iscc)) { $iscc = Join-Path ${env:ProgramFiles(x86)} 'Inno Setup 6\ISCC.exe' }
$cfg  = Import-PowerShellDataFile (Join-Path $root 'apps.psd1')
New-Item -ItemType Directory -Force "$root\logs","$root\work","$root\dist","$root\installers" | Out-Null
$failed = @()
foreach ($a in $cfg.Apps) {
  if ($Only.Count -and ($Only -notcontains $a.Key)) { continue }
  $src  = Join-Path $root "src\$($a.Src)"
  $work = Join-Path $root "work\$($a.Exe)"
  New-Item -ItemType Directory -Force $work | Out-Null
  $ver  = Join-Path $work 'version.txt'
  & $py (Join-Path $root 'make_version.py') --name $a.Name --exe $a.Exe --ver $a.Ver --out $ver | Out-Null
  $log = Join-Path $root "logs\pyinstaller-$($a.Exe).log"
  Write-Host "== PyInstaller $($a.Name) $($a.Ver)"
  $meta = @(); foreach ($m in @($a.Meta)) { if ($m) { $meta += @('--copy-metadata', $m) } }   # packages that read their own version via importlib.metadata
  & $py -m PyInstaller --noconfirm --clean --windowed --onedir --name $a.Exe `
      --icon (Join-Path $src 'icon.ico') --version-file $ver `
      --add-data "$(Join-Path $src 'icon.ico');." --paths $src @meta `
      --distpath (Join-Path $root 'dist') --workpath $work --specpath $work `
      (Join-Path $src $a.Script) *> $log
  if ($LASTEXITCODE -ne 0) { Write-Host "PyInstaller FAILED ($log)"; $failed += $a.Exe; continue }
  Write-Host "   exe: $(Join-Path $root "dist\$($a.Exe)\$($a.Exe).exe")"
  if ($SkipInstaller) { continue }
  $ilog = Join-Path $root "logs\inno-$($a.Exe).log"
  & $iscc /Q "/DAppVer=$($a.Ver)" (Join-Path $root "installer\$($a.Exe).iss") *> $ilog
  if ($LASTEXITCODE -ne 0) { Write-Host "Inno Setup FAILED ($ilog)"; $failed += "$($a.Exe)-setup"; continue }
  Write-Host "   setup: $(Join-Path $root "installers\$($a.Exe)-Setup-$($a.Ver).exe")"
}
if ($failed.Count) { Write-Host "FAILED: $($failed -join ', ')"; exit 1 }
Write-Host 'BUILD OK'
