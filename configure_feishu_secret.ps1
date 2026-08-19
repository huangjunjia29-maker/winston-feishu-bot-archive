param(
    [string]$ProjectRoot = $PSScriptRoot
)

$ErrorActionPreference = "Stop"

function Set-EnvValue {
    param(
        [string]$Path,
        [string]$Key,
        [string]$Value
    )

    $text = [System.IO.File]::ReadAllText($Path)
    $pattern = "(?m)^" + [regex]::Escape($Key) + "=.*$"
    if (-not [regex]::IsMatch($text, $pattern)) {
        throw "Missing configuration key: $Key in $Path"
    }
    $replacement = $Key + "=" + $Value
    $updated = [regex]::Replace($text, $pattern, $replacement, 1)
    $utf8 = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($Path, $updated, $utf8)
}

$secure = Read-Host "Paste Winston App Secret (input is hidden)" -AsSecureString
$ptr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
try {
    $secret = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($ptr)
    if ([string]::IsNullOrWhiteSpace($secret)) {
        throw "App Secret cannot be empty."
    }

    $chatEnv = Join-Path $ProjectRoot "workflow\env\chats.env"
    $baseEnv = Join-Path $ProjectRoot "workflow\env\bases.env"
    Set-EnvValue -Path $chatEnv -Key "FEISHU_APP_SECRET" -Value $secret
    Set-EnvValue -Path $baseEnv -Key "FEISHU_APP_SECRET" -Value $secret
    Write-Host "Winston project credentials configured successfully."
}
finally {
    if ($ptr -ne [IntPtr]::Zero) {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($ptr)
    }
    $secret = $null
    $secure.Dispose()
}

Read-Host "Press Enter to close"
