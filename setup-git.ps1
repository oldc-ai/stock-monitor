# One-shot: cleans any stale .git, initializes a fresh repo, commits everything,
# creates a GitHub repo via the gh CLI, and pushes.
#
# Requirements:
#   - git installed (https://git-scm.com)
#   - gh CLI installed AND authenticated (`gh auth status` succeeds)
#       - install: winget install --id GitHub.cli
#       - auth:    gh auth login
#
# Usage (from PowerShell, in this folder):
#   .\setup-git.ps1                              # private repo, name = stock-monitor
#   .\setup-git.ps1 -RepoName my-alerter -Public # custom name, public repo

[CmdletBinding()]
param(
    [string]$RepoName = "stock-monitor",
    [switch]$Public
)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

# Sanity checks
foreach ($cmd in @("git", "gh")) {
    if (-not (Get-Command $cmd -ErrorAction SilentlyContinue)) {
        Write-Error "$cmd not found on PATH. Install it and re-run."
        exit 1
    }
}

# Confirm gh is authenticated
$ghStatus = gh auth status 2>&1
if ($LASTEXITCODE -ne 0) {
    Write-Error "gh CLI is not authenticated. Run: gh auth login"
    exit 1
}

# Clean any stale .git from a prior aborted attempt
if (Test-Path .git) {
    Write-Host "Removing stale .git directory..."
    Remove-Item -Recurse -Force .git
}

# Init + commit
git init -b main | Out-Null
git add .
git commit -m "Initial commit: stock SMA Discord alerter" | Out-Null

# Create the repo on GitHub and push
$visibility = if ($Public) { "--public" } else { "--private" }
Write-Host "Creating GitHub repo $RepoName ($($visibility -replace '--',''))..."
gh repo create $RepoName $visibility --source=. --remote=origin --push

Write-Host ""
Write-Host "Done. Repo is live:"
gh repo view --web 2>$null
gh repo view | Select-String "^https://"
