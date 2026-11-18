<#
.SYNOPSIS
    Run a test command and capture one honest coverage artifact per engine.

.DESCRIPTION
    The evidence wrapper `backend/docs/test-gaps.md` tells a reader to use.
    It reads `$LASTEXITCODE` on the next statement with no pipeline (a pipeline
    reports its own exit code, never the suite's), writes exactly ONE artifact
    per engine (`coverage-sqlite.json` / `coverage-pg.json`) and snapshots both,
    refuses `.coverage` data the run did not rewrite, enforces `[report]
    fail_under` from `.coveragerc`, and requires the artifact's totals to match
    `coverage report`.

.PARAMETER Engine
    `sqlite` or `pg`; selects the artifact name and the summary label. It does
    NOT choose the database -- the command does, and the label must match it.

.PARAMETER Command
    Test command as a string, run from the backend directory. It must invoke a
    native executable so `$LASTEXITCODE` is set; a PowerShell-only expression
    fails closed.

.PARAMETER BackendDir
    Directory the command runs in. Defaults to `../backend` beside this script.

.PARAMETER Coveragerc
    Coverage config whose `fail_under` is enforced. Defaults to
    `backend/.coveragerc`; passed as an absolute `--rcfile` to both `coverage
    json` and `coverage report` so they share `omit`/`exclude_lines`.

.EXAMPLE
    .\scripts\test-evidence.ps1 -Engine sqlite -Command "venv\Scripts\python -m coverage run manage.py test --verbosity 0"

.EXAMPLE
    $env:DATABASE_URL = 'postgresql://postgres:postgres@localhost:5433/perfume_store_b'
    $env:REDIS_URL = 'redis://127.0.0.1:6379/2'
    .\scripts\test-evidence.ps1 -Engine pg -Command "venv\Scripts\python -m coverage run manage.py test --verbosity 0"

.NOTES
    Exit codes: the test command's own code when it fails (preserved); 1 for
    every other failed run (stale data, sub-floor coverage, missing or
    disagreeing artifact, the other engine's artifact changed); 2 for a
    usage/environment error. 0 means tests exited 0 AND coverage is at or above
    the floor AND the artifact agrees with the canonical report.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet('sqlite', 'pg')]
    [string]$Engine,

    [Parameter(Mandatory = $true)]
    [string]$Command,

    [string]$BackendDir = '',

    [string]$Coveragerc = ''
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

# Per-engine artifact names, fixed here so no caller can point two engines at
# one file. `pg` is the lane-B container; `postgresql` is the vendor name.
$ArtifactNames = @{ sqlite = 'coverage-sqlite.json'; pg = 'coverage-pg.json' }
$OtherEngine = if ($Engine -eq 'sqlite') { 'pg' } else { 'sqlite' }

if (-not $BackendDir) {
    $BackendDir = Join-Path $PSScriptRoot '..\backend'
}
if (-not (Test-Path -LiteralPath $BackendDir -PathType Container)) {
    Write-Host "test-evidence: backend directory not found: $BackendDir" -ForegroundColor Red
    exit 2
}
$BackendDir = (Resolve-Path -LiteralPath $BackendDir).Path

if (-not $Coveragerc) {
    $Coveragerc = Join-Path $BackendDir '.coveragerc'
}
if (-not (Test-Path -LiteralPath $Coveragerc -PathType Leaf)) {
    Write-Host "test-evidence: coverage config not found: $Coveragerc" -ForegroundColor Red
    exit 2
}

$Python = Join-Path $BackendDir 'venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) {
    $Python = 'python'
}

# The floor is read from [report] fail_under: a second hardcoded copy is a
# second source of truth, and the first thing to drift.
$FailUnder = $null
$InReport = $false
foreach ($line in Get-Content -LiteralPath $Coveragerc) {
    $trimmed = $line.Trim()
    if ($trimmed -match '^\[(.+)\]$') {
        $InReport = ($Matches[1] -eq 'report')
        continue
    }
    if ($InReport -and $trimmed -match '^fail_under\s*=\s*([0-9]+(?:\.[0-9]+)?)\s*$') {
        $FailUnder = [double]$Matches[1]
        break
    }
}
if ($null -eq $FailUnder) {
    Write-Host "test-evidence: no [report] fail_under in $Coveragerc" -ForegroundColor Red
    exit 2
}

$Artifact = Join-Path $BackendDir $ArtifactNames[$Engine]
$OtherArtifact = Join-Path $BackendDir $ArtifactNames[$OtherEngine]
$CoverageData = Join-Path $BackendDir '.coverage'

# Snapshot BOTH artifacts' existence AND hash before the run: an absent file
# is a state, so a command that creates the other engine's file is caught.
function Get-ArtifactState {
    param([string]$Path)
    if (Test-Path -LiteralPath $Path -PathType Leaf) {
        return [pscustomobject]@{
            Exists = $true
            Hash   = (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash
        }
    }
    return [pscustomobject]@{ Exists = $false; Hash = $null }
}

$ArtifactsBefore = @{}
foreach ($path in @($Artifact, $OtherArtifact)) {
    $ArtifactsBefore[$path] = Get-ArtifactState -Path $path
}

function Get-CoverageDataStamp {
    if (-not (Test-Path -LiteralPath $CoverageData -PathType Leaf)) {
        return $null
    }
    return (Get-Item -LiteralPath $CoverageData).LastWriteTimeUtc.Ticks
}

$DataBefore = Get-CoverageDataStamp

Write-Host "test-evidence: engine=$Engine backend=$BackendDir"
Write-Host "test-evidence: command=$Command"

$Location = Get-Location
$TestExit = $null
try {
    Set-Location -LiteralPath $BackendDir
    # Clear the stale native exit code first, and deliberately do NOT pipe the
    # command: `$LASTEXITCODE` belongs to the last native command, so
    # `| Tee-Object`/`| Out-String` would report the pipeline's success instead.
    $global:LASTEXITCODE = $null
    Invoke-Expression $Command
    $TestExit = $LASTEXITCODE
}
catch {
    Write-Host "test-evidence: command could not be run: $_" -ForegroundColor Red
    $TestExit = 1
}
finally {
    Set-Location -LiteralPath $Location
}

if ($null -eq $TestExit) {
    # Fail closed: a PowerShell-only command never sets $LASTEXITCODE.
    Write-Host "test-evidence: the command did not set `$LASTEXITCODE (not a native executable); refusing to report success" -ForegroundColor Red
    exit 1
}
Write-Host "test-evidence: test exit code = $TestExit"

$DataAfter = Get-CoverageDataStamp
if ($null -eq $DataAfter) {
    Write-Host "test-evidence: no .coverage database was produced; the command must be a 'coverage run' invocation" -ForegroundColor Red
    if ($TestExit -ne 0) { exit $TestExit }
    exit 1
}
if ($DataAfter -eq $DataBefore) {
    Write-Host "test-evidence: .coverage was not rewritten by this command; refusing to report an artifact over stale data" -ForegroundColor Red
    if ($TestExit -ne 0) { exit $TestExit }
    exit 1
}

# 3. ONE artifact for this engine, via `coverage json`. The floor is also
#    passed to coverage itself, so a sub-floor run cannot exit 0 even if this
#    script's own comparison were skipped.
$CoverageExit = $null
$CoveragePercent = $null
if (Test-Path -LiteralPath $Artifact -PathType Leaf) {
    Remove-Item -LiteralPath $Artifact -Force
}
# `--rcfile` is load-bearing: `coverage json` finds `.coveragerc` in the
# CURRENT directory, so without it the artifact used coverage.py's defaults
# (no `omit`, no `exclude_lines`) and the totals stopped matching.
& $Python -m coverage json "--rcfile=$Coveragerc" --data-file=$CoverageData --pretty-print --fail-under=$FailUnder -o $Artifact
$CoverageExit = $LASTEXITCODE

if (-not (Test-Path -LiteralPath $Artifact -PathType Leaf)) {
    Write-Host "test-evidence: coverage json wrote no artifact at $Artifact (exit $CoverageExit)" -ForegroundColor Red
    if ($TestExit -ne 0) { exit $TestExit }
    exit 1
}
# Parse with the interpreter that wrote it: Windows PowerShell 5.1's
# ConvertFrom-Json rejects coverage.py's absolute-path member names.
$TotalsLine = & $Python -c "import json, sys; t = json.load(open(sys.argv[1], encoding='utf-8'))['totals']; print('|'.join(str(t[k]) for k in ('percent_covered', 'percent_covered_display', 'num_statements', 'missing_lines')))" $Artifact
if ($LASTEXITCODE -ne 0 -or -not $TotalsLine) {
    Write-Host "test-evidence: could not read totals out of $Artifact" -ForegroundColor Red
    if ($TestExit -ne 0) { exit $TestExit }
    exit 1
}
$TotalParts = ([string]$TotalsLine).Trim().Split('|')
$CoveragePercent = [double]$TotalParts[0]
$CoverageDisplay = [string]$TotalParts[1]
$ArtifactStatements = [int]$TotalParts[2]
$ArtifactMissed = [int]$TotalParts[3]

# 3b. The artifact must agree with the canonical `coverage report` over the
#     same data file and config. A hardcoded total would itself go stale.
$ReportLines = & $Python -m coverage report "--rcfile=$Coveragerc" --data-file=$CoverageData --fail-under=0
$ReportExit = $LASTEXITCODE
$TotalRow = @($ReportLines | Where-Object { $_ -match '^TOTAL\s' }) | Select-Object -First 1
if ($ReportExit -ne 0 -or -not $TotalRow) {
    # `--fail-under=0` keeps a sub-floor total from exiting 2 here: the floor
    # is enforced below and by `coverage json`; this step only compares counts.
    Write-Host "test-evidence: coverage report could not be read (exit $ReportExit); the artifact totals are unverified" -ForegroundColor Red
    if ($TestExit -ne 0) { exit $TestExit }
    exit 1
}
Write-Host ("test-evidence: canonical report total: {0}" -f ([string]$TotalRow).Trim())
$TotalCells = ([string]$TotalRow).Trim() -split '\s+'
$ReportStatements = [int]$TotalCells[1]
$ReportMissed = [int]$TotalCells[2]
if ($ArtifactStatements -ne $ReportStatements -or $ArtifactMissed -ne $ReportMissed) {
    Write-Host ("test-evidence: artifact totals disagree with coverage report ({0} stmts/{1} missed vs {2} stmts/{3} missed); the artifact was measured with a different config" -f $ArtifactStatements, $ArtifactMissed, $ReportStatements, $ReportMissed) -ForegroundColor Red
    if ($TestExit -ne 0) { exit $TestExit }
    exit 1
}

# 4. Neither artifact may be created, deleted or rewritten by a run labelled
#    with ONE engine; comparing existence catches the created-from-nothing case.
$OtherMoved = $false
$OtherBeforeState = $ArtifactsBefore[$OtherArtifact]
$OtherAfterState = Get-ArtifactState -Path $OtherArtifact
if ($OtherAfterState.Exists -ne $OtherBeforeState.Exists) {
    $verb = if ($OtherAfterState.Exists) { 'was created' } else { 'was deleted' }
    Write-Host "test-evidence: $($ArtifactNames[$OtherEngine]) $verb during a $Engine run; one invocation must write one engine's artifact" -ForegroundColor Red
    $OtherMoved = $true
}
elseif ($OtherAfterState.Exists -and $OtherAfterState.Hash -ne $OtherBeforeState.Hash) {
    Write-Host "test-evidence: $($ArtifactNames[$OtherEngine]) changed during a $Engine run; one invocation must write one engine's artifact" -ForegroundColor Red
    $OtherMoved = $true
}

$ArtifactHash = (Get-FileHash -LiteralPath $Artifact -Algorithm SHA256).Hash
Write-Host ("test-evidence: artifact={0} sha256={1}" -f $Artifact, $ArtifactHash)
Write-Host ("test-evidence: coverage={0}% floor={1}% coverage-exit={2}" -f $CoverageDisplay, $FailUnder, $CoverageExit)

# --------------------------------------------------------------------------
# Verdict. The test command's exit code is preserved when it failed -- a red
# suite must never be reported as 1 when the suite said 3.
# --------------------------------------------------------------------------
if ($TestExit -ne 0) {
    Write-Host "test-evidence: FAILED - test command exited $TestExit" -ForegroundColor Red
    exit $TestExit
}
if ($OtherMoved) {
    exit 1
}
if ($CoverageExit -ne 0) {
    Write-Host "test-evidence: FAILED - coverage json exited $CoverageExit (below fail_under=$FailUnder)" -ForegroundColor Red
    exit 1
}
if ($CoveragePercent -lt $FailUnder) {
    Write-Host ("test-evidence: FAILED - {0}% is below fail_under={1}%" -f $CoveragePercent, $FailUnder) -ForegroundColor Red
    exit 1
}

Write-Host ("test-evidence: OK - {0}% >= fail_under={1}%, tests exit 0" -f $CoverageDisplay, $FailUnder) -ForegroundColor Green
exit 0
