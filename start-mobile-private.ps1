[CmdletBinding()]
param(
    [ValidateRange(1024, 65535)]
    [int]$Port = 8768,

    [ValidatePattern('^\d{1,3}(\.\d{1,3}){3}$')]
    [string]$BindAddress
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$PythonExe = $env:CAPITAL_GAIN_RADAR_PYTHON
$PreviousPassword = $env:CAPITAL_GAIN_RADAR_ACCESS_PASSWORD
$LaunchErrorLog = Join-Path $Root ".local-data\mobile-launch-error.log"

trap {
    New-Item -ItemType Directory -Path (Split-Path $LaunchErrorLog) -Force | Out-Null
    Set-Content -LiteralPath $LaunchErrorLog -Value ($_ | Out-String) -Encoding UTF8
    Write-Error ($_ | Out-String)
    exit 1
}

Remove-Item -LiteralPath $LaunchErrorLog -ErrorAction SilentlyContinue

function Test-PrivateIPv4 {
    param([string]$Address)
    return (
        $Address -match '^10\.' -or
        $Address -match '^192\.168\.' -or
        $Address -match '^172\.(1[6-9]|2\d|3[01])\.'
    )
}

function New-AccessPassword {
    $alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789"
    $bytes = New-Object byte[] 12
    $random = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    try {
        $random.GetBytes($bytes)
    }
    finally {
        $random.Dispose()
    }
    return -join ($bytes | ForEach-Object { $alphabet[$_ % $alphabet.Length] })
}

if (-not $PythonExe) {
    $PythonCommand = Get-Command python -ErrorAction SilentlyContinue
    if ($PythonCommand) {
        $PythonExe = $PythonCommand.Source
    }
}

if (-not $PythonExe -or -not (Test-Path -LiteralPath $PythonExe)) {
    throw "Python was not found. Set CAPITAL_GAIN_RADAR_PYTHON to python.exe."
}

$networkCandidates = Get-NetIPConfiguration |
    Where-Object {
        $_.IPv4DefaultGateway -and
        $_.NetAdapter.Status -eq "Up" -and
        $_.IPv4Address.IPAddress
    }

if ($BindAddress) {
    $network = $networkCandidates |
        Where-Object { $_.IPv4Address.IPAddress -contains $BindAddress } |
        Select-Object -First 1
}
else {
    $network = $networkCandidates |
        Where-Object { Test-PrivateIPv4 $_.IPv4Address.IPAddress } |
        Select-Object -First 1
    if ($network) {
        $BindAddress = $network.IPv4Address.IPAddress | Select-Object -First 1
    }
}

if (-not $network -or -not (Test-PrivateIPv4 $BindAddress)) {
    throw "A private IPv4 address for mobile access was not found."
}

$InterfaceIndex = $network.InterfaceIndex
$Profile = Get-NetConnectionProfile -InterfaceIndex $InterfaceIndex -ErrorAction Stop
$MarkerPath = Join-Path $Root ".local-data\mobile-access.json"
$Marker = $null
if (Test-Path -LiteralPath $MarkerPath) {
    try {
        $Marker = Get-Content -LiteralPath $MarkerPath -Raw | ConvertFrom-Json
    }
    catch {
        $Marker = $null
    }
}

$NeedsConfiguration = (
    $Profile.NetworkCategory -ne "Private" -or
    -not $Marker -or
    $Marker.bindAddress -ne $BindAddress -or
    [int]$Marker.port -ne $Port
)

if ($NeedsConfiguration) {
    Write-Host "First run only: approve the Windows administrator prompt."
    $SetupScript = Join-Path $Root "configure-mobile-private.ps1"
    $PowerShellExe = "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe"
    $SetupArguments = (
        "-NoProfile -ExecutionPolicy Bypass -File `"$SetupScript`" " +
        "-InterfaceIndex $InterfaceIndex -BindAddress $BindAddress " +
        "-Port $Port -PythonExe `"$PythonExe`""
    )
    $SetupProcess = Start-Process `
        -FilePath $PowerShellExe `
        -ArgumentList $SetupArguments `
        -Verb RunAs `
        -Wait `
        -PassThru
    if ($SetupProcess.ExitCode -ne 0) {
        throw "Windows setup for mobile access failed."
    }
    $Profile = Get-NetConnectionProfile -InterfaceIndex $InterfaceIndex -ErrorAction Stop
    if ($Profile.NetworkCategory -ne "Private") {
        throw "The selected Wi-Fi profile is not Private."
    }
    New-Item -ItemType Directory -Path (Split-Path $MarkerPath) -Force | Out-Null
    @{
        bindAddress = $BindAddress
        port = $Port
        interfaceIndex = $InterfaceIndex
        configuredAt = (Get-Date).ToString("o")
    } | ConvertTo-Json | Set-Content -LiteralPath $MarkerPath -Encoding UTF8
}

if (-not $PreviousPassword -or $PreviousPassword.Length -lt 8) {
    $env:CAPITAL_GAIN_RADAR_ACCESS_PASSWORD = New-AccessPassword
}

Push-Location $Root
try {
    Write-Host "Refreshing the verified Nikkei 225 candidate data..."
    & $PythonExe "work\prepare_local_private.py"
    if ($LASTEXITCODE -ne 0) {
        throw "Preparing the local candidate JSON failed."
    }

    Write-Host "Refreshing Nikkei PER and JPX weekly statistics..."
    & $PythonExe "work\market_analysis.py" "--local-private"
    if ($LASTEXITCODE -ne 0) {
        throw "Generating the local analysis JSON failed."
    }

    Write-Host ""
    Write-Host "Mobile URL: http://${BindAddress}:${Port}/"
    Write-Host "Access password: $env:CAPITAL_GAIN_RADAR_ACCESS_PASSWORD"
    Write-Host "Connect the PC and phone to the same trusted Wi-Fi."
    Write-Host "Keep this window open. Press Ctrl+C to stop."
    Write-Host ""

    & $PythonExe `
        "work\local_private_server.py" `
        "--host" $BindAddress `
        "--port" $Port
    if ($LASTEXITCODE -ne 0) {
        throw "Starting the mobile local server failed."
    }
}
finally {
    Pop-Location
    if ($null -eq $PreviousPassword) {
        Remove-Item Env:CAPITAL_GAIN_RADAR_ACCESS_PASSWORD -ErrorAction SilentlyContinue
    }
    else {
        $env:CAPITAL_GAIN_RADAR_ACCESS_PASSWORD = $PreviousPassword
    }
}
