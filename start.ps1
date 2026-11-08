# RouteLLM launcher (Windows PowerShell).
#
#   .\start.ps1                  # http://127.0.0.1:8000
#   .\start.ps1 --dev            # backend reload + Vite dev server
#   .\start.ps1 --port 8123      # different port
#   .\start.ps1 --help           # all options
#
# NOTE: Windows blocks local scripts by default. If PowerShell refuses to run
# this file, either use the bundled wrapper:
#
#   powershell -ExecutionPolicy Bypass -File .\start.ps1
#
# ...or allow local scripts for your user once (no admin rights needed):
#
#   Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
#
# Everything real happens in run.py; this wrapper only locates Python.

$ErrorActionPreference = 'Stop'
Set-Location -Path $PSScriptRoot

$candidates = @('py -3.13', 'py -3.12', 'py -3.11', 'python', 'python3')
foreach ($candidate in $candidates) {
    $parts = $candidate.Split(' ')
    $exe = $parts[0]
    if (Get-Command $exe -ErrorAction SilentlyContinue) {
        $arguments = @()
        if ($parts.Length -gt 1) { $arguments += $parts[1..($parts.Length - 1)] }
        $arguments += 'run.py'
        $arguments += $args
        & $exe @arguments
        exit $LASTEXITCODE
    }
}

Write-Host 'Python 3.11+ was not found on PATH.' -ForegroundColor Red
Write-Host '  Install it from https://www.python.org/downloads/windows/'
Write-Host '  and tick "Add python.exe to PATH" during setup, then rerun this script.'
exit 1
