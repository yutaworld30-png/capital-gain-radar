#Requires -RunAsAdministrator

[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [ValidateRange(1, 2147483647)]
    [int]$InterfaceIndex,

    [Parameter(Mandatory)]
    [ValidatePattern('^\d{1,3}(\.\d{1,3}){3}$')]
    [string]$BindAddress,

    [Parameter(Mandatory)]
    [ValidateRange(1024, 65535)]
    [int]$Port,

    [Parameter(Mandatory)]
    [string]$PythonExe,

    [string]$LogPath
)

$ErrorActionPreference = "Stop"
$RuleName = "Capital Gain Radar Mobile $Port ($BindAddress)"

trap {
    $message = ($_ | Out-String)
    if ($LogPath) {
        Set-Content -LiteralPath $LogPath -Value $message -Encoding UTF8
    }
    Write-Error $message
    exit 1
}

$assignedAddress = Get-NetIPAddress `
    -InterfaceIndex $InterfaceIndex `
    -AddressFamily IPv4 `
    -ErrorAction Stop |
    Where-Object { $_.IPAddress -eq $BindAddress }

if (-not $assignedAddress) {
    throw "$BindAddress is not assigned to the selected network interface."
}

$profile = Get-NetConnectionProfile -InterfaceIndex $InterfaceIndex -ErrorAction Stop
if ($profile.NetworkCategory -ne "Private") {
    Set-NetConnectionProfile `
        -InterfaceIndex $InterfaceIndex `
        -NetworkCategory Private `
        -ErrorAction Stop
}

$existingRule = Get-NetFirewallRule -DisplayName $RuleName -ErrorAction SilentlyContinue
if ($existingRule) {
    Set-NetFirewallRule `
        -DisplayName $RuleName `
        -Enabled True `
        -Profile Private `
        -Direction Inbound `
        -Action Allow `
        -ErrorAction Stop | Out-Null
}
else {
    New-NetFirewallRule `
        -DisplayName $RuleName `
        -Description "Capital Gain Radar mobile access from the local private subnet only." `
        -Enabled True `
        -Profile Private `
        -Direction Inbound `
        -Action Allow `
        -Protocol TCP `
        -LocalAddress $BindAddress `
        -LocalPort $Port `
        -RemoteAddress LocalSubnet `
        -Program $PythonExe `
        -ErrorAction Stop | Out-Null
}

Write-Host "OK: Private Wi-Fi and local-subnet firewall access configured."
if ($LogPath) {
    Set-Content -LiteralPath $LogPath -Value "OK" -Encoding UTF8
}
