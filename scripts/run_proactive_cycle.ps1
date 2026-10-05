param(
    [Parameter(Mandatory = $true)][string]$PythonExe,
    [Parameter(Mandatory = $true)][string]$DatabasePath,
    [int]$Limit = 50,
    [switch]$DryRun
)

$repoRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$previousPythonPath = $env:PYTHONPATH
$result = 1
try {
    if (-not (Test-Path -LiteralPath $PythonExe -PathType Leaf)) {
        throw 'Configured Python executable was not found.'
    }
    $env:PYTHONPATH = Join-Path $repoRoot 'src'
    $arguments = @('-B', '-m', 'carmind', 'proactive', '--db', $DatabasePath,
                   'cycle', '--limit', "$Limit", '--console', '--json')
    if ($DryRun) { $arguments += '--dry-run' }
    & $PythonExe @arguments
    $result = $LASTEXITCODE
    if ($null -eq $result) { $result = 1 }
}
catch {
    Write-Error 'Proactive cycle could not start.'
}
finally {
    $env:PYTHONPATH = $previousPythonPath
}
exit $result
