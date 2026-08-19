param(
    [switch]$InstallDependencies
)

$ErrorActionPreference = "Stop"
$ProjectRoot = $PSScriptRoot
$TemplateRoot = Join-Path $ProjectRoot "敏感配置模板"

function Copy-TemplateIfMissing {
    param([string]$Template, [string]$Destination)
    if (Test-Path -LiteralPath $Destination) {
        Write-Host "Keeping existing local config: $Destination"
        return
    }
    $parent = Split-Path -Parent $Destination
    New-Item -ItemType Directory -Force -Path $parent | Out-Null
    Copy-Item -LiteralPath $Template -Destination $Destination
    Write-Host "Created local config: $Destination"
}

function Set-EnvValue {
    param([string]$Path, [string]$Key, [string]$Value)
    $text = [IO.File]::ReadAllText($Path)
    $pattern = "(?m)^" + [regex]::Escape($Key) + "=.*$"
    if (-not [regex]::IsMatch($text, $pattern)) {
        throw "Missing key $Key in $Path"
    }
    $updated = [regex]::Replace($text, $pattern, $Key + "=" + $Value, 1)
    $utf8 = New-Object Text.UTF8Encoding($false)
    [IO.File]::WriteAllText($Path, $updated, $utf8)
}

$BotEnv = Join-Path $ProjectRoot "bot\.env"
$ChatEnv = Join-Path $ProjectRoot "bot\chat.env"
$GlobalEnv = Join-Path $ProjectRoot "workflow\env\global.env"
$CdpEnv = Join-Path $ProjectRoot "workflow\env\cdp.env"
$ChatsEnv = Join-Path $ProjectRoot "workflow\env\chats.env"
$BasesEnv = Join-Path $ProjectRoot "workflow\env\bases.env"

Copy-TemplateIfMissing (Join-Path $TemplateRoot "bot.env.template") $BotEnv
Copy-TemplateIfMissing (Join-Path $TemplateRoot "chat.env.template") $ChatEnv
Copy-TemplateIfMissing (Join-Path $TemplateRoot "workflow_global.env.template") $GlobalEnv
Copy-TemplateIfMissing (Join-Path $TemplateRoot "workflow_cdp.env.template") $CdpEnv
Copy-TemplateIfMissing (Join-Path $TemplateRoot "workflow_chats.env.template") $ChatsEnv
Copy-TemplateIfMissing (Join-Path $TemplateRoot "workflow_bases.env.template") $BasesEnv

Set-EnvValue $BotEnv "JIELONG_BOT_WORKDIR" $ProjectRoot
Set-EnvValue $BotEnv "JIELONG_BOT_HEARTBEAT_SECONDS" "300"
Set-EnvValue $GlobalEnv "JIELONG_SOCIAL_WORKDIR" $ProjectRoot
Set-EnvValue $CdpEnv "SOCIAL_CDP_PROFILE" (Join-Path $ProjectRoot "workflow\assets\chrome-cdp-profile")
Set-EnvValue $BasesEnv "BACKFILL_XLSX_DIR" (Join-Path $ProjectRoot "exports")
Set-EnvValue $ChatEnv "WINSTON_CHAT_HISTORY_PATH" (Join-Path $ProjectRoot "bot\tmp\winston_chat_history.json")

@("exports", "logs", "tmp", "bot\tmp", "workflow\tmp", "workflow\assets\chrome-cdp-profile") |
    ForEach-Object { New-Item -ItemType Directory -Force -Path (Join-Path $ProjectRoot $_) | Out-Null }

if ($InstallDependencies) {
    $Python = (Get-Command python -ErrorAction Stop).Source
    & $Python -m venv (Join-Path $ProjectRoot ".venv")
    $VenvPython = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
    & $VenvPython -m pip install --upgrade pip
    & $VenvPython -m pip install -r (Join-Path $ProjectRoot "requirements.txt")
    & $VenvPython -m playwright install chromium
}

Write-Host "Local configuration skeleton is ready."
Write-Host "Next: read RESTORE_ON_NEW_DEVICE.md and fill Feishu resource IDs locally."
