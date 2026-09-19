$ErrorActionPreference = 'Stop'
$script:VaultPath = Join-Path $env:LOCALAPPDATA 'T3CodesReleaseBot\credentials.dpapi'

function Read-XVault {
    $encrypted = Get-Content -LiteralPath $script:VaultPath -Raw -ErrorAction Stop
    $secure = ConvertTo-SecureString $encrypted
    $plain = [System.Net.NetworkCredential]::new('', $secure).Password
    return ($plain | ConvertFrom-Json)
}

function Save-XVault($Value) {
    $secure = ConvertTo-SecureString ($Value | ConvertTo-Json -Depth 8 -Compress) -AsPlainText -Force
    $encrypted = ConvertFrom-SecureString $secure
    $temporary = $script:VaultPath + '.' + [Guid]::NewGuid().ToString('N') + '.tmp'
    [IO.File]::WriteAllText($temporary, $encrypted)
    if (Test-Path -LiteralPath $script:VaultPath) {
        $backup = $script:VaultPath + '.previous'
        [IO.File]::Replace($temporary, $script:VaultPath, $backup)
        [IO.File]::Delete($backup)
    } else {
        [IO.File]::Move($temporary, $script:VaultPath)
    }
}

function Get-T3XAccessToken {
    [CmdletBinding()]
    param([switch]$ForceRefresh)
    # All processes using this module serialize refreshes to preserve rotating tokens.
    $lock = $null
    $deadline = [DateTime]::UtcNow.AddSeconds(30)
    while ($null -eq $lock) {
        try {
            $lock = [IO.File]::Open(($script:VaultPath + '.lock'), 'OpenOrCreate', 'ReadWrite', 'None')
        } catch [IO.IOException] {
            if ([DateTime]::UtcNow -gt $deadline) { throw 'Credential store is busy.' }
            Start-Sleep -Milliseconds 200
        }
    }
    try {
        $vault = Read-XVault
        if ($ForceRefresh -or [DateTimeOffset]::Parse($vault.expires_at) -lt [DateTimeOffset]::UtcNow.AddMinutes(5)) {
            $basic = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($vault.client_id + ':' + $vault.client_secret))
            try {
                $response = Invoke-RestMethod -Method Post -Uri 'https://api.x.com/2/oauth2/token' -Headers @{ Authorization = "Basic $basic" } -ContentType 'application/x-www-form-urlencoded' -Body @{ grant_type = 'refresh_token'; refresh_token = $vault.refresh_token } -TimeoutSec 30 -ErrorAction Stop
            } catch {
                $status = if ($_.Exception.Response) { [int]$_.Exception.Response.StatusCode } else { 'network failure' }
                throw "X token refresh failed ($status). Stored credentials were preserved."
            }
            if (-not $response.access_token -or -not $response.expires_in) { throw 'X returned an incomplete token response.' }
            $vault.access_token = $response.access_token
            if ($response.refresh_token) { $vault.refresh_token = $response.refresh_token }
            $vault.expires_at = [DateTimeOffset]::UtcNow.AddSeconds([double]$response.expires_in).ToString('o')
            $vault | Add-Member -NotePropertyName scope -NotePropertyValue $response.scope -Force
            Save-XVault $vault
        }
        return $vault.access_token
    } finally {
        if ($lock) { $lock.Dispose() }
    }
}

function Test-T3XAuthentication {
    [CmdletBinding()]
    param([switch]$ForceRefresh)
    $token = Get-T3XAccessToken -ForceRefresh:$ForceRefresh
    try {
        $result = Invoke-RestMethod -Uri 'https://api.x.com/2/users/me' -Headers @{ Authorization = "Bearer $token" } -TimeoutSec 30 -ErrorAction Stop
    } catch {
        $status = if ($_.Exception.Response) { [int]$_.Exception.Response.StatusCode } else { 'network failure' }
        throw "X account verification failed ($status). No tokens were logged."
    }
    if ($result.data.username -ine 't3codes') { throw 'Credentials belong to an unexpected X account.' }
    [pscustomobject]@{ Username = $result.data.username; Authenticated = $true }
}

Export-ModuleMember -Function Get-T3XAccessToken, Test-T3XAuthentication
