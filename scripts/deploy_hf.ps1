# Deploy the backend/ folder to a Hugging Face Space (Windows PowerShell).
#
# What this does: `git subtree split --prefix backend` builds a throwaway
# history containing ONLY backend/ at its root, then force-pushes that to the
# Space's `main`. Hugging Face reads the Space repo root, so the Space ends up
# with hf_app.py, requirements.txt and app/ where it expects them.
#
# ⚠ This OVERWRITES the Space's starter files (README.md, app.py, everything
# Hugging Face generated when you created it). That is intended: this repo is
# the source of truth.
#
# No credentials live in this script or in the git remote. `git push` prompts
# for the Hugging Face username and a WRITE access token.
#
# Usage:
#   pwsh scripts/deploy_hf.ps1                  # deploy current main
#   pwsh scripts/deploy_hf.ps1 -SkipChecks      # emergency: skip the test gate
#
# NOTE on PowerShell: native commands write progress to stderr, and Windows
# PowerShell 5 turns that into a scary NativeCommandError even on success.
# This script therefore never relies on $? or the error stream — it checks
# $LASTEXITCODE after every git call and prints its own clear verdict.

[CmdletBinding()]
param(
    [switch]$SkipChecks
)

$ErrorActionPreference = 'Stop'

$HfSpaceRepo = if ($env:HF_SPACE_REPO) { $env:HF_SPACE_REPO } else { 'saboornotgonna/routellm' }
$HfRemote    = if ($env:HF_REMOTE)    { $env:HF_REMOTE }    else { 'hf' }
$Branch      = if ($env:BRANCH)       { $env:BRANCH }       else { 'main' }

function Write-Step([string]$Message) {
    Write-Host ""
    Write-Host "==> $Message" -ForegroundColor Cyan
}

function Stop-Deploy([string]$Message) {
    Write-Host ""
    Write-Host "FAILED: $Message" -ForegroundColor Red
    exit 1
}

$RepoRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $RepoRoot

# ---------------------------------------------------------------------------
Write-Step "1/5 Checking the working tree"
$status = git status --porcelain
if ($LASTEXITCODE -ne 0) { Stop-Deploy "git status failed — is this a git repository?" }
if ($status) {
    git status --short
    Stop-Deploy "working tree is dirty. Commit or stash first (the Space is public - an accidental .env would be world-readable)."
}
Write-Host "clean."

# ---------------------------------------------------------------------------
Write-Step "2/5 Checking that the backend tests last passed"
if ($SkipChecks) {
    Write-Host "skipped (-SkipChecks). Only do this for a hotfix you verified another way." -ForegroundColor Yellow
} else {
    $py = Join-Path $RepoRoot '.venv\Scripts\python.exe'
    if (-not (Test-Path $py)) { $py = Join-Path $RepoRoot 'backend\.venv\Scripts\python.exe' }
    if (-not (Test-Path $py)) {
        Stop-Deploy "no virtualenv found. Run: python -m venv .venv; .\.venv\Scripts\pip install -r backend\requirements-dev.txt"
    }
    Push-Location (Join-Path $RepoRoot 'backend')
    & $py -m pytest -q
    $testExit = $LASTEXITCODE
    Pop-Location
    if ($testExit -ne 0) { Stop-Deploy "backend tests failed - not deploying." }
    Write-Host "tests green."
}

# ---------------------------------------------------------------------------
Write-Step "3/5 Making sure the '$HfRemote' remote exists"
$expectedUrl = "https://huggingface.co/spaces/$HfSpaceRepo"
$currentUrl = git remote get-url $HfRemote 2>$null
if ($LASTEXITCODE -eq 0 -and $currentUrl) {
    if ($currentUrl.Trim() -ne $expectedUrl) {
        Write-Host "remote '$HfRemote' points at $currentUrl; updating to $expectedUrl"
        git remote set-url $HfRemote $expectedUrl
        if ($LASTEXITCODE -ne 0) { Stop-Deploy "could not update the remote URL." }
    } else {
        Write-Host "remote OK: $expectedUrl"
    }
} else {
    git remote add $HfRemote $expectedUrl
    if ($LASTEXITCODE -ne 0) { Stop-Deploy "could not add the remote." }
    Write-Host "added remote '$HfRemote' -> $expectedUrl"
}

# ---------------------------------------------------------------------------
Write-Step "4/5 Splitting out backend/"
$splitSha = git subtree split --prefix backend $Branch
if ($LASTEXITCODE -ne 0 -or -not $splitSha) { Stop-Deploy "git subtree split failed." }
$splitSha = ($splitSha | Select-Object -Last 1).Trim()
Write-Host "subtree sha: $splitSha"

# ---------------------------------------------------------------------------
Write-Step "5/5 Force-pushing to the Space"
Write-Host @"
You will be prompted for credentials:
  Username: your Hugging Face username
  Password: a WRITE access token from https://huggingface.co/settings/tokens
           (NOT your account password; the token is never stored by this script)
"@ -ForegroundColor Yellow

git push $HfRemote "$($splitSha):main" --force
$pushExit = $LASTEXITCODE

# git writes normal progress to stderr; PowerShell may have printed a
# NativeCommandError above even though the push succeeded. $LASTEXITCODE is the
# only trustworthy signal.
if ($pushExit -eq 0) {
    $owner = $HfSpaceRepo.Split('/')[0]
    $space = $HfSpaceRepo.Split('/')[1]
    Write-Host ""
    Write-Host "Deployed. Space: https://huggingface.co/spaces/$HfSpaceRepo" -ForegroundColor Green
    Write-Host "  API base:  https://$owner-$space.hf.space"
    Write-Host "  Next:      watch the Space's Logs tab, then run:"
    Write-Host "             python scripts/smoke_deployed.py --api https://$owner-$space.hf.space --origin <frontend-url>"
    exit 0
} else {
    Stop-Deploy "push rejected (exit $pushExit). Usual causes: a READ token instead of WRITE, not logged in, or the Space id is wrong."
}
