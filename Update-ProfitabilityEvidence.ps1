param([switch]$StatusOnly)
$ErrorActionPreference = 'Stop'
Push-Location -LiteralPath $PSScriptRoot
try {
    $researchPython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
    if (-not (Test-Path -LiteralPath $researchPython)) {
        throw 'The project Python environment is missing. Follow README setup first.'
    }
    $researchAction = 'update'
    if ($StatusOnly) { $researchAction = 'status' }
    & $researchPython -m trading_bot.evidence_update $researchAction
    if ($LASTEXITCODE -ne 0) {
        throw 'Profitability evidence update failed. Previous evidence is preserved; review the error above.'
    }
} finally {
    Pop-Location
}
