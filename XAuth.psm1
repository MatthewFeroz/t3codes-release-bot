# Retired. Nightly posts are published by GitHub Actions with OAuth 1.0a secrets.
# This module no longer reads the Windows credential store or talks to X.

function Get-T3XAccessToken {
    [CmdletBinding()]
    param([switch]$ForceRefresh)
    throw 'XAuth.psm1 is retired. Nightly posts are published by GitHub Actions with OAuth 1.0a secrets. See README.md.'
}

function Test-T3XAuthentication {
    [CmdletBinding()]
    param([switch]$ForceRefresh)
    throw 'XAuth.psm1 is retired. Nightly posts are published by GitHub Actions with OAuth 1.0a secrets. See README.md.'
}

Export-ModuleMember -Function Get-T3XAccessToken, Test-T3XAuthentication
