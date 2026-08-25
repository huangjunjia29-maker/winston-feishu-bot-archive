param(
    [string]$TaskName = "WinstonFeishuBot"
)

$ErrorActionPreference = "Stop"
$ProjectRoot = $PSScriptRoot
$ListenerScript = Join-Path $ProjectRoot "bot\scripts\run_listener.ps1"

if (-not (Test-Path -LiteralPath $ListenerScript)) {
    throw "Listener script not found: $ListenerScript"
}

$PowerShell = (Get-Command powershell.exe -ErrorAction Stop).Source
$Arguments = '-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "' + $ListenerScript + '"'
$Action = New-ScheduledTaskAction -Execute $PowerShell -Argument $Arguments -WorkingDirectory $ProjectRoot
$Trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$Trigger.Delay = "PT3M"
$Settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit ([TimeSpan]::Zero)
$UserId = "$env:USERDOMAIN\$env:USERNAME"
$Principal = New-ScheduledTaskPrincipal -UserId $UserId -LogonType Interactive -RunLevel Limited

Register-ScheduledTask `
    -TaskName $TaskName `
    -Description "Start the Winston Feishu bot listener after this user signs in." `
    -Action $Action `
    -Trigger $Trigger `
    -Settings $Settings `
    -Principal $Principal `
    -Force | Out-Null

Write-Host "Installed scheduled task: $TaskName"
Write-Host "Winston will start 3 minutes after $UserId signs in."
