[CmdletBinding()]
param(
    [switch]$Submit,
    [string]$ConfirmAccount,
    [string]$Policy = 'live-policy.json'
)
$ErrorActionPreference = 'Stop'
$livePython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $livePython -PathType Leaf)) {
    throw 'Project Python environment is missing.'
}
$liveArguments = @('-u', '-m', 'trading_bot.live_trading')
if ($Submit) {
    if (-not $ConfirmAccount) { throw '-Submit requires -ConfirmAccount with the exact live account UUID.' }
    $liveArguments += @('run', '--confirm-account', $ConfirmAccount)
} else {
    $liveArguments += 'check'
}
$liveArguments += @('--policy', $Policy)
Push-Location -LiteralPath $PSScriptRoot
try {
    $liveRuntime = & $livePython -c 'from trading_bot.live_trading import live_directory; print(live_directory())'
    if ($LASTEXITCODE -ne 0) { throw 'Cannot resolve the separate live runtime directory.' }
    $liveLogDirectory = Join-Path $liveRuntime 'logs'
    New-Item -ItemType Directory -Path $liveLogDirectory -Force | Out-Null
    $liveLog = Join-Path $liveLogDirectory ("live-{0}-{1}.log" -f (Get-Date -Format 'yyyyMMdd-HHmmss-fff'), $PID)
    Write-Host "Live console log: $liveLog"
    $livePreviousPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = 'Continue'
        & $livePython @liveArguments 2>&1 | ForEach-Object { $_.ToString() } | Tee-Object -FilePath $liveLog -ErrorAction Stop
        $liveExitCode = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $livePreviousPreference
    }
    if ($liveExitCode -ne 0) {
        Write-Host "Live operation remains stopped or requires review. Full details: $liveLog"
        exit $liveExitCode
    }
} finally {
    Pop-Location
}
