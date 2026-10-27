# Registers the "bravia_daemon" scheduled task from the bravia_daemon.xml template, filling in
# this machine's paths and user so the template checked into the repo stays free of them.
# Run from an elevated PowerShell: the task uses RunLevel=HighestAvailable.
$ErrorActionPreference = 'Stop'

$repoDir = $PSScriptRoot

# The task must run the base interpreter's pythonw.exe directly. .venv\Scripts\pythonw.exe is a
# trampoline that spawns a console python.exe (a black window appears), and wrapping the call in
# powershell.exe leaves a grandchild that "schtasks /End" cannot stop.
$cfgPath = Join-Path $repoDir '.venv\pyvenv.cfg'
if (-not (Test-Path $cfgPath)) { throw "$cfgPath not found - run 'uv sync' first" }
$pyHome = $null
foreach ($line in Get-Content $cfgPath) {
    if ($line -match '^home\s*=\s*(.+)$') { $pyHome = $Matches[1].Trim() }
}
if (-not $pyHome) { throw "No 'home' entry in $cfgPath" }
$pythonw = Join-Path $pyHome 'pythonw.exe'
if (-not (Test-Path $pythonw)) { throw "$pythonw not found" }

$sid = [System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value

$esc = { param($s) [System.Security.SecurityElement]::Escape($s) }
$xml = (Get-Content (Join-Path $repoDir 'bravia_daemon.xml') -Raw -Encoding UTF8).
    Replace('{{USER_SID}}', (& $esc $sid)).
    Replace('{{PYTHONW}}', (& $esc $pythonw)).
    Replace('{{REPO_DIR}}', (& $esc $repoDir)).
    Replace('encoding="UTF-8"', 'encoding="UTF-16"')

$tmp = Join-Path $env:TEMP 'bravia_daemon.task.xml'
try {
    # schtasks expects the XML as UTF-16
    Set-Content -Path $tmp -Value $xml -Encoding Unicode
    schtasks /Create /XML $tmp /TN bravia_daemon /F
    if ($LASTEXITCODE -ne 0) { throw "schtasks failed with exit code $LASTEXITCODE" }
} finally {
    Remove-Item $tmp -ErrorAction SilentlyContinue
}
