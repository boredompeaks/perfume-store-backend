<#
.SYNOPSIS
    Run a test command and capture one honest coverage artifact per engine.

.DESCRIPTION
    This is the evidence wrapper that `backend/docs/test-gaps.md` tells a reader
    to use. It exists because two evidence files in this repo were written by a
    shell pipeline that hid the process exit code (`Q-S3-evidence.md` records
    FAILED runs with `exit 0`) and because `pg_cov.log` was byte-identical to
    `sqlite_cov.log`, which is only possible if one engine's "measured" output
    was a copy of the other's.

    Four things it does, each one closing that defect:

      1. It reads the test command's `$LASTEXITCODE` on the very next statement
         and never pipes the command's output. A native command's exit code is
         not the exit code of `Tee-Object`, `Select-String` or `Out-String`, so
         any pipeline between the command and the read is how "FAILED" gets
         recorded next to "exit 0".
      2. It writes EXACTLY ONE artifact per engine -- `coverage-sqlite.json` or
         `coverage-pg.json` -- through `coverage json` with `.coveragerc` passed
         explicitly as `--rcfile`, and it snapshots BOTH artifacts' existence
         and hash around the run, so a run that creates, deletes or rewrites
         the other engine's artifact fails. Two engines can never share a file.
      3. It refuses STALE coverage data: the `.coverage` database must have been
         rewritten by the command it just ran. A `coverage json` over yesterday's
         data is a plausible-looking wrong number, not evidence.
      4. It asserts the total against `[report] fail_under` read from
         `.coveragerc` (and also passes the floor to `coverage json` itself), so
         a sub-floor run exits non-zero even when every test passed.
      5. It asserts the artifact's statement totals equal the canonical
         `coverage report` over the same data file and config. `coverage json`
         locates `.coveragerc` relative to the CURRENT directory, so an artifact
         written from the caller's cwd silently uses coverage.py's defaults --
         no `exclude_lines`, so a subprocess-only guard is counted as missed --
         and its totals stop matching `coverage report`. Both commands now receive the absolute `--rcfile`, and a disagreement
         between them fails the run rather than being filed as evidence.

    Nothing here parses prose. The artifact is produced by coverage.py and the
    numbers printed are read back out of that artifact.

.PARAMETER Engine
    Which engine this run is measuring: `sqlite` or `pg`. It selects the
    artifact name and is recorded in the summary line. It does NOT choose the
    database -- the test command decides that, and a run labelled `pg` against
    SQLite is the exact defect this wrapper was written for, so state the
    database in the command (see EXAMPLES) and keep the label honest.

.PARAMETER Command
    The test command to run, as a string, e.g.
    `"venv\Scripts\python -m coverage run manage.py test --verbosity 0"`.
    It runs from the backend directory. It must invoke a native executable
    (`python`, `manage.py`, ...) so that `$LASTEXITCODE` is set; a PowerShell
    expression that does not set it fails closed rather than reporting success.

.PARAMETER BackendDir
    The backend directory the command runs in. Defaults to `../backend`
    relative to this script.

.PARAMETER Coveragerc
    The coverage config whose `fail_under` is enforced. Defaults to
    `backend/.coveragerc`. It is passed to both `coverage json` and `coverage
    report` as an absolute `--rcfile`, so both read the same `omit` and
    `exclude_lines` rules regardless of the caller's working directory.

.EXAMPLE
    # SQLite leg
    .\scripts\test-evidence.ps1 -Engine sqlite -Command "venv\Scripts\python -m coverage run manage.py test --verbosity 0"

.EXAMPLE
    # PostgreSQL leg: lane B's pg2b container, database perfume_store_b.
    $env:DATABASE_URL = 'postgresql://postgres:postgres@localhost:5433/perfume_store_b'
    $env:REDIS_URL = 'redis://127.0.0.1:6379/2'
    .\scripts\test-evidence.ps1 -Engine pg -Command "venv\Scripts\python -m coverage run manage.py test --verbosity 0"

.NOTES
    Exit codes: the test command's own exit code when it fails (preserved, never
    rewritten to 1); 1 when the run failed for another reason (stale coverage
    data, sub-floor coverage, missing artifact, artifact totals that disagree
    with `coverage report`, the other engine's artifact created/deleted/changed);
    2 for a usage/environment error (bad paths, unparsable `.coveragerc`). 0
    means: the tests exited 0 AND coverage is at or above the floor AND the
    artifact agrees with the canonical report.
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
# one file. `pg` is the lane-B container (`pg2b`); `postgresql` is accepted by
# coverage/changelog_figures as the engine *vendor* name, and the label here is
# the short one because that is what the artifact filename carries.
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

# --------------------------------------------------------------------------
# The floor, read from [report] fail_under. Parsed rather than hardcoded: a
# wrapper that carries its own copy of the gate is a second source of truth,
# and the first thing to drift.
# --------------------------------------------------------------------------
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

# Snapshot BOTH artifacts' existence AND content before the run. The guard
# used to arm only when the other artifact already existed, so a command that
# created it from nothing -- the exact way one engine's "measurement" becomes a
# copy of the other's -- slipped through as "no change". An absent file is a
# state: Exists=False is compared like any hash.
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

# --------------------------------------------------------------------------
# 1. Run the tests. No pipeline between the call and the read.
# --------------------------------------------------------------------------
Write-Host "test-evidence: engine=$Engine backend=$BackendDir"
Write-Host "test-evidence: command=$Command"

$Location = Get-Location
$TestExit = $null
try {
    Set-Location -LiteralPath $BackendDir
    # Clear the previous native exit code first: a stale 0 inherited from an
    # earlier command in the same session is the other half of the masking
    # defect, and a PowerShell-only command would then look successful.
    $global:LASTEXITCODE = $null
    # Deliberately NOT piped: `$LASTEXITCODE` belongs to the last native
    # command, so a `| Tee-Object`/`| Out-String` here would report the
    # pipeline's success instead of the suite's. The output goes straight to
    # the console for the same reason.
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
    # Fail closed. A PowerShell-only command never sets $LASTEXITCODE, and
    # "$null -eq 0" is false, so an unguarded script would report success for
    # a command that may never have run.
    Write-Host "test-evidence: the command did not set `$LASTEXITCODE (not a native executable); refusing to report success" -ForegroundColor Red
    exit 1
}
Write-Host "test-evidence: test exit code = $TestExit"

# --------------------------------------------------------------------------
# 2. Refuse stale coverage data. The artifact must describe the run above.
# --------------------------------------------------------------------------
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

# --------------------------------------------------------------------------
# 3. ONE artifact for this engine, via `coverage json`. The floor is passed to
#    coverage itself as well, so a sub-floor run cannot exit 0 even if this
#    script's own comparison were skipped.
# --------------------------------------------------------------------------
$CoverageExit = $null
$CoveragePercent = $null
if (Test-Path -LiteralPath $Artifact -PathType Leaf) {
    Remove-Item -LiteralPath $Artifact -Force
}
# `--rcfile` is load-bearing. `coverage json` looks for `.coveragerc` in the
# CURRENT directory, and this line runs after the script restored the caller's
# location: without the flag the artifact silently used coverage.py's defaults
# (no `omit`, no `exclude_lines`) and counted the subprocess-only guard in
# config/settings.py as missed. The path is absolute, so the artifact does not
# depend on where the wrapper was invoked.
& $Python -m coverage json "--rcfile=$Coveragerc" --data-file=$CoverageData --pretty-print --fail-under=$FailUnder -o $Artifact
$CoverageExit = $LASTEXITCODE

if (-not (Test-Path -LiteralPath $Artifact -PathType Leaf)) {
    Write-Host "test-evidence: coverage json wrote no artifact at $Artifact (exit $CoverageExit)" -ForegroundColor Red
    if ($TestExit -ne 0) { exit $TestExit }
    exit 1
}
# Parse the artifact with the interpreter that wrote it. Windows PowerShell's
# ConvertFrom-Json cannot ingest coverage.py's `files` object: its keys are
# absolute Windows paths, and 5.1 rejects them as member names. Reading the
# same artifact through json.load is the same number, without that parser bug.
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

# --------------------------------------------------------------------------
# 3b. The artifact must agree with the canonical `coverage report` over the
#     same data file and the same config. Comparing the two counts, rather than
#     asserting a remembered total, is what keeps this check honest at every
#     future tip: a hardcoded 14005 would itself become the next stale figure.
#     A disagreement means the artifact was produced under a different config,
#     which is exactly the 14007/1-by-a-default-config defect.
# --------------------------------------------------------------------------
$ReportLines = & $Python -m coverage report "--rcfile=$Coveragerc" --data-file=$CoverageData --fail-under=0
$ReportExit = $LASTEXITCODE
$TotalRow = @($ReportLines | Where-Object { $_ -match '^TOTAL\s' }) | Select-Object -First 1
if ($ReportExit -ne 0 -or -not $TotalRow) {
    # `--fail-under=0` keeps a sub-floor total from exiting 2 here: the floor is
    # enforced below and by `coverage json`, and this step only compares counts.
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

# --------------------------------------------------------------------------
# 4. Neither artifact may be created, deleted or rewritten by a run labelled
#    with ONE engine. Comparing existence as well as content is what catches a
#    command that creates the other engine's file from scratch; the previous
#    guard skipped the comparison whenever that file did not already exist.
# --------------------------------------------------------------------------
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
