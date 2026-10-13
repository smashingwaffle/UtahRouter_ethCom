# Registers Utah eth2RS422 to start automatically when this user logs on.
# Run once from PowerShell in the repo folder:
#   powershell -ExecutionPolicy Bypass -File tools\install_autostart.ps1
# Remove again with:
#   Unregister-ScheduledTask -TaskName "Utah eth2RS422" -Confirm:$false

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$exe  = Join-Path $root "dist\UtahEth2RS422.exe"

if (Test-Path $exe) {
    $action = New-ScheduledTaskAction -Execute $exe -Argument "--no-browser" -WorkingDirectory $root
} else {
    $python = (Get-Command python -ErrorAction Stop).Source
    $script = Join-Path $root "run_eth2rs422.py"
    $action = New-ScheduledTaskAction -Execute $python -Argument "`"$script`" --no-browser" -WorkingDirectory $root
}

$trigger  = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
            -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero)

Register-ScheduledTask -TaskName "Utah eth2RS422" -Action $action -Trigger $trigger -Settings $settings `
    -Description "TCP <-> RS-422 bridge between Cerebrum and the Utah SC-40" -Force | Out-Null

Write-Host "Installed. Utah eth2RS422 will start at logon (web UI: http://localhost:8422)."
Write-Host "Start it now with:  Start-ScheduledTask -TaskName 'Utah eth2RS422'"
