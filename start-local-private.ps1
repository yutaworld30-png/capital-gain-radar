[CmdletBinding()]
param(
    [ValidateRange(1024, 65535)]
    [int]$Port = 8766
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$PythonExe = $env:CAPITAL_GAIN_RADAR_PYTHON

if (-not $PythonExe) {
    $PythonCommand = Get-Command python -ErrorAction SilentlyContinue
    if ($PythonCommand) {
        $PythonExe = $PythonCommand.Source
    }
}

if (-not $PythonExe -or -not (Test-Path -LiteralPath $PythonExe)) {
    throw "Pythonが見つかりません。CAPITAL_GAIN_RADAR_PYTHONにpython.exeの絶対パスを設定してください。"
}

Push-Location $Root
try {
    Write-Host "検証済み225銘柄データをローカル専用領域へ更新しています..."
    & $PythonExe "work\prepare_local_private.py"
    if ($LASTEXITCODE -ne 0) {
        throw "ローカル候補JSONの準備に失敗しました。"
    }

    Write-Host "日経PER・JPX週次統計をローカル専用領域へ更新しています..."
    & $PythonExe "work\market_analysis.py" "--local-private"
    if ($LASTEXITCODE -ne 0) {
        throw "ローカル分析JSONの生成に失敗しました。"
    }

    Write-Host ""
    Write-Host "ローカル専用サーバーを起動します。"
    & $PythonExe "work\local_private_server.py" "--port" $Port
    if ($LASTEXITCODE -ne 0) {
        throw "ローカル専用サーバーを起動できませんでした。"
    }
}
finally {
    Pop-Location
}
