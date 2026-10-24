[CmdletBinding()]
param(
    [switch]$Submit,
    [switch]$Continuous,
    [switch]$ShowCommand
)

$ErrorActionPreference = 'Stop'
if ($Continuous -and -not $Submit) {
    throw '-Continuous requires -Submit. Omit both switches for a one-time preview.'
}
$candidatePython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $candidatePython -PathType Leaf)) {
    throw 'Project Python environment is missing. Follow OPERATING_GUIDE.md first.'
}

# Frozen relative_strength_189 candidate from PROFITABILITY_REVIEW.md.
# Set parameters explicitly so changes to a CLI preset do not change this run.
$candidateArguments = @(
    '--symbols-file', 'universe_popular.txt',
    '--profile', 'custom', '--strategy', 'relative-strength',
    '--cash', '100000', '--days', '600', '--feed', 'iex', '--entry-limit-bps', '5',
    '--short-window', '20', '--long-window', '100', '--min-confidence', '0.68',
    '--max-open-positions', '4', '--max-daily-orders', '2',
    '--max-position-pct', '0.25', '--max-order-pct', '0.10', '--target-cash-pct', '0.08',
    '--stop-loss-pct', '0.10', '--trailing-stop-pct', '0.10',
    '--max-drawdown-stop-pct', '0.15', '--slippage-bps', '5', '--commission', '0',
    '--buy-cooldown-days', '3'
)

if ($Submit) {
    $candidateCommand = 'alpaca-session'
    $candidateArguments += @(
        '--journal-dir', 'var', '--submit',
        '--risk-interval-minutes', '5', '--signal-interval-minutes', '390',
        '--open-buffer-minutes', '10', '--close-buffer-minutes', '15'
    )
    if ($Continuous) {
        $candidateArguments += '--continuous'
    } else {
        $candidateArguments += @('--until-close', '--wait-for-open')
    }
} else {
    $candidateCommand = 'alpaca-once'
    $candidateArguments += @('--journal-dir', 'var\relative_strength_preview')
}

if ($ShowCommand) {
    Write-Output "$candidatePython -m trading_bot.cli $candidateCommand $($candidateArguments -join ' ')"
    return
}

Push-Location -LiteralPath $PSScriptRoot
try {
    $candidateLogDirectory = Join-Path $PSScriptRoot 'var\logs'
    New-Item -ItemType Directory -Path $candidateLogDirectory -Force | Out-Null
    $candidateLogPath = Join-Path $candidateLogDirectory ("candidate-{0}-{1}.log" -f (Get-Date -Format 'yyyyMMdd-HHmmss-fff'), $PID)
    Write-Host "Full console log: $candidateLogPath"
    if ($Submit) {
        if ($Continuous) {
            Write-Host 'Starting continuous Alpaca PAPER trading with the frozen relative-strength candidate.'
            Write-Host 'Keep this terminal and computer awake. The bot waits through nights and market holidays.'
            Write-Host 'This process does not restart after closing the terminal, a crash, or a computer reboot.'
        } else {
            Write-Host 'Starting one Alpaca PAPER session with the frozen relative-strength candidate.'
            Write-Host 'Keep this terminal and computer awake. The session ends at market close.'
        }
        Write-Host 'Ctrl+C stops the process; existing broker stops remain active.'
    } else {
        Write-Host 'Preview only. No orders will be submitted. Fresh entry quotes require market hours.'
    }
    $candidatePreviousPreference = $ErrorActionPreference
    try {
        # Windows PowerShell converts redirected native stderr to error records.
        # Keep reading through the traceback, preserving the real process exit.
        $ErrorActionPreference = 'Continue'
        & $candidatePython -u -m trading_bot.cli $candidateCommand @candidateArguments 2>&1 |
            ForEach-Object { $_.ToString() } |
            Tee-Object -FilePath $candidateLogPath -ErrorAction Stop
        $candidateExitCode = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $candidatePreviousPreference
    }
    if ($candidateExitCode -ne 0) {
        throw "Candidate stopped with exit code $candidateExitCode. Full error log: $candidateLogPath"
    }
} finally {
    Pop-Location
}
