[CmdletBinding()]
param(
    [switch]$VerifyOnly
)

$ErrorActionPreference = 'Stop'
$repo = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$composeFile = Join-Path $repo 'docker-compose.test.yml'
$project = 'perfume-tmpfs-' + [Guid]::NewGuid().ToString('N')
$compose = @('compose', '--project-name', $project, '--file', $composeFile, '--profile', 'tmpfs')
$started = $false

function Invoke-Docker {
    param([string[]]$Arguments)
    & docker @Arguments
    if ($LASTEXITCODE -ne 0) { throw "docker command failed ($LASTEXITCODE)" }
}

function Get-ServiceContainerId {
    param([string]$Service)
    $ids = & docker @compose ps --all --quiet $Service
    if ($LASTEXITCODE -ne 0 -or -not $ids) { throw "No container found for service $Service" }
    return ($ids | Select-Object -First 1).Trim()
}

function Assert-TmpfsMounts {
    param([string]$Container, [string[]]$Destinations)
    $raw = & docker inspect --format '{{json .HostConfig.Tmpfs}}' $Container
    if ($LASTEXITCODE -ne 0) { throw 'docker inspect failed' }
    $configured = $raw | ConvertFrom-Json
    $mountTable = & docker exec $Container cat /proc/mounts
    if ($LASTEXITCODE -ne 0) { throw 'Could not inspect the running container mount table' }
    $mountedPaths = @($mountTable | ForEach-Object {
        $fields = $_ -split '\s+'
        if ($fields.Count -ge 3 -and $fields[2] -eq 'tmpfs') { $fields[1] }
    })
    foreach ($destination in $Destinations) {
        $configEntry = $configured.PSObject.Properties[$destination]
        if (-not $configEntry -or $destination -notin $mountedPaths) {
            throw "Runtime mount check failed: $destination is not tmpfs"
        }
    }
}

try {
    New-Item -ItemType Directory -Force (Join-Path $repo 'scripts/figures') | Out-Null
    Invoke-Docker ($compose + @('config', '--quiet'))
    Invoke-Docker ($compose + @('up', '--detach', '--build', 'db', 'runner'))
    $started = $true

    $dbId = Get-ServiceContainerId 'db'
    $runnerId = Get-ServiceContainerId 'runner'
    Assert-TmpfsMounts $dbId @('/var/lib/postgresql/data')
    Assert-TmpfsMounts $runnerId @('/tmp/media', '/tmp/coverage', '/tmp/pytest', '/tmp/ruff', '/tmp/sqlite')

    $fsync = & docker @compose exec --no-TTY db psql -U test -d test -Atc 'SHOW fsync'
    if ($LASTEXITCODE -ne 0 -or $fsync.Trim() -ne 'off') {
        throw 'Runtime PostgreSQL setting check failed: SHOW fsync was not off'
    }

    # changelog_figures runs its SQLite leg first, then PostgreSQL, serially;
    # the profile has one test runner and one database container.
    if (-not $VerifyOnly) {
        Invoke-Docker ($compose + @('exec', '--no-TTY', 'runner', 'python', '/workspace/scripts/changelog_figures.py', 'floor', '--task', 'PRISTINE-12', '--pg-url', 'postgresql://test:test@db:5432/test?sslmode=disable'))
        Invoke-Docker ($compose + @('exec', '--no-TTY', 'runner', 'python', 'manage.py', 'makemigrations', '--check', '--dry-run'))
        Invoke-Docker ($compose + @('exec', '--no-TTY', 'runner', 'ruff', 'check', '.'))
    } else {
        Write-Host 'VERIFY-ONLY: skipped suite/migrations/ruff (auditor owns suite runs).'
    }
}
finally {
    if ($started) {
        & docker @compose down --remove-orphans
        if ($LASTEXITCODE -ne 0) { throw "Cleanup failed for test project $project" }
    }
}
