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

$secure = Read-Host "Paste LingSuan API Key (input is hidden)" -AsSecureString
$ptr = [IntPtr]::Zero
$apiKey = $null
try {
    $ptr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
    $apiKey = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($ptr)
    if ([string]::IsNullOrWhiteSpace($apiKey)) {
        throw "LingSuan API Key cannot be empty."
    }
    if ($apiKey -match "[\r\n]") {
        throw "LingSuan API Key contains invalid line breaks."
    }

    $chatEnv = Join-Path $ProjectRoot "bot\chat.env"
    Set-EnvValue -Path $chatEnv -Key "WINSTON_CHAT_API_KEY" -Value $apiKey.Trim()
    Set-EnvValue -Path $chatEnv -Key "WINSTON_CHAT_BASE_URL" -Value "https://lingsuan.org/v1"
    Set-EnvValue -Path $chatEnv -Key "WINSTON_CHAT_MODEL" -Value "gpt-5.6-terra"
    Set-EnvValue -Path $chatEnv -Key "WINSTON_CHAT_API_STYLE" -Value "responses"
    Set-EnvValue -Path $chatEnv -Key "WINSTON_CHAT_ENABLED" -Value "true"
    Write-Host "LingSuan chat configuration saved successfully."
}
finally {
    if ($ptr -ne [IntPtr]::Zero) {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($ptr)
    }
    $apiKey = $null
    if ($null -ne $secure) {
        $secure.Dispose()
    }
}

Read-Host "Press Enter to close"
