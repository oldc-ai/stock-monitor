# Registers the Stock SMA Monitor as a Windows scheduled task that auto-starts
# at logon. Run this in PowerShell from anywhere — no admin needed.
#
# To stop:    Stop-ScheduledTask        -TaskName StockSMAMonitor
# To start:   Start-ScheduledTask       -TaskName StockSMAMonitor
# To remove:  Unregister-ScheduledTask  -TaskName StockSMAMonitor -Confirm:$false

$ErrorActionPreference = "Stop"

$taskName  = "StockSMAMonitor"
$batchPath = "C:\Users\limin\claude\stock-monitor\run.bat"

if (-not (Test-Path $batchPath)) {
    Write-Error "Could not find $batchPath"
    exit 1
}

# If the task already exists, remove it first so this script is idempotent.
if (Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue) {
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
    Write-Host "Removed existing $taskName task."
}

$action  = New-ScheduledTaskAction -Execute $batchPath
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit (New-TimeSpan -Days 0)

Register-ScheduledTask `
    -TaskName    $taskName `
    -Action      $action `
    -Trigger     $trigger `
    -Settings    $settings `
    -Description "Stock SMA Monitor — alerts on price crossing below 10/20/50/200-day SMA"

Write-Host ""
Write-Host "Registered scheduled task: $taskName"
Write-Host "It will auto-start the next time you log in."
Write-Host ""
Write-Host "To start it right now without logging out:"
Write-Host "    Start-ScheduledTask -TaskName $taskName"
